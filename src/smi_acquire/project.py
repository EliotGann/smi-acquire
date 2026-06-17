"""
smi_acquire.project
===================

The **Project** — the persistent spine of the app. The sample list is the centre of gravity;
everything else (alignment, the microscope, scan recipes) orbits it.

Model
-----
::

    Project
    ├── samples      : [Sample]      ← THE SPINE (each may carry a position + free metadata)
    ├── sample_sets  : [SampleSet]   ← named, colored groups (subsets) of samples
    ├── references   : [Bookmark]    ← fiducials / landmarks (never measured)
    └── experiments  : [Experiment]  ← scan RECIPES (beam+apparatus+axes), each targeting a set

Bookmarks vs samples (per the agreed UX)
----------------------------------------
A **Sample** with a ``position`` that is *visible* (itself or via its set) shows on the image as
a marker — i.e. the sample *is* its own bookmark. Separately, a :class:`Bookmark` is a named
position that is a **reference** (fiducial) or a **transient** scratch point; a transient
bookmark can be *assigned* to a sample (copying its position) or *promoted* to a new sample.
The persistent project stores samples, sets, references, and experiments; transient bookmarks
are live microscope state that resolve into samples via :meth:`Project.new_sample_from` /
:meth:`Project.assign_position`.

An :class:`Experiment` reuses the pure scan-recipe blocks from :mod:`smi_acquire.spec`
(``BeamSpec`` / ``ApparatusSpec`` / ``AxisSpec`` / ``ManualSetupStep``) and adds a
:class:`Target`. ``Experiment.to_spec(samples)`` projects it (plus the resolved sample subset)
back into an :class:`~smi_acquire.spec.ExperimentSpec`, so the existing codegen / dry-run work
unchanged — one experiment → one generated ``acquire_bar`` plan over its target subset.

Everything here is pure data (JSON-serializable); ``pandas`` is imported lazily only for the
spreadsheet round-trip.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .spec import (AxisSpec, ApparatusSpec, BeamSpec, ExperimentSpec, ManualSetupStep,
                   SamplesSpec)

PROJECT_VERSION = 1


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------
@dataclass
class Position:
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    th: Optional[float] = None

    def is_set(self) -> bool:
        return any(v is not None for v in (self.x, self.y, self.z))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Position":
        return cls(**(d or {}))


# ---------------------------------------------------------------------------
# Sample (the spine)
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    name: str
    id: str = field(default_factory=_new_id)
    position: Position = field(default_factory=Position)
    incident_angles: List[float] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    set_ids: List[str] = field(default_factory=list)
    visible: bool = True

    def has_position(self) -> bool:
        return self.position.is_set()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["position"] = self.position.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Sample":
        d = dict(d)
        d["position"] = Position.from_dict(d.get("position"))
        d.setdefault("id", _new_id())
        return cls(**d)

    def row(self, motor_object: str = "piezo") -> Dict[str, Any]:
        """As a SampleList.from_columns / ExperimentSpec row (position → motor columns)."""
        r: Dict[str, Any] = {"name": self.name}
        p = self.position
        if p.x is not None:
            r["{}_x".format(motor_object)] = p.x
        if p.y is not None:
            r["{}_y".format(motor_object)] = p.y
        if p.z is not None:
            r["{}_z".format(motor_object)] = p.z
        if p.th is not None:
            r["{}_th".format(motor_object)] = p.th
        if self.incident_angles:
            r["incident_angles"] = list(self.incident_angles)
        if self.metadata:
            r["md"] = dict(self.metadata)
        return r


# ---------------------------------------------------------------------------
# SampleSet
# ---------------------------------------------------------------------------
@dataclass
class SampleSet:
    name: str
    id: str = field(default_factory=_new_id)
    color: str = "#2ecc71"
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SampleSet":
        d = dict(d)
        d.setdefault("id", _new_id())
        return cls(**d)


# ---------------------------------------------------------------------------
# Bookmark (reference / transient marker, optionally tied to a sample)
# ---------------------------------------------------------------------------
@dataclass
class Bookmark:
    name: str
    position: Position = field(default_factory=Position)
    id: str = field(default_factory=_new_id)
    kind: str = "reference"               # "reference" | "transient"
    sample_id: Optional[str] = None       # set if assigned to / promoted from a sample
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["position"] = self.position.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bookmark":
        d = dict(d)
        d["position"] = Position.from_dict(d.get("position"))
        d.setdefault("id", _new_id())
        return cls(**d)


# ---------------------------------------------------------------------------
# Experiment (a scan recipe targeting a sample-set)
# ---------------------------------------------------------------------------
@dataclass
class Target:
    kind: str = "all"                      # "all" | "set" | "samples"
    set_id: Optional[str] = None
    sample_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Target":
        return cls(**(d or {}))


@dataclass
class Experiment:
    name: str
    id: str = field(default_factory=_new_id)
    scan_name: str = "acquire"
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
            beam=BeamSpec(**d.get("beam", {})) if d.get("beam") else BeamSpec(),
            apparatus=(ApparatusSpec(**d["apparatus"]) if d.get("apparatus")
                       else ApparatusSpec()),
            axes=[AxisSpec(**a) for a in d.get("axes", [])],
            manual_setup=[ManualSetupStep(**m) for m in d.get("manual_setup", [])],
            target=Target.from_dict(d.get("target")),
            md=dict(d.get("md", {})),
        )

    # ---- projection into the codegen/dryrun model -------------------------
    def to_spec(self, samples: List[Sample], *, project_name: str = "",
                motor_object: str = "piezo") -> ExperimentSpec:
        """Build an :class:`ExperimentSpec` for this recipe over a resolved sample subset."""
        rows = [s.row(motor_object) for s in samples]
        return ExperimentSpec(
            project_name=project_name, scan_name=self.scan_name, md=dict(self.md),
            beam=self.beam, apparatus=self.apparatus, axes=self.axes,
            manual_setup=self.manual_setup,
            samples=SamplesSpec(rows=rows, motor_object=motor_object),
        )

    @classmethod
    def from_spec(cls, spec: ExperimentSpec, *, name: str = "experiment",
                  target: Optional[Target] = None) -> "Experiment":
        """Adopt a scan recipe authored as an ExperimentSpec (e.g. the interrogation seed)."""
        return cls(
            name=name, scan_name=spec.scan_name, beam=spec.beam, apparatus=spec.apparatus,
            axes=list(spec.axes), manual_setup=list(spec.manual_setup),
            target=target or Target(), md=dict(spec.md),
        )


# ---------------------------------------------------------------------------
# Project (the top-level document)
# ---------------------------------------------------------------------------
@dataclass
class Project:
    version: int = PROJECT_VERSION
    name: str = ""
    motor_object: str = "piezo"
    samples: List[Sample] = field(default_factory=list)
    sample_sets: List[SampleSet] = field(default_factory=list)
    references: List[Bookmark] = field(default_factory=list)
    experiments: List[Experiment] = field(default_factory=list)

    # ---- lookups ----------------------------------------------------------
    def sample_by_id(self, sid: str) -> Optional[Sample]:
        return next((s for s in self.samples if s.id == sid), None)

    def set_by_id(self, set_id: str) -> Optional[SampleSet]:
        return next((g for g in self.sample_sets if g.id == set_id), None)

    def set_by_name(self, name: str) -> Optional[SampleSet]:
        return next((g for g in self.sample_sets if g.name == name), None)

    def samples_in_set(self, set_id: str) -> List[Sample]:
        return [s for s in self.samples if set_id in s.set_ids]

    # ---- mutations (the sample-building flow) -----------------------------
    def ensure_set(self, name: str, *, color: str = "#2ecc71") -> SampleSet:
        g = self.set_by_name(name)
        if g is None:
            g = SampleSet(name=name, color=color)
            self.sample_sets.append(g)
        return g

    def new_sample_from(self, name: str, position: Position, *,
                        set_ids: Optional[List[str]] = None, **metadata) -> Sample:
        """Create a Sample at ``position`` (the 'New sample here' action)."""
        s = Sample(name=name, position=position, set_ids=list(set_ids or []),
                   metadata=dict(metadata))
        self.samples.append(s)
        return s

    def assign_position(self, sample_id: str, position: Position) -> Optional[Sample]:
        """Assign a (bookmarked) position to an existing sample."""
        s = self.sample_by_id(sample_id)
        if s is not None:
            s.position = position
        return s

    def visible_markers(self) -> List[Bookmark]:
        """Everything that should render on the microscope image right now.

        = visible references + (visible samples-with-position whose set is visible). Sample
        markers are returned as synthetic ``kind='sample'`` bookmarks tied by ``sample_id``.
        """
        markers: List[Bookmark] = [b for b in self.references if b.visible]
        hidden_sets = {g.id for g in self.sample_sets if not g.visible}
        for s in self.samples:
            if not (s.visible and s.has_position()):
                continue
            if s.set_ids and all(sid in hidden_sets for sid in s.set_ids):
                continue  # all of this sample's sets are hidden
            markers.append(Bookmark(name=s.name, position=s.position, id=s.id,
                                    kind="sample", sample_id=s.id, visible=True))
        return markers

    # ---- target resolution ------------------------------------------------
    def resolve_target(self, experiment: Experiment) -> List[Sample]:
        """The sample subset an experiment runs on (only positioned samples are measurable)."""
        t = experiment.target
        if t.kind == "set" and t.set_id:
            cand = self.samples_in_set(t.set_id)
        elif t.kind == "samples":
            ids = set(t.sample_ids)
            cand = [s for s in self.samples if s.id in ids]
        else:
            cand = list(self.samples)
        return cand

    # ---- codegen / validation per experiment ------------------------------
    def experiment_spec(self, experiment: Experiment) -> ExperimentSpec:
        return experiment.to_spec(self.resolve_target(experiment),
                                  project_name=self.name, motor_object=self.motor_object)

    # ---- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version, "name": self.name, "motor_object": self.motor_object,
            "samples": [s.to_dict() for s in self.samples],
            "sample_sets": [g.to_dict() for g in self.sample_sets],
            "references": [b.to_dict() for b in self.references],
            "experiments": [e.to_dict() for e in self.experiments],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Project":
        d = dict(d or {})
        return cls(
            version=d.get("version", PROJECT_VERSION),
            name=d.get("name", ""),
            motor_object=d.get("motor_object", "piezo"),
            samples=[Sample.from_dict(x) for x in d.get("samples", [])],
            sample_sets=[SampleSet.from_dict(x) for x in d.get("sample_sets", [])],
            references=[Bookmark.from_dict(x) for x in d.get("references", [])],
            experiments=[Experiment.from_dict(x) for x in d.get("experiments", [])],
        )

    # ---- spreadsheet round-trip (samples-centric) -------------------------
    def to_dataframe(self):
        """Samples as a flat spreadsheet: position + metadata + set + experiments columns.

        The full experiment *recipes* live in the JSON; the ``experiments`` column only names
        which experiments target each sample so a re-imported sheet can re-link them.
        """
        import pandas as pd

        set_name = {g.id: g.name for g in self.sample_sets}
        # which experiments target each sample (by resolving each experiment's target)
        exp_for: Dict[str, List[str]] = {s.id: [] for s in self.samples}
        for e in self.experiments:
            for s in self.resolve_target(e):
                exp_for.setdefault(s.id, []).append(e.name)

        meta_keys: List[str] = []
        for s in self.samples:
            for k in s.metadata:
                if k not in meta_keys:
                    meta_keys.append(k)

        rows = []
        for s in self.samples:
            r = {
                "name": s.name,
                "x": s.position.x, "y": s.position.y, "z": s.position.z, "th": s.position.th,
                "incident_angles": " ".join(str(a) for a in s.incident_angles),
                "sample_sets": ", ".join(set_name.get(g, g) for g in s.set_ids),
                "experiments": ", ".join(exp_for.get(s.id, [])),
            }
            for k in meta_keys:
                r["md.{}".format(k)] = s.metadata.get(k)
            rows.append(r)
        cols = (["name", "x", "y", "z", "th", "incident_angles", "sample_sets", "experiments"]
                + ["md.{}".format(k) for k in meta_keys])
        return pd.DataFrame(rows, columns=cols)

    @classmethod
    def from_dataframe(cls, df, *, name: str = "") -> "Project":
        """Import a samples spreadsheet (positions optional; align later in the microscope)."""
        proj = cls(name=name)

        def _f(v):
            try:
                if v is None or (isinstance(v, float) and v != v) or str(v).strip() == "":
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        for _, row in df.iterrows():
            d = row.to_dict()
            sample_name = str(d.get("name") or "").strip()
            if not sample_name:
                continue
            pos = Position(_f(d.get("x")), _f(d.get("y")), _f(d.get("z")), _f(d.get("th")))
            ia = []
            raw_ia = d.get("incident_angles")
            if raw_ia is not None and str(raw_ia).strip() and str(raw_ia) != "nan":
                ia = [float(t) for t in str(raw_ia).replace(",", " ").split()
                      if t.replace(".", "", 1).replace("-", "", 1).isdigit()]
            md = {}
            for k, v in d.items():
                if isinstance(k, str) and k.startswith("md.") and v is not None and str(v) != "nan":
                    md[k[3:]] = v
            set_ids = []
            raw_sets = d.get("sample_sets")
            if raw_sets and str(raw_sets).strip() and str(raw_sets) != "nan":
                for sn in str(raw_sets).split(","):
                    sn = sn.strip()
                    if sn:
                        set_ids.append(proj.ensure_set(sn).id)
            proj.samples.append(Sample(name=sample_name, position=pos, incident_angles=ia,
                                       metadata=md, set_ids=set_ids))
        return proj


__all__ = [
    "PROJECT_VERSION", "Position", "Sample", "SampleSet", "Bookmark",
    "Target", "Experiment", "Project",
]
