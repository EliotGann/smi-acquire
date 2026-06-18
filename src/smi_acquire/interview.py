"""
smi_acquire.interview
=====================

The **interrogation** — the rethink's centrepiece. Instead of "pick one of A–O", we ask the
experimenter a sequence of plain-language questions and *assemble a bespoke plan* from their
answers: which detectors, what geometry/environment, what they are varying (one or several
nested things), whether samples are swapped by hand, and so on. The result is an
:class:`~smi_acquire.spec.ExperimentSpec` with a correctly-ordered stack of scan axes that the
user then fine-tunes per concern.

This module is pure, declarative data + small pure functions, so the Panel app is a thin
renderer and the logic is unit-testable headlessly:

* :data:`INTAKE` — the branching question graph (the interrogation itself).
* :func:`seed_spec_from_intake` — answers → a starting ExperimentSpec (axes pre-stacked
  slow-outermost, apparatus/beam pre-filled).
* :func:`axis_param_schema` / :func:`default_axis` — per-axis editors + sensible defaults so
  each concern the interrogation added can be refined.
* :func:`reorder_axes_by_speed` — keep the guardrail happy (slow axes outermost).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .spec import (AxisSpec, ApparatusSpec, BeamSpec, ExperimentSpec, ManualSetupStep,
                   SamplesSpec, SPEED_SLOW)


# ---------------------------------------------------------------------------
# Question model (rendered generically by the GUI)
# ---------------------------------------------------------------------------
@dataclass
class Question:
    key: str
    prompt: str
    kind: str                                  # "choice" | "multichoice" | "bool" | "text"
    options: List[Any] = field(default_factory=list)   # [(value, label), ...] for (multi)choice
    default: Any = None
    help: str = ""
    when: Optional[Callable[[Dict[str, Any]], bool]] = None   # show only if predicate(answers)

    def visible(self, answers: Dict[str, Any]) -> bool:
        return self.when is None or bool(self.when(answers))


def _varying(a: Dict[str, Any], kind: str) -> bool:
    return kind in (a.get("varying") or [])


# ---------------------------------------------------------------------------
# THE INTERROGATION — branching question graph
# ---------------------------------------------------------------------------
INTAKE: List[Question] = [
    Question(
        "project_name", "What is this beamtime / project?", "text",
        help="Goes into run metadata and seeds filenames, e.g. 311234_Doe.",
    ),
    Question(
        "geometry", "How does the beam hit the sample?", "choice",
        options=[("transmission", "Transmission (through the film/solution)"),
                 ("reflection", "Grazing incidence / reflection (GISAXS/GIWAXS)")],
        default="transmission",
        help="Reflection unlocks grazing-incidence-angle scanning and alignment.",
    ),
    Question(
        "q", "Which q-range / detectors do you need?", "choice",
        options=[("both", "SAXS + WAXS (arc-aware)"),
                 ("saxs", "SAXS only (Pilatus 2M)"),
                 ("waxs", "WAXS only (arc)")],
        default="both",
    ),
    Question(
        "varying", "What will you VARY during the measurement? (pick all that apply)",
        "multichoice",
        options=[("energy", "X-ray energy (NEXAFS / across an edge)"),
                 ("temperature", "Temperature (heater ramp)"),
                 ("incidence", "Grazing incidence angle"),
                 ("potential", "Applied potential (electrochemistry)"),
                 ("rh", "Relative humidity"),
                 ("time", "Time (kinetics / in-situ)"),
                 ("spatial", "Position (map / fresh spots / microfocus raster)")],
        default=[],
        help="Each one becomes a nested scan axis; we order them slow-outermost for you.",
    ),
    # environment follow-ups
    Question(
        "heater", "Which heater?", "choice",
        options=[("linkam", "Linkam hot/cold stage"), ("lakeshore", "Lakeshore cryo")],
        default="linkam", when=lambda a: _varying(a, "temperature"),
    ),
    Question(
        "incidence_when_reflection", "Note", "text",
        help="Grazing incidence requires reflection geometry — set above.",
        when=lambda a: _varying(a, "incidence") and a.get("geometry") != "reflection",
    ),
    Question(
        "align", "Run grazing-incidence alignment in setup?", "bool", default=True,
        when=lambda a: a.get("geometry") == "reflection",
    ),
    Question(
        "spatial_shape", "What spatial pattern?", "choice",
        options=[("spot", "A few fresh spots (dose spreading)"),
                 ("line", "A line"),
                 ("grid", "A raster grid (microfocus map)")],
        default="spot", when=lambda a: _varying(a, "spatial"),
    ),
    # samples
    Question(
        "sample_mode", "How are samples handled?", "choice",
        options=[("bar", "A bar of samples at known positions (one run each)"),
                 ("single", "A single sample / spot"),
                 ("manual", "I swap samples by hand as we go")],
        default="bar",
        help="Bar positions can be built interactively in the Samples tab (on-axis microscope).",
    ),
    Question(
        "manual_thickness", "Capture a typed value per manual swap (e.g. thickness)?",
        "bool", default=True, when=lambda a: a.get("sample_mode") == "manual",
    ),
]


def visible_questions(answers: Dict[str, Any]) -> List[Question]:
    return [q for q in INTAKE if q.visible(answers)]


# ---------------------------------------------------------------------------
# answers → a starting ExperimentSpec
# ---------------------------------------------------------------------------
def seed_spec_from_intake(answers: Dict[str, Any],
                          sample_rows: Optional[List[Dict[str, Any]]] = None) -> ExperimentSpec:
    """Assemble a tailored starting spec from the interrogation answers.

    Axes are created with sensible defaults and ordered slow-outermost. The user refines each
    concern afterwards; this is the scaffold the interrogation builds *for* them.
    """
    a = dict(answers or {})
    geometry = a.get("geometry", "transmission")
    q = a.get("q", "both")
    varying = list(a.get("varying") or [])

    beam = BeamSpec(arc_aware=(q == "both"))
    if q == "saxs":
        beam.detectors = ["pil2M"]
        beam.reads = ["energy", "xbpm2", "xbpm3", "pin_diode"]
    elif q == "waxs":
        beam.detectors = ["pil900KW"]
        beam.reads = ["energy", "waxs", "xbpm2", "xbpm3"]

    appa = ApparatusSpec(geometry=geometry)
    if geometry == "reflection" and a.get("align", True):
        appa.align_routine = "alignement_gisaxs_hex"
    if "temperature" in varying:
        appa.heater = a.get("heater", "linkam")

    # Build axes, then sort slow-outermost.
    axes: List[AxisSpec] = []
    for v in varying:
        if v == "spatial":
            axes.append(default_axis("spatial", shape=a.get("spatial_shape", "spot")))
        else:
            axes.append(default_axis(v))
    axes = reorder_axes_by_speed(axes)

    # samples
    mode = a.get("sample_mode", "bar")
    samples = SamplesSpec(rows=list(sample_rows or []))
    manual_setup = []
    scan_name = _suggest_scan_name(geometry, varying)

    spec = ExperimentSpec(
        project_name=a.get("project_name", ""),
        scan_name=scan_name,
        beam=beam, apparatus=appa, axes=axes, samples=samples,
        manual_setup=manual_setup,
    )
    if mode == "manual" and a.get("manual_thickness", True):
        spec.manual_setup.append(ManualSetupStep(
            prompt="Load the next sample; read the prep sheet",
            values=[{"name": "thickness_nm", "cast": "float"}]))
    return spec


def _suggest_scan_name(geometry: str, varying: List[str]) -> str:
    base = "giwaxs" if geometry == "reflection" else "saxs"
    tags = []
    if "temperature" in varying:
        tags.append("Tramp")
    if "energy" in varying:
        tags.append("NEXAFS")
    if "time" in varying:
        tags.append("kinetics")
    if "spatial" in varying:
        tags.append("map")
    return "_".join([base] + tags) if tags else base


# ---------------------------------------------------------------------------
# per-axis editors + defaults (refine the concerns the interrogation added)
# ---------------------------------------------------------------------------
@dataclass
class Field:
    key: str            # dotted into AxisSpec.params (supports "grid.edge")
    label: str
    kind: str           # "float" | "int" | "bool" | "floatlist" | "text"
    default: Any = None
    help: str = ""


def axis_param_schema(axis_type: str) -> List[Field]:
    if axis_type == "energy":
        return [
            Field("grid.edge", "Edge energy (eV)", "float", 2472.0),
            Field("grid.near", "Near-edge [lo, hi, step] rel. to edge", "floatlist", [-2, 2, 0.25]),
            Field("grid.post", "Post-edge [lo, hi, step] rel. to edge", "floatlist", [2, 60, 5]),
            Field("settle", "Settle after move (s)", "float", 2.0),
            Field("flux_reseek", "Re-seek beam if I0 drops", "bool", False),
        ]
    if axis_type == "temperature":
        return [
            Field("values", "Setpoints (degC)", "floatlist", [30, 60, 90]),
            Field("soak", "Soak at each (s)", "float", 120.0),
            Field("first_soak", "First-point soak (s)", "float", 300.0),
        ]
    if axis_type == "incidence":
        return [Field("values", "Incident angles (deg, rel. to aligned 0)", "floatlist", [0.1, 0.2])]
    if axis_type == "motor":
        return [
            Field("name", "Axis label", "text", "arc"),
            Field("device", "Device (registry name)", "text", "waxs.arc",
                  help="The WAXS arc is waxs.arc (NOT waxs). Other motors: stage.phi, piezo.x …"),
            Field("values", "Positions", "floatlist", [0, 20]),
            Field("speed", "Slowness (0 fast … 2 slow)", "int", SPEED_SLOW),
        ]
    if axis_type == "spatial":
        return [
            Field("x", "X positions", "floatlist", [0, 30, 60, 90, 120]),
            Field("y", "Y positions (blank = none)", "floatlist", []),
            Field("snake", "Snake the inner axis", "bool", True),
            Field("motor_object", "Stage", "text", "piezo"),
        ]
    if axis_type == "potential":
        return [
            Field("values", "Potentials (V)", "floatlist", [0, 0.4, 0.8]),
            Field("equilibration", "Equilibrate (s)", "float", 5.0),
        ]
    if axis_type == "rh":
        return [Field("values", "Relative humidity setpoints (%)", "floatlist", [30, 50, 70])]
    if axis_type == "time":
        return [
            Field("n_frames", "Number of frames", "int", 20),
            Field("period", "Period between frames (s)", "float", 10.0),
        ]
    if axis_type == "manual":
        return [
            Field("name", "Axis label", "text", "temp_manual"),
            Field("prompt", "Prompt shown each point", "text", "Dial the hot stage to"),
            Field("values", "Values to step through", "floatlist", [35, 50, 65]),
        ]
    return []


def default_axis(axis_type: str, *, shape: str = "spot") -> AxisSpec:
    """A ready-to-use AxisSpec with defaults from :func:`axis_param_schema`."""
    params: Dict[str, Any] = {}
    for f in axis_param_schema(axis_type):
        _set_dotted(params, f.key, f.default)
    if axis_type == "spatial":
        if shape == "spot":
            params["x"], params["y"] = [0, 30, 60, 90, 120], []
        elif shape == "line":
            params["x"], params["y"] = list(range(0, 110, 10)), []
        elif shape == "grid":
            params["x"], params["y"] = [0, 25, 50, 75, 100], [0, 25, 50, 75, 100]
    return AxisSpec(type=axis_type, params=params)


def _set_dotted(d: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def reorder_axes_by_speed(axes: List[AxisSpec]) -> List[AxisSpec]:
    """Stable-sort so slower axes are outermost (the guardrail's preferred order)."""
    return sorted(axes, key=lambda a: -a.speed)


__all__ = [
    "Question", "INTAKE", "visible_questions", "seed_spec_from_intake",
    "Field", "axis_param_schema", "default_axis", "reorder_axes_by_speed",
]
