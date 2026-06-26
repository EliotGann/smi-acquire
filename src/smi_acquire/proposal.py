"""
smi_acquire.proposal
====================

A **read-only** view of the beamtime's proposal / data-session identity, for display in the GUI.

The proposal is a **session/queue fact, never a sample fact** (see ``smi-plans`` docs
``QSERVER_WIRING.md`` → "Deferred: proposal/project metadata"): it is set in the beamline IPython
session via ``proposal_id(...)`` (which writes ``RE.md`` in *that* process). The GUI runs in a
**separate process** and deliberately does **not** import the profile / RunEngine / EPICS — so it
cannot read ``RE.md`` directly. Instead it best-effort reads a **shared redis key** (the same
db-server the sample store / interlock use), exactly mirroring :class:`smi_acquire.interlock.Interlock`.

This is **display-only**: the GUI shows the proposal so the operator can confirm it, and carries
the user's *intent* (``project_name``) in generated scripts' ``md`` — it never sets the proposal.

The exact key/db the deployment uses for the persisted ``RE.md`` is a **beamline/profile detail**;
it is configurable here (``SMI_ACQUIRE_PROPOSAL_KEY`` / ``SMI_ACQUIRE_PROPOSAL_DB``) and degrades
to "unknown" when absent, so the GUI stays usable until that key is wired on the profile side
(tracked in ``docs/SMI_PLANS_FOLLOWUPS.md``).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

#: Redis db + key holding the persisted RE.md (bluesky ``PersistentDict``-style). Both overridable
#: by env so the value can be pointed at whatever the SMI profile actually writes.
_DEFAULT_DB = int(os.environ.get("SMI_ACQUIRE_PROPOSAL_DB", "0") or "0")
_DEFAULT_KEY = os.environ.get("SMI_ACQUIRE_PROPOSAL_KEY", "")  # empty -> disabled until wired

#: Keys (in priority order) we look for inside a JSON-dict value to find the proposal id.
_PROPOSAL_FIELDS = ("data_session", "proposal_id", "proposal", "proposal_number")


class Proposal:
    """Best-effort, read-only reader of the current proposal / data-session for display.

    Construct via :meth:`from_redis`. Call :meth:`current` (uncached; poll at ~0.5 Hz) to get the
    proposal string, or ``None`` when it can't be determined (no key configured, redis down,
    off-site). Never raises; never writes.
    """

    def __init__(self, client=None, *, db: int = _DEFAULT_DB, key: str = _DEFAULT_KEY):
        self._client = client
        self.db = db
        self.key = key

    @classmethod
    def from_redis(cls, *, host: Optional[str] = None, port: int = 6380, ssl: bool = True,
                   db: int = _DEFAULT_DB, key: str = _DEFAULT_KEY,
                   password: Optional[str] = None,
                   secret_path: str = "/etc/bluesky/redis.secret") -> "Proposal":
        """Open a best-effort read-only connection (same server as the store).

        Returns a **disabled** reader (``current() -> None``) when no key is configured or on any
        connection failure — the app stays usable; the proposal just shows as unknown.
        """
        if not key:
            return cls(None, db=db, key=key)            # not wired yet -> disabled
        if _env_truthy(os.environ.get("SMI_ACQUIRE_NO_PROPOSAL")):
            return cls(None, db=db, key=key)
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
            return cls(client, db=db, key=key)
        except Exception:
            return cls(None, db=db, key=key)

    # ------------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        if self._client is None or not self.key:
            return {}
        try:
            raw = self._client.get(self.key)
            if raw is None:
                return {}
            if isinstance(raw, bytes):
                raw = raw.decode()
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

    def current(self) -> Optional[str]:
        """The current proposal / data-session string, or ``None`` if it can't be determined."""
        md = self._read()
        for field in _PROPOSAL_FIELDS:
            v = md.get(field)
            if v:
                return str(v)
        return None

    @property
    def configured(self) -> bool:
        """True if a proposal key is configured AND a live connection was established."""
        return self._client is not None and bool(self.key)


# ---------------------------------------------------------------------------
def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["Proposal"]
