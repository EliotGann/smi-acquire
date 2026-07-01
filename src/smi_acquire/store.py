"""
smi_acquire.store
=================

The app's boundary onto the **shared redis db=2 sample store** (the ``smi_plans.SampleStore``).

The sample list is the spine of this app, and per ``smi-plans/docs/SAMPLE_SYSTEM_PLAN.md`` the
canonical samples/holders/active-pointer live in **Redis db=2** (``'swaxssamples'``), the
shared bus every tool (this app, the beamline session, a future qserver worker) reads and
writes.  This module wraps that facade with the few app-shaped conveniences the GUI needs:

* connect to the live store (``SampleStore.from_redis()``) on this workstation, or fall back to
  an **in-memory/offline** store for laptop development (no redis) — a dev convenience, *not* a
  way to see live samples (§1b).
* the app's old "sample sets" are now **holders** (the redis :class:`~smi_plans.Holder`); a
  sample belongs to exactly one holder (its bar/plate/cell).  References (fiducials) and the
  transient experiment recipes stay **local** (see :mod:`smi_acquire.project`).
* capture a live stage reading into a sample's ``nominal`` :class:`~smi_plans.Position`,
  recording every axis the microscope exposes.  Click-to-move defaults to the **piezo** fine
  stage (fast/precise); the coarse **Huber stage** augments range/orientation — both sets of
  axes are captured so a position is fully described regardless of which the user drove.

Nothing here imports bluesky/ophyd/Panel; the only dependency is ``smi_plans`` (pure-Python
model + the lazily-redis-backed store).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from smi_plans import Holder, Position, Sample, SampleStore

# The on-axis microscope reads three Cartesian motors (x/y/z).  By the SMI stage stack the
# fast/precise top stage is the piezo, so a captured x/y/z defaults onto the piezo axes; the
# coarse Huber stage augments reach/orientation.  These maps let a capture record *all* exposed
# axes into one Position (see ``capture_position``).
PIEZO_AXES = ("piezo_x", "piezo_y", "piezo_z", "piezo_th", "piezo_chi")
STAGE_AXES = ("stage_x", "stage_y", "stage_z", "stage_theta", "stage_chi", "stage_phi")

#: Default holder name used when the store is empty / a sample is created without one.
DEFAULT_HOLDER_NAME = "default"


class AcquireStore:
    """App-facing wrapper around a :class:`smi_plans.SampleStore`.

    Holds the connection (live redis or offline) and presents holders + samples to the GUI.
    All persistence is the shared db=2 store; this object keeps no sample state of its own.
    """

    def __init__(self, backend: SampleStore, *, live: bool, location: str):
        self.store = backend
        self.live = live              # True = real redis db=2; False = offline dict/JSON
        self.location = location      # human label for the status bar
        self._default_holder_id: Optional[str] = None

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, *, offline: Optional[bool] = None) -> "AcquireStore":
        """Open the sample store.

        Tries the live redis db=2 (``SampleStore.from_redis()``) unless ``offline`` is forced
        (or ``SMI_ACQUIRE_OFFLINE`` is set).  On any connection failure falls back to an
        in-memory store so the app still launches (with a clear "offline" status) — this is a
        dev convenience and never shows live samples.
        """
        if offline is None:
            offline = _env_truthy(os.environ.get("SMI_ACQUIRE_OFFLINE"))
        if not offline:
            try:
                store = SampleStore.from_redis()
                # Touch the backend so a dead connection fails here, not later.
                _ = store.magazine()
                host = os.environ.get(
                    "SMI_ACQUIRE_REDIS_HOST", "xf12id2-smi-redis1")
                return cls(store, live=True, location="redis db=2 @ {}".format(host))
            except Exception as exc:  # noqa: BLE001 (any connection problem -> offline)
                import warnings
                warnings.warn(
                    "live sample store unavailable ({}: {}); running OFFLINE with an in-memory "
                    "store (no live samples).".format(type(exc).__name__, exc),
                    stacklevel=2,
                )
        return cls(SampleStore({}), live=False, location="offline (in-memory)")

    # ------------------------------------------------------------------
    # holders (the app's old "sample sets")
    # ------------------------------------------------------------------
    def list_holders(self) -> List[Holder]:
        return self.store.list_holders()

    def holder_by_id(self, holder_id: str) -> Optional[Holder]:
        try:
            return self.store.get_holder(holder_id)
        except KeyError:
            return None

    def holder_by_name(self, name: str) -> Optional[Holder]:
        return next((h for h in self.list_holders() if h.name == name), None)

    def ensure_holder(self, name: str, *, kind: str = "bar") -> Holder:
        """Return the holder named ``name``, creating it if absent."""
        h = self.holder_by_name(name)
        if h is None:
            h = Holder(name=name, kind=kind)
            self.store.put_holder(h)
            self._register_holder_in_magazine(h)
        return h

    def default_holder(self) -> Holder:
        """The fallback holder new samples land on when none is chosen."""
        if self._default_holder_id is not None:
            h = self.holder_by_id(self._default_holder_id)
            if h is not None:
                return h
        h = self.ensure_holder(DEFAULT_HOLDER_NAME)
        self._default_holder_id = h.id
        return h

    def rename_holder(self, holder_id: str, new_name: str) -> None:
        h = self.holder_by_id(holder_id)
        if h is not None and new_name.strip():
            h.name = new_name.strip()
            self.store.put_holder(h)

    def delete_holder(self, holder_id: str, *, delete_samples: bool = False) -> int:
        """Remove a holder from the magazine, optionally deleting its samples.

        Returns the number of samples deleted.  When ``delete_samples`` is false, samples are
        retained but detached from the holder.
        """
        h = self.holder_by_id(holder_id)
        if h is None:
            return 0
        deleted = 0
        for sid in list(h.sample_ids):
            if delete_samples:
                self.store.delete_sample(sid)
                deleted += 1
            else:
                s = self.sample_by_id(sid)
                if s is not None:
                    s.holder_id = ""
                    self.store.put_sample(s)
        try:
            self.store.prune(holders=[holder_id], require_export=False)
        except AttributeError:
            m = self.store.magazine()
            if holder_id in m.holder_ids:
                m.holder_ids.remove(holder_id)
                self.store._put_magazine(m)  # noqa: SLF001
        return deleted

    def clear_holders(self, *, delete_samples: bool = False) -> int:
        """Remove all holders, optionally deleting samples on them."""
        deleted = 0
        for h in list(self.list_holders()):
            deleted += self.delete_holder(h.id, delete_samples=delete_samples)
        return deleted

    def _register_holder_in_magazine(self, holder: Holder) -> None:
        m = self.store.magazine()
        if holder.id not in m.holder_ids:
            m.holder_ids.append(holder.id)
            self.store._put_magazine(m)  # noqa: SLF001 (facade has no public add-holder yet)

    # ------------------------------------------------------------------
    # samples
    # ------------------------------------------------------------------
    def list_samples(self, holder_id: Optional[str] = None) -> List[Sample]:
        return self.store.list_samples(holder_id=holder_id)

    def sample_by_id(self, sample_id: str) -> Optional[Sample]:
        try:
            return self.store.get_sample(sample_id)
        except KeyError:
            return None

    def add_sample(self, name: str, *, holder_id: Optional[str] = None,
                   nominal: Optional[Position] = None, md: Optional[Dict[str, Any]] = None,
                   incident_angles: Optional[List[float]] = None) -> Sample:
        """Create + persist a new sample on a holder (default holder if none given)."""
        holder = (self.holder_by_id(holder_id) if holder_id else None) or self.default_holder()
        s = Sample(
            name=name,
            holder_id=holder.id,
            nominal=nominal if nominal is not None else Position(frame="holder"),
            incident_angles=list(incident_angles or []),
            md=dict(md or {}),
        )
        self.store.put_sample(s)
        self._add_sample_to_holder(holder, s.id)
        return s

    def update_sample(self, sample: Sample) -> None:
        self.store.put_sample(sample)

    def delete_sample(self, sample_id: str) -> None:
        s = self.sample_by_id(sample_id)
        self.store.delete_sample(sample_id)
        if s is not None and s.holder_id:
            h = self.holder_by_id(s.holder_id)
            if h is not None and sample_id in h.sample_ids:
                h.sample_ids.remove(sample_id)
                self.store.put_holder(h)

    def set_sample_holder(self, sample_id: str, holder_id: str) -> None:
        """Move a sample to a different holder (updates both holders' membership)."""
        s = self.sample_by_id(sample_id)
        if s is None:
            return
        old = self.holder_by_id(s.holder_id) if s.holder_id else None
        new = self.holder_by_id(holder_id)
        if new is None:
            return
        if old is not None and sample_id in old.sample_ids:
            old.sample_ids.remove(sample_id)
            self.store.put_holder(old)
        s.holder_id = new.id
        self.store.put_sample(s)
        self._add_sample_to_holder(new, sample_id)

    def assign_nominal(self, sample_id: str, nominal: Position) -> Optional[Sample]:
        """Set a sample's nominal (holder-frame) position; persist."""
        s = self.sample_by_id(sample_id)
        if s is not None:
            s.nominal = nominal
            self.store.put_sample(s)
        return s

    def adjust_nominal_axis(self, sample_ids: List[str], axis: str, value: float, *, mode: str,
                            clear_refined: bool = False) -> int:
        """Bulk-adjust one nominal Position axis for selected samples.

        ``mode`` is ``"relative"`` (add value) or ``"absolute"`` (set value). Refined positions
        are intentionally not adjusted; callers may clear them explicitly.
        """
        if axis not in PIEZO_AXES + STAGE_AXES:
            raise ValueError("unknown position axis: {}".format(axis))
        n = 0
        for sid in sample_ids:
            s = self.sample_by_id(sid)
            if s is None:
                continue
            cur = getattr(s.nominal, axis)
            if mode == "relative":
                setattr(s.nominal, axis, (float(cur) if cur is not None else 0.0) + float(value))
            elif mode == "absolute":
                setattr(s.nominal, axis, float(value))
            else:
                raise ValueError("mode must be 'relative' or 'absolute'")
            if clear_refined:
                s.refined = None
            self.store.put_sample(s)
            n += 1
        return n

    def _add_sample_to_holder(self, holder: Holder, sample_id: str) -> None:
        if sample_id not in holder.sample_ids:
            holder.sample_ids.append(sample_id)
            self.store.put_holder(holder)

    # ------------------------------------------------------------------
    # the active ("loaded") sample (intent hand-off; D12)
    # ------------------------------------------------------------------
    def active_sample(self) -> Optional[Sample]:
        return self.store.get_active_sample()

    def set_active_sample(self, sample_id: Optional[str]) -> None:
        """Write the active-sample *intent* into the shared store (the GUI never moves motors).

        The beamline session/worker performs the actual ``load_sample`` motion; this is only the
        cross-process hand-off (§9).
        """
        self.store.set_active_sample(sample_id)

    # ------------------------------------------------------------------
    # position capture (microscope reading -> a Position)
    # ------------------------------------------------------------------
    @staticmethod
    def position_from_axes(axes: Dict[str, float], *, frame: str = "holder",
                           default_to_piezo: bool = True) -> Position:
        """Build a :class:`Position` from a dict of live axis readings.

        ``axes`` keys may be the bare microscope axes (``"x"``/``"y"``/``"z"``) and/or the
        fully-qualified ones (``"piezo_x"``, ``"stage_phi"`` …).  Bare x/y/z default onto the
        **piezo** fine stage (``default_to_piezo``), matching the SMI stack where the on-axis
        click-to-move drives the piezo; any fully-qualified keys are recorded verbatim so a
        capture describes *all* axes the microscope exposed.
        """
        pos = Position(frame=frame)
        for key, val in axes.items():
            if val is None:
                continue
            k = str(key)
            if k in PIEZO_AXES or k in STAGE_AXES:
                setattr(pos, k, float(val))
            elif k in ("x", "y", "z", "th", "chi") and default_to_piezo:
                setattr(pos, "piezo_" + k, float(val))
            elif k in ("theta", "phi"):
                setattr(pos, "stage_" + k, float(val))
        return pos


# ---------------------------------------------------------------------------
# sample -> codegen/ExperimentSpec row
# ---------------------------------------------------------------------------
def sample_to_row(sample: Sample) -> Dict[str, Any]:
    """Project a :class:`smi_plans.Sample` into a ``SampleList.from_columns`` row dict.

    Uses the sample's *runnable* position (refined if aligned, else nominal).  Piezo axes map
    to ``piezo_*`` columns and Huber stage axes to ``hexa_*`` columns — the column names
    ``SampleList.from_columns`` / the codegen ``render_samplelist`` still consume (the package
    keeps the ``hexa_*`` constructor kwargs as the coarse-stage columns).  Only set axes are
    emitted; ``incident_angles`` + ``md`` ride along.
    """
    pos = sample.runnable_position()
    row: Dict[str, Any] = {"name": sample.name}
    for short, attr in (("piezo_x", "piezo_x"), ("piezo_y", "piezo_y"),
                        ("piezo_z", "piezo_z"), ("piezo_th", "piezo_th")):
        v = getattr(pos, attr)
        if v is not None:
            row[short] = v
    for col, attr in (("hexa_x", "stage_x"), ("hexa_y", "stage_y"),
                      ("hexa_z", "stage_z"), ("hexa_th", "stage_theta")):
        v = getattr(pos, attr)
        if v is not None:
            row[col] = v
    angles = sample.incident_angles or pos.incident_angles
    if angles:
        row["incident_angles"] = list(angles)
    if sample.md:
        row["md"] = dict(sample.md)
    return row


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _env_truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["AcquireStore", "sample_to_row", "PIEZO_AXES", "STAGE_AXES", "DEFAULT_HOLDER_NAME"]
