"""
smi_acquire.wizard
==================

The **scan-building wizard** model — the headless logic behind the visual, iconic
"what do you want to *measure*, and what do you want to *change*?" experience.

An SMI scan is a **measurement core** (geometry + q-range/detectors + exposure) wrapped by a
**stack of nested "things you change"** (the scan axes, outermost = slowest).  The wizard walks
the experimenter through five steps and assembles an :class:`~smi_acquire.spec.ExperimentSpec`:

1. **Measure** — how the beam hits the sample, which detectors / q-range, exposure.
2. **Change** — pick which quantities vary (the iconic cards: energy, temperature, incidence,
   spatial, time, potential, rh, manual).  Each becomes a nested scan axis.
3. **Configure** — set each chosen change's parameters (values, soak, grid, …).
4. **Compose** — order/nest the changes (the nested-box canvas) and target a holder.
5. **Review** — generated script + dry-run + submit.

This module is **pure data + small functions** (no Panel, no bluesky), so the GUI is a thin
renderer and every transition is unit-testable.  It reuses :mod:`smi_acquire.interview`
(``default_axis``/``reorder_axes_by_speed``/``axis_param_schema``) and the
:mod:`smi_acquire.registry` catalog (now carrying icons + colors).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import interview, registry
from .project import Experiment, Target
from .spec import AxisSpec, BeamSpec, ApparatusSpec, ExperimentSpec


# ---------------------------------------------------------------------------
# the five steps
# ---------------------------------------------------------------------------
STEPS = ["measure", "change", "configure", "compose", "review"]
STEP_TITLES = {
    "measure": "What to measure",
    "change": "What to change",
    "configure": "Configure each change",
    "compose": "Compose & target",
    "review": "Review & run",
}
STEP_ICONS = {
    "measure": "🔬", "change": "🎛️", "configure": "🎚️", "compose": "🧩", "review": "🚀",
}


# ---------------------------------------------------------------------------
# the measurables (the "what to measure" iconic choices)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Geometry:
    value: str
    label: str
    icon: str
    blurb: str


GEOMETRIES: List[Geometry] = [
    Geometry("transmission", "Transmission", "➡️",
             "Beam through the film/solution (SAXS/WAXS in transmission)."),
    Geometry("reflection", "Grazing incidence", "📐",
             "GISAXS / GIWAXS — unlocks incidence-angle scanning + alignment."),
]


@dataclass(frozen=True)
class QRange:
    value: str
    label: str
    icon: str
    detectors: List[str]
    blurb: str


Q_RANGES: List[QRange] = [
    QRange("both", "SAXS + WAXS", "🔭", ["pil2M", "pil900KW"],
           "Both detectors, arc-aware (SAXS dropped when the WAXS arc is parked)."),
    QRange("saxs", "SAXS only", "🟦", ["pil2M"], "Large-area SAXS (Pilatus 2M)."),
    QRange("waxs", "WAXS only", "🟧", ["pil900KW"], "WAXS on the arc (Pilatus 900kW)."),
]

GEOMETRY_BY_VALUE = {g.value: g for g in GEOMETRIES}
QRANGE_BY_VALUE = {q.value: q for q in Q_RANGES}


def axis_kind(axis_type: str) -> Optional[registry.AxisKind]:
    return registry.AXIS_KIND_BY_TYPE.get(axis_type)


def changeables() -> List[registry.AxisKind]:
    """The iconic "what to change" cards (the scan-axis kinds), in catalog order."""
    return list(registry.AXIS_KINDS)


# ---------------------------------------------------------------------------
# the wizard state
# ---------------------------------------------------------------------------
@dataclass
class WizardState:
    """The mutable working state the wizard UI edits; produces an Experiment/ExperimentSpec.

    Holds the *measure* selections, the chosen *change* axis types (kept as a live list of
    :class:`AxisSpec` so per-axis config + ordering survive across steps), the target, and the
    current step index.  Designed so the UI can rebuild itself purely from this.
    """

    # navigation
    step_index: int = 0

    # step 1 — measure
    geometry: str = "transmission"
    q: str = "both"
    arc_aware: bool = True
    exposure_s: float = 1.0
    reads: List[str] = field(default_factory=lambda: ["energy", "waxs", "xbpm2", "xbpm3"])
    project_name: str = ""
    scan_name: str = ""

    # step 1 — apparatus (filled when relevant)
    align_routine: Optional[str] = None
    align_angle: float = 0.1
    heater: Optional[str] = None
    attenuators_in: List[str] = field(default_factory=list)

    # step 2/3 — the changes (nested scan axes, outermost first)
    axes: List[AxisSpec] = field(default_factory=list)

    # step 4 — target
    target_kind: str = "all"            # "all" | "holder"
    target_holder_id: Optional[str] = None

    # ---- navigation -------------------------------------------------------
    @property
    def step(self) -> str:
        return STEPS[self.step_index]

    def can_advance(self) -> bool:
        """Whether the current step is satisfied enough to go Next."""
        s = self.step
        if s == "measure":
            return bool(self.geometry and self.q)
        if s == "change":
            return True                 # zero changes is valid (a single point per sample)
        if s == "configure":
            return all(a.n_points() >= 1 for a in self.axes)
        return True

    def goto(self, index: int) -> None:
        self.step_index = max(0, min(len(STEPS) - 1, index))

    def next(self) -> None:
        if self.step_index < len(STEPS) - 1 and self.can_advance():
            self.step_index += 1

    def back(self) -> None:
        if self.step_index > 0:
            self.step_index -= 1

    # ---- step 2: toggle a change on/off -----------------------------------
    def has_change(self, axis_type: str) -> bool:
        return any(a.type == axis_type for a in self.axes)

    def add_change(self, axis_type: str, *, shape: str = "spot") -> AxisSpec:
        """Add a change (scan axis) with sensible defaults; keep the stack slow-outermost."""
        ax = interview.default_axis(axis_type, shape=shape)
        self.axes.append(ax)
        self.axes = interview.reorder_axes_by_speed(self.axes)
        # Apparatus implications.
        if axis_type == "temperature" and not self.heater:
            self.heater = "linkam"
        return ax

    def remove_change(self, axis_type: str) -> None:
        self.axes = [a for a in self.axes if a.type != axis_type]
        if axis_type == "temperature":
            self.heater = None

    def toggle_change(self, axis_type: str) -> bool:
        """Flip a change on/off; return the new state (True = present)."""
        if self.has_change(axis_type):
            self.remove_change(axis_type)
            return False
        self.add_change(axis_type)
        return True

    # ---- step 4: reorder the nesting --------------------------------------
    def move_axis(self, index: int, delta: int) -> None:
        j = index + delta
        if 0 <= index < len(self.axes) and 0 <= j < len(self.axes):
            self.axes[index], self.axes[j] = self.axes[j], self.axes[index]

    def auto_order(self) -> None:
        self.axes = interview.reorder_axes_by_speed(self.axes)

    # ---- derived: the spec/experiment -------------------------------------
    def beam_spec(self) -> BeamSpec:
        qr = QRANGE_BY_VALUE.get(self.q)
        dets = list(qr.detectors) if qr else ["pil2M", "pil900KW"]
        reads = list(self.reads)
        if self.q == "saxs":
            reads = ["energy", "xbpm2", "xbpm3", "pin_diode"]
        elif self.q == "waxs":
            reads = ["energy", "waxs", "xbpm2", "xbpm3"]
        return BeamSpec(detectors=dets, arc_aware=(self.q == "both"),
                        reads=reads, exposure_s=float(self.exposure_s))

    def apparatus_spec(self) -> ApparatusSpec:
        ap = ApparatusSpec(geometry=self.geometry)
        if self.geometry == "reflection" and self.align_routine:
            ap.align_routine = self.align_routine
            ap.align_angle = float(self.align_angle)
        ap.heater = self.heater
        ap.attenuators_in = list(self.attenuators_in)
        return ap

    def suggested_scan_name(self) -> str:
        if self.scan_name:
            return self.scan_name
        return interview._suggest_scan_name(self.geometry, [a.type for a in self.axes])

    def to_spec(self) -> ExperimentSpec:
        return ExperimentSpec(
            project_name=self.project_name,
            scan_name=self.suggested_scan_name(),
            beam=self.beam_spec(),
            apparatus=self.apparatus_spec(),
            axes=list(self.axes),
        )

    def target(self) -> Target:
        if self.target_kind == "holder" and self.target_holder_id:
            return Target(kind="holder", holder_id=self.target_holder_id)
        return Target(kind="all")

    def to_experiment(self, *, name: Optional[str] = None) -> Experiment:
        spec = self.to_spec()
        exp = Experiment.from_spec(spec, name=name or self.suggested_scan_name(),
                                   target=self.target())
        return exp

    def apply_to_experiment(self, exp: Experiment) -> None:
        """Write the wizard's recipe onto an existing Experiment (preserving its id/name)."""
        spec = self.to_spec()
        exp.beam = spec.beam
        exp.apparatus = spec.apparatus
        exp.axes = list(spec.axes)
        exp.scan_name = spec.scan_name
        exp.target = self.target()

    # ---- construction from an existing Experiment (edit mode) -------------
    @classmethod
    def from_experiment(cls, exp: Experiment) -> "WizardState":
        q = _q_from_beam(exp.beam)
        st = cls(
            geometry=exp.apparatus.geometry,
            q=q,
            arc_aware=exp.beam.arc_aware,
            exposure_s=exp.beam.exposure_s,
            reads=list(exp.beam.reads),
            scan_name=exp.scan_name,
            align_routine=exp.apparatus.align_routine,
            align_angle=exp.apparatus.align_angle,
            heater=exp.apparatus.heater,
            attenuators_in=list(exp.apparatus.attenuators_in),
            axes=[AxisSpec(type=a.type, params=dict(a.params)) for a in exp.axes],
            target_kind=exp.target.kind if exp.target.kind in ("all", "holder") else "all",
            target_holder_id=exp.target.holder_id,
        )
        return st

    # ---- guardrails (surface in the UI) -----------------------------------
    def order_warnings(self) -> List[str]:
        return self.to_spec().order_warnings()

    def events_per_sample(self) -> int:
        return self.to_spec().events_per_sample()


def _q_from_beam(beam: BeamSpec) -> str:
    dets = set(beam.detectors)
    if dets == {"pil2M"}:
        return "saxs"
    if dets == {"pil900KW"}:
        return "waxs"
    return "both"


__all__ = [
    "STEPS", "STEP_TITLES", "STEP_ICONS",
    "Geometry", "GEOMETRIES", "GEOMETRY_BY_VALUE",
    "QRange", "Q_RANGES", "QRANGE_BY_VALUE",
    "axis_kind", "changeables", "WizardState",
]
