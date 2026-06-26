"""
smi_acquire.lists
=================

The app's boundary onto the shared **redis db=2 named-list store** (``smi_plans.ListStore``,
prefix ``swaxslists``) — the sibling of :mod:`smi_acquire.store` for *scan inputs* instead of
samples.

Per ``smi-plans/docs/NAMED_LISTS_PLAN.md`` the Redis sample store proved to be an excellent
GUI↔profile channel (reference by name, zero copy-paste), and the same pattern now covers the
other big lists in a scan — **energies** (edges), **incident angles**, **temperatures**,
**exposure/period times**. Instead of the GUI dumping a Python list the user pastes into bluesky,
it **curates a named library of reusable lists** (Redis-backed, view/edit/add in the GUI) and the
generated plan **references them by name** (``resolve_list("Fe_K_XANES", kind="energy")``).

This wrapper mirrors :class:`smi_acquire.store.AcquireStore`:

* connect to the live db=2 ``ListStore.from_redis()`` on this workstation, or fall back to an
  **in-memory/offline** dict store for laptop development (no redis) — a dev convenience, *not* a
  way to see live lists.
* GUI-shaped conveniences: save a :class:`~smi_plans.NamedList` (authoritative ``values`` plus an
  editable ``spec`` and free ``md`` extras), list the names available for a kind, load one back
  for further editing, delete one.

Nothing here imports bluesky/ophyd/Panel; the only dependency is ``smi_plans`` (the pure-Python
``NamedList`` / ``ListStore`` model + its lazily-redis-backed store).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from smi_plans import ListStore, NamedList

#: The list kinds the GUI curates (mirrors ``smi_plans._lists.KINDS``). Open set — an unknown
#: kind still stores/loads its explicit ``values``; only the spec→values *builder* is per-kind.
KINDS = ("energy", "incidence", "temperature", "time")


class AcquireListStore:
    """App-facing wrapper around a :class:`smi_plans.ListStore`.

    Holds the connection (live redis db=2 prefix ``swaxslists`` or offline dict) and presents the
    named scan-input lists to the GUI. All persistence is the shared db=2 store; this object keeps
    no list state of its own.
    """

    def __init__(self, backend: ListStore, *, live: bool, location: str):
        self.store = backend
        self.live = live              # True = real redis db=2; False = offline dict
        self.location = location      # human label for the status bar

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, *, offline: Optional[bool] = None) -> "AcquireListStore":
        """Open the named-list store.

        Tries the live redis db=2 (``ListStore.from_redis()``) unless ``offline`` is forced (or
        ``SMI_ACQUIRE_OFFLINE`` is set). On any connection failure falls back to an in-memory dict
        store so the app still launches (with a clear "offline" status) — this matches
        :meth:`AcquireStore.connect` so there is one connection story.
        """
        if offline is None:
            offline = _env_truthy(os.environ.get("SMI_ACQUIRE_OFFLINE"))
        if not offline:
            try:
                store = ListStore.from_redis()
                # Touch the backend so a dead connection fails here, not later.
                _ = store.list_lists()
                host = os.environ.get("SMI_ACQUIRE_REDIS_HOST", "xf12id2-smi-redis1")
                return cls(store, live=True,
                           location="redis db=2 'swaxslists' @ {}".format(host))
            except Exception as exc:  # noqa: BLE001 (any connection problem -> offline)
                import warnings
                warnings.warn(
                    "live named-list store unavailable ({}: {}); running OFFLINE with an "
                    "in-memory store (no shared lists).".format(type(exc).__name__, exc),
                    stacklevel=2,
                )
        return cls(ListStore({}), live=False, location="offline (in-memory)")

    # ------------------------------------------------------------------
    # CRUD (GUI-shaped)
    # ------------------------------------------------------------------
    def save_list(self, name: str, kind: str, values: List[float], *,
                  spec: Optional[Dict[str, Any]] = None, units: Optional[str] = None,
                  md: Optional[Dict[str, Any]] = None) -> NamedList:
        """Upsert a named list: authoritative ``values`` + an editable ``spec`` + free ``md``.

        The GUI always writes ``values`` (what the plan / dry-run needs) so a reference resolves
        without the backend re-materializing; ``spec`` is the kind's generator recipe kept so the
        entry stays re-editable in the GUI (e.g. an energy editor's ``{boundaries, steps}``), and
        ``md`` carries per-kind extras used only for nice interactions (flux thresholds, ramp
        rates, hold times, cycling) that the plan does not consume. ``(kind, name)`` is the unique
        handle, so saving the same name overwrites.
        """
        existing = self.get_list(name, kind)
        nl = NamedList(
            name=name,
            kind=kind,
            values=[float(v) for v in values],
            spec=dict(spec) if spec else None,
            units=units,
            id=existing.id if existing is not None else NamedList(name=name, kind=kind).id,
            md=dict(md or {}),
        )
        self.store.put_list(nl)
        return nl

    def get_list(self, name: str, kind: str) -> Optional[NamedList]:
        """Load a named list for ``(kind, name)`` (for further GUI editing), or ``None``."""
        return self.store.find_list(name, kind)

    def list_names(self, kind: str) -> List[str]:
        """The names of every stored list of ``kind`` (sorted) — for an 'open' dropdown."""
        return sorted(nl.name for nl in self.store.list_lists(kind=kind))

    def list_all(self, kind: Optional[str] = None) -> List[NamedList]:
        """Every stored :class:`NamedList` (optionally only those of ``kind``)."""
        return self.store.list_lists(kind=kind)

    def delete_list(self, name: str, kind: str) -> None:
        """Remove the ``(kind, name)`` list (no-op if absent)."""
        self.store.delete_list(name, kind)

    def exists(self, name: str, kind: str) -> bool:
        return self.store.find_list(name, kind) is not None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["AcquireListStore", "KINDS"]
