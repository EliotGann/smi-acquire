"""Small Redis-backed store for operator-adjustable configuration values.

The YAML microscope config remains the startup/default source. This store is an explicit sync
point for values that operators may adjust from another tool during a beamtime.

These values belong with the operational status/configuration Redis area, not the sample/list
store. By default this uses Redis db=3, next to the RE-busy shared signal.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


_DEFAULT_DB = int(os.environ.get("SMI_ACQUIRE_CONFIG_DB", "3") or "3")
_DEFAULT_PREFIX = os.environ.get("SMI_ACQUIRE_CONFIG_PREFIX", "swaxsconfig")


class AcquireConfigStore:
    """App-facing JSON key/value store for beamline configuration snippets."""

    def __init__(self, client=None, *, live: bool, location: str,
                 prefix: str = _DEFAULT_PREFIX):
        self._client = client
        self.live = live
        self.location = location
        self.prefix = prefix.rstrip(":")
        self._offline: dict[str, dict[str, Any]] = {}

    @classmethod
    def connect(
        cls,
        *,
        offline: Optional[bool] = None,
        host: Optional[str] = None,
        port: int = 6380,
        ssl: bool = True,
        db: int = _DEFAULT_DB,
        prefix: str = _DEFAULT_PREFIX,
        password: Optional[str] = None,
        secret_path: str = "/etc/bluesky/redis.secret",
    ) -> "AcquireConfigStore":
        if offline is None:
            offline = _env_truthy(os.environ.get("SMI_ACQUIRE_OFFLINE")) or _env_truthy(
                os.environ.get("SMI_ACQUIRE_NO_CONFIG_STORE")
            )
        if not offline:
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
                return cls(client, live=True, location=f"redis db={db} '{prefix}' @ {host}",
                           prefix=prefix)
            except Exception as exc:  # noqa: BLE001
                import warnings

                warnings.warn(
                    "live config store unavailable ({}: {}); running with an in-memory "
                    "config store.".format(type(exc).__name__, exc),
                    stacklevel=2,
                )
        return cls(None, live=False, location="offline (in-memory)", prefix=prefix)

    def get(self, name: str) -> dict[str, Any] | None:
        if self._client is None:
            val = self._offline.get(name)
            return dict(val) if val is not None else None
        raw = self._client.get(self._key(name))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        val = json.loads(raw)
        return val if isinstance(val, dict) else None

    def put(self, name: str, value: dict[str, Any]) -> None:
        payload = dict(value)
        if self._client is None:
            self._offline[name] = payload
            return
        self._client.set(self._key(name), json.dumps(payload, sort_keys=True))

    def _key(self, name: str) -> str:
        return f"{self.prefix}:{name}"


def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["AcquireConfigStore"]
