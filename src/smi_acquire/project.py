"""
smi_acquire.project
===================

The app's **local** session document: the transient experiment *recipes* the user is composing
and the local reference (fiducial) bookmarks.  The **samples are no longer here** — they live in
the shared redis db=2 store (see :mod:`smi_acquire.store` /
``smi-plans/docs/SAMPLE_SYSTEM_PLAN.md``).  This split mirrors the contract:

* **persistent / shared** (redis): samples, holders, the active-sample pointer, scan history.
* **transient / per-experiment** (this file, saved to a local ``project.json``): the scan
  recipes (beam + apparatus + axes + manual steps + a target), plus local references.

An :class:`Experiment` reuses the pure scan-recipe blocks from :mod:`smi_acquire.spec`
(``BeamSpec`` / ``ApparatusSpec`` / ``AxisSpec`` / ``ManualSetupStep``) and adds a
:class:`Target` (which **holder** — or all samples — it runs on).  ``Experiment.to_spec(samples)``
projects it (plus the resolved sample subset, as ``smi_plans.Sample`` objects) into an
:class:`~smi_acquire.spec.ExperimentSpec`, so the existing codegen / dry-run work unchanged — one
experiment → one generated ``acquire_bar`` plan over its target subset.

Everything here is pure data (JSON-serializable).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .spec import (AxisSpec, ApparatusSpec, BeamSpec, ExperimentSpec, ManualSetupStep,
                   SamplesSpec)
from .store import sample_to_row

PROJECT_VERSION = 2


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _sample_priority(sample) -> int:
    """A sample's run-order priority (lower runs first; default 0).

    Stored on ``Sample.md['priority']`` as a stopgap until ``smi_plans.Sample`` gains a native
    ``priority`` field (which will also make ``load_holder`` order by it). See docs/DESIGN.md.
    """
    try:
        return int((sample.md or {}).get("priority", 0))
    except (TypeError, ValueError):
        return 0


def _sample_project(sample) -> str:
    """A sample's project_name (``Sample.md['project_name']``), or "" if unset.

    Per-sample so project can vary across a bar; carried into each run's md by ``acquire_bar``.
    """
    v = (sample.md or {}).get("project_name")
    return str(v) if v else ""


def _common_holder_name(samples, store):
    """The single holder NAME shared by every sample, or None if they span/lack holders.

    When all samples sit on one holder, codegen can reference it by name (``load_holder``);
    otherwise it falls back to ``from_columns``.  ``store`` is an ``AcquireStore``.
    """
    if not samples:
        return None
    holder_ids = {getattr(s, "holder_id", None) for s in samples}
    if len(holder_ids) != 1:
        return None
    hid = next(iter(holder_ids))
    if not hid:
        return None
    holder = store.holder_by_id(hid)
    return holder.name if holder is not None else None


# ---------------------------------------------------------------------------
# Reference (local fiducial marker)
# ---------------------------------------------------------------------------
@dataclass
class Reference:
    """A named landmark position (fiducial) kept in the local project, not in redis.

    References render on the microscope image but are never measured.  Stored as bare x/y/z
    (the microscope's Cartesian axes) for simplicity.
    """

    name: str
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    id: str = field(default_factory=_new_id)
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Reference":
        d = dict(d)
        d.setdefault("id", _new_id())
        return cls(**d)


# ---------------------------------------------------------------------------
# Experiment target (which holder, or all samples)
# ---------------------------------------------------------------------------
@dataclass
class Target:
    kind: str = "all"                      # "all" | "holder" | "samples"
    holder_id: Optional[str] = None
    sample_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Target":
        d = dict(d or {})
        # Back-compat: the old model targeted sample-"set"s; map set_id -> holder_id.
        if "set_id" in d and "holder_id" not in d:
            d["holder_id"] = d.pop("set_id")
        if d.get("kind") == "set":
            d["kind"] = "holder"
        d.pop("set_id", None)
        return cls(kind=d.get("kind", "all"), holder_id=d.get("holder_id"),
                   sample_ids=list(d.get("sample_ids", []) or []))


# ---------------------------------------------------------------------------
# Experiment (a scan recipe targeting a holder / all samples)
# ---------------------------------------------------------------------------
@dataclass
class Experiment:
    name: str
    id: str = field(default_factory=_new_id)
    scan_name: str = "acquire"
    project_name: str = ""          # -> md={'project_name': ...} on the acquire run(s)
    beam: BeamSpec = field(default_factory=BeamSpec)
    apparatus: ApparatusSpec = field(default_factory=ApparatusSpec)
    axes: List[AxisSpec] = field(default_factory=list)
    manual_setup: List[ManualSetupStep] = field(default_factory=list)
    target: Target = field(default_factory=Target)
    md: Dict[str, Any] = field(default_factory=dict)

    # ---- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "id": self.id, "scan_name": self.scan_name,
            "project_name": self.project_name,
            "beam": asdict(self.beam), "apparatus": asdict(self.apparatus),
            "axes": [asdict(a) for a in self.axes],
            "manual_setup": [asdict(m) for m in self.manual_setup],
            "target": self.target.to_dict(), "md": dict(self.md),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Experiment":
        d = dict(d)
        return cls(
            name=d.get("name", "experiment"),
            id=d.get("id", _new_id()),
            scan_name=d.get("scan_name", "acquire"),
            project_name=d.get("project_name", ""),
            beam=BeamSpec(**d.get("beam", {})) if d.get("beam") else BeamSpec(),
            apparatus=(ApparatusSpec(**d["apparatus"]) if d.get("apparatus")
                       else ApparatusSpec()),
            axes=[AxisSpec(**a) for a in d.get("axes", [])],
            manual_setup=[ManualSetupStep(**m) for m in d.get("manual_setup", [])],
            target=Target.from_dict(d.get("target")),
            md=dict(d.get("md", {})),
        )

    # ---- projection into the codegen/dryrun model -------------------------
    def to_spec(self, samples, *, project_name: str = "",
                motor_object: str = "piezo", holder_name: Optional[str] = None) -> ExperimentSpec:
        """Build an :class:`ExperimentSpec` over a resolved subset of ``smi_plans.Sample``.

        ``samples`` are the (already target-resolved, priority-ordered) redis samples; each
        becomes a ``SampleList.from_columns`` row via :func:`smi_acquire.store.sample_to_row`.

        When every sample belongs to **one** named holder (``holder_name``), the samples block is
        marked ``source="holder"`` so codegen emits ``load_holder(holder_name)`` (Redis-first, no
        copy-paste); otherwise it stays ``"inline"`` (the ``from_columns`` fallback).  Per-sample
        ``project_name`` (from each sample's ``md``) is carried alongside, falling back to the
        experiment's / project's name.  The experiment's own ``project_name`` wins over the
        caller's (the Project name fallback).
        """
        rows = [sample_to_row(s) for s in samples]
        default_project = self.project_name or project_name
        project_names = [_sample_project(s) or default_project for s in samples]
        source = "holder" if (holder_name and samples) else "inline"
        return ExperimentSpec(
            project_name=default_project,
            scan_name=self.scan_name, md=dict(self.md),
            beam=self.beam, apparatus=self.apparatus, axes=self.axes,
            manual_setup=self.manual_setup,
            samples=SamplesSpec(rows=rows, motor_object=motor_object,
                                source=source, holder=holder_name if source == "holder" else None,
                                project_names=project_names),
        )

    @classmethod
    def from_spec(cls, spec: ExperimentSpec, *, name: str = "experiment",
                  target: Optional[Target] = None) -> "Experiment":
        """Adopt a scan recipe authored as an ExperimentSpec (e.g. the interrogation seed)."""
        return cls(
            name=name, scan_name=spec.scan_name, project_name=spec.project_name,
            beam=spec.beam, apparatus=spec.apparatus,
            axes=list(spec.axes), manual_setup=list(spec.manual_setup),
            target=target or Target(), md=dict(spec.md),
        )


# ---------------------------------------------------------------------------
# Project (the local session document: recipes + references)
# ---------------------------------------------------------------------------
@dataclass
class Project:
    version: int = PROJECT_VERSION
    name: str = ""
    motor_object: str = "piezo"
    references: List[Reference] = field(default_factory=list)
    experiments: List[Experiment] = field(default_factory=list)

    # ---- lookups ----------------------------------------------------------
    def experiment_by_id(self, eid: str) -> Optional[Experiment]:
        return next((e for e in self.experiments if e.id == eid), None)

    # ---- target resolution (against a live AcquireStore) ------------------
    def resolve_target(self, experiment: Experiment, store) -> List[Any]:
        """The sample subset an experiment runs on, read from the shared sample ``store``.

        ``store`` is an :class:`smi_acquire.store.AcquireStore`.  Returns ``smi_plans.Sample``
        objects **sorted by run-order priority** (lower first) so the generated bar runs them in
        the same order the GUI's sample list shows.  Only positioned samples are truly measurable,
        but (matching the old behavior) unpositioned ones are returned too so the GUI can warn.
        """
        t = experiment.target
        if t.kind == "holder" and t.holder_id:
            samples = store.list_samples(holder_id=t.holder_id)
        elif t.kind == "samples":
            ids = set(t.sample_ids)
            samples = [s for s in store.list_samples() if s.id in ids]
        else:
            samples = store.list_samples()
        return sorted(samples, key=_sample_priority)

    # ---- codegen / validation per experiment ------------------------------
    def experiment_spec(self, experiment: Experiment, store) -> ExperimentSpec:
        samples = self.resolve_target(experiment, store)
        holder_name = _common_holder_name(samples, store)
        return experiment.to_spec(samples, project_name=self.name,
                                  motor_object=self.motor_object, holder_name=holder_name)

    # ---- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version, "name": self.name, "motor_object": self.motor_object,
            "references": [r.to_dict() for r in self.references],
            "experiments": [e.to_dict() for e in self.experiments],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Project":
        d = dict(d or {})
        return cls(
            version=d.get("version", PROJECT_VERSION),
            name=d.get("name", ""),
            motor_object=d.get("motor_object", "piezo"),
            references=[Reference.from_dict(x) for x in d.get("references", [])],
            experiments=[Experiment.from_dict(x) for x in d.get("experiments", [])],
        )


__all__ = [
    "PROJECT_VERSION", "Reference", "Target", "Experiment", "Project",
]
