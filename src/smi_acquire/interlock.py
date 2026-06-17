"""
smi_acquire.interlock
=====================

A **safety interlock** that lets the app lock out its own direct-EPICS actions (motor jogging,
camera exposure/focus writes) whenever an external bluesky **RunEngine is running**.

Why
---
Until the queueserver arrives, this app drives motors for alignment via direct
``ophyd.EpicsMotor.set()`` (fast, lightweight, working).  The actual scans run on a **dedicated
RunEngine elsewhere** (the beamline IPython session).  Two processes commanding the same motors
is dangerous: a jog issued here mid-scan would corrupt a run.  The clean, qserver-ready guard is
a **shared "RE busy" flag in redis** that the app reads before any direct action.

Status (LIVE)
-------------
The producer **exists on the bluesky side** (``re_status.py``): the RunEngine is the **sole
writer** of a busy flag in redis **db=3**, key **``swaxsstatus:re_busy``** (same host/port/ssl/
password as the sample store).  This interlock is the **reader**.

The contract (from the bluesky side):

* **Absent key → RE idle → the GUI may move.**  Present → parse JSON; ``{"busy": true, ...}``
  → locked out.  The value also carries ``plan``, ``scan_id``, ``since``, ``host``, ``pid`` so
  the banner can show *why* and *since when*.
* **Anti-latch:** the key has a **30 s TTL refreshed every 10 s**.  So "absent = idle" is always
  safe — if the worker dies, the key vanishes within 30 s and the GUI auto-unlocks.  Therefore
  the GUI must **poll** (1–2 Hz) and **must NOT cache the idle state** (a stale "idle" cache
  would defeat the lock; a stale "busy" cache would defeat the anti-latch).
* **Read-only:** the GUI treats the key as read-only and never writes it.  (If the GUI ever
  needs the reverse direction — assert its own lock so the RE waits — that is a *separate* key.)

If redis is unreachable (off-site, no secret), the interlock degrades to permanently-unlocked
(fail-open) and the operator relies on judgement.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

# The bluesky-side contract: db=3, key 'swaxsstatus:re_busy', 30s TTL / 10s heartbeat.
_DEFAULT_DB = int(os.environ.get("SMI_ACQUIRE_REBUSY_DB", "3"))
_DEFAULT_KEY = os.environ.get("SMI_ACQUIRE_REBUSY_KEY", "swaxsstatus:re_busy")


class Interlock:
    """Advisory lock keyed off the external "RunEngine busy" redis flag (db=3, read-only).

    Construct via :meth:`from_redis` (best-effort; degrades to a permanently-unlocked
    :class:`Interlock` if redis is unavailable).  Call :meth:`is_busy` before any direct-EPICS
    action and show :meth:`banner` when busy.  **Poll** it (1–2 Hz); the read is **not cached**
    so the 30 s-TTL anti-latch works (a dead worker auto-unlocks within the TTL).
    """

    def __init__(self, client=None, *, db: int = _DEFAULT_DB, key: str = _DEFAULT_KEY,
                 enabled: bool = True):
        self._client = client
        self.db = db
        self.key = key
        self.enabled = enabled and client is not None

    @classmethod
    def from_redis(cls, *, host: Optional[str] = None, port: int = 6380, ssl: bool = True,
                   db: int = _DEFAULT_DB, key: str = _DEFAULT_KEY,
                   password: Optional[str] = None,
                   secret_path: str = "/etc/bluesky/redis.secret") -> "Interlock":
        """Open a best-effort connection to the busy flag (db=3, same server as the store).

        On any failure (no redis, no secret, off-site) returns a **disabled** interlock that
        always reports "not busy" — the app stays usable; the operator relies on judgement.
        """
        if _env_truthy(os.environ.get("SMI_ACQUIRE_NO_INTERLOCK")):
            return cls(None, db=db, key=key, enabled=False)
        try:
            import redis
            host = host or os.environ.get("SMI_ACQUIRE_REDIS_HOST",
                                          "xf12id2-smi-redis1.nsls2.bnl.gov")
            if password is None:
                with open(secret_path) as fh:
                    password = fh.read().strip()
            client = redis.Redis(host, db=db, ssl=ssl, port=port, password=password,
                                 socket_timeout=2, socket_connect_timeout=2)
            client.ping()
            return cls(client, db=db, key=key, enabled=True)
        except Exception:
            return cls(None, db=db, key=key, enabled=False)

    # ------------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        """Fetch + parse the flag fresh (``{}`` when absent / disabled / on any error).

        Deliberately **uncached** — the bluesky side keeps the key on a 30 s TTL refreshed every
        10 s, so a fresh read is what makes "absent = idle" safe (a stale cache would either
        defeat the lock or defeat the anti-latch).  Callers poll this at 1–2 Hz.
        """
        if not self.enabled:
            return {}
        try:
            raw = self._client.get(self.key)
            if raw is None:
                return {}                       # absent => idle (anti-latch)
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                val = json.loads(raw)
            except (ValueError, TypeError):
                val = raw
            if isinstance(val, dict):
                return val
            if _truthy(val):
                return {"busy": True}
            return {}
        except Exception:
            return {}                           # fail-open: advisory only

    def is_busy(self) -> bool:
        """True only if the external RunEngine has published a busy flag (else False)."""
        flag = self._read()
        return bool(flag.get("busy", False))

    def banner(self) -> str:
        """A short human banner describing the lock (empty when not busy).

        Surfaces the bluesky-side context (``plan``/``scan_id``/``host``) when present.
        """
        flag = self._read()
        if not flag.get("busy", False):
            return ""
        bits = []
        if flag.get("plan"):
            bits.append("plan {}".format(flag["plan"]))
        if flag.get("scan_id") is not None:
            bits.append("scan {}".format(flag["scan_id"]))
        if flag.get("host"):
            bits.append("on {}".format(flag["host"]))
        detail = (" — " + ", ".join(bits)) if bits else ""
        return ("RunEngine busy{}: direct motor/camera control is locked out here. "
                "(Auto-unlocks when the run finishes.)".format(detail))

    @property
    def active(self) -> bool:
        """Whether the interlock is actually wired to a live flag (vs. inert/disabled)."""
        return self.enabled


# ---------------------------------------------------------------------------
def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "busy", "running", "paused")
    return bool(v)


def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["Interlock"]
