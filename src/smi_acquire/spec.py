"""
smi_acquire.spec
================

The **ExperimentSpec** — the single serializable model that sits between the interrogation
GUI and every consumer (code generator, dry-run validator, future queueserver submitter).

This is the heart of the rethink. An SMI experiment is *not* "one of A–O"; it is an assembly
of independent concerns that the user builds up by answering questions:

    beam / q-range      -- which detectors (+ WAXS arc reach), what to record per event
    apparatus / geometry -- grazing vs transmission, alignment, heater, attenuators ...
    sampling / scanning  -- a STACK of nested scan axes (energy, temperature, incidence,
                            spatial grid, potential, RH, time, manual ...), outermost first
    manual / interactive -- one-shot prompts that capture typed values into recorded Signals
    samples              -- one run per sample; positions come from the interactive microscope

The model is **pure data** (JSON-serializable; device references are *names/strings*, never
live objects) so it can be saved, diffed, validated headlessly, and later shipped over the
wire to a queueserver worker. ``codegen`` renders it to a runnable ``smi_plans`` script;
``dryrun`` exercises that script against simulated devices.

Nothing in this module imports bluesky, ophyd, or Panel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SPEC_VERSION = 2

# Slowness hints (mirror smi_plans._compose.SPEED_*); used by the ordering guardrail.
SPEED_FAST = 0      # piezo x/y/z, fast Signals, time frames
SPEED_MEDIUM = 1    # incident angle, energy (DCM), potential
SPEED_SLOW = 2      # waxs arc, stage.phi rotation, temperature, RH, manual swaps (slow / equilibration)


# ---------------------------------------------------------------------------
# Scan axes — one entry per nested loop dimension, OUTERMOST FIRST
# ---------------------------------------------------------------------------
@dataclass
class AxisSpec:
    """One scan dimension. ``type`` selects the smi_plans axis builder; ``params`` carry the
    builder's arguments as plain data.

    Recognized ``type`` values and their params:

    * ``energy``      ``values: [eV...]`` (or ``grid: {edge, near:[lo,hi,step], post:[lo,hi,step]}``),
                      ``settle``, ``flux_reseek: bool``
    * ``temperature`` ``values: [degC...]``, ``soak``, ``first_soak``  (heater from apparatus)
    * ``incidence``   ``values: [deg...]``                              (th0 = current piezo.th)
    * ``motor``       ``name``, ``device`` (registry name), ``values: [...]``, ``speed``
    * ``spatial``     ``x: [...]`` and/or ``y: [...]``  (absolute positions; snake inner)
    * ``potential``   ``values: [V...]``, ``equilibration``
    * ``rh``          ``values: [%RH...]``
    * ``time``        ``n_frames``, ``period``
    * ``manual``      ``name``, ``prompt``, ``values: [...]`` (enumerated user-driven loop)
    """

    type: str
    params: Dict[str, Any] = field(default_factory=dict)

    # ---- derived helpers (no side effects) --------------------------------
    @property
    def label(self) -> str:
        p = self.params
        if self.type == "motor":
            return "motor:{}".format(p.get("name", p.get("device", "?")))
        if self.type == "manual":
            return "manual:{}".format(p.get("name", "step"))
        return self.type

    @property
    def speed(self) -> int:
        if self.type in ("temperature", "rh", "manual"):
            return SPEED_SLOW
        if self.type in ("energy", "incidence", "potential"):
            return SPEED_MEDIUM
        if self.type == "motor":
            return int(self.params.get("speed", SPEED_FAST))
        return SPEED_FAST  # spatial, time

    def n_points(self) -> int:
        """How many points this axis visits (1 if degenerate / unknown)."""
        p = self.params
        if self.type == "energy":
            return max(1, len(self.values()))
        if self.type == "spatial":
            nx = max(1, len(p.get("x", []) or [1]))
            ny = max(1, len(p.get("y", []) or [1]))
            return nx * ny
        if self.type == "time":
            return max(1, int(p.get("n_frames", 1)))
        return max(1, len(self.values()))

    def values(self) -> List[Any]:
        """The concrete list of visited values.

        Expands an energy ``grid`` (segments) and a ``range: [start, stop, step]`` shorthand
        (inclusive of ``stop`` within float tolerance) used by value-list axes like ``incidence``
        — so the user can give a start/stop/step instead of listing every point. An explicit
        ``values`` list always wins if present.
        """
        p = self.params
        if p.get("values"):
            return list(p.get("values") or [])
        if self.type == "energy" and "grid" in p:
            return energy_grid_values(p["grid"])
        rng = p.get("range")
        if rng and len(rng) == 3:
            return _arange(float(rng[0]), float(rng[1]), float(rng[2]))
        return list(p.get("values", []) or [])


# ---------------------------------------------------------------------------
# Concern blocks
# ---------------------------------------------------------------------------
@dataclass
class BeamSpec:
    """Beam / q-range: which detectors and what to record per event."""
    detectors: List[str] = field(default_factory=lambda: ["pil2M", "pil900KW"])
    arc_aware: bool = True                       # use saxs_waxs_dets() vs explicit list
    reads: List[str] = field(default_factory=lambda: ["energy", "waxs", "xbpm2", "xbpm3"])
    exposure_s: float = 1.0


@dataclass
class ApparatusSpec:
    """Apparatus / geometry: composes into the ``setup()`` plan run once per run."""
    geometry: str = "transmission"               # "transmission" | "reflection"
    align_routine: Optional[str] = None          # registry name, e.g. "alignement_gisaxs_hex"
    align_angle: float = 0.1
    heater: Optional[str] = None                 # None | "linkam" | "lakeshore"
    attenuators_in: List[str] = field(default_factory=list)


@dataclass
class ManualSetupStep:
    """A one-shot manual checkpoint that captures typed values into recorded Signals."""
    prompt: str
    values: List[Dict[str, str]] = field(default_factory=list)   # [{name, cast}]


@dataclass
class SamplesSpec:
    """One run per sample. Rows usually come from the interactive microscope bookmark list."""
    source: str = "inline"                       # "inline" | "csv"
    rows: List[Dict[str, Any]] = field(default_factory=list)     # SampleList.to_dicts() shape
    motor_object: str = "piezo"                  # which stack the x/y/z map onto


@dataclass
class ExperimentSpec:
    version: int = SPEC_VERSION
    project_name: str = ""
    scan_name: str = "acquire"
    md: Dict[str, Any] = field(default_factory=dict)

    beam: BeamSpec = field(default_factory=BeamSpec)
    apparatus: ApparatusSpec = field(default_factory=ApparatusSpec)
    axes: List[AxisSpec] = field(default_factory=list)           # OUTERMOST FIRST
    manual_setup: List[ManualSetupStep] = field(default_factory=list)
    samples: SamplesSpec = field(default_factory=SamplesSpec)

    # ---- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentSpec":
        d = dict(d or {})
        beam = BeamSpec(**d.get("beam", {})) if d.get("beam") else BeamSpec()
        appa = ApparatusSpec(**d.get("apparatus", {})) if d.get("apparatus") else ApparatusSpec()
        axes = [AxisSpec(**a) for a in d.get("axes", [])]
        manual = [ManualSetupStep(**m) for m in d.get("manual_setup", [])]
        samples = SamplesSpec(**d.get("samples", {})) if d.get("samples") else SamplesSpec()
        return cls(
            version=d.get("version", SPEC_VERSION),
            project_name=d.get("project_name", ""),
            scan_name=d.get("scan_name", "acquire"),
            md=dict(d.get("md", {})),
            beam=beam, apparatus=appa, axes=axes,
            manual_setup=manual, samples=samples,
        )

    # ---- analysis (pure; mirrors the smi_plans guardrails) ----------------
    def n_samples(self) -> int:
        return max(1, len(self.samples.rows))

    def events_per_sample(self) -> int:
        n = 1
        for a in self.axes:
            n *= a.n_points()
        return n

    def total_events(self) -> int:
        return self.events_per_sample() * self.n_samples()

    def order_warnings(self) -> List[str]:
        """Warn when a slow axis is nested inside a faster one (moved too often).

        Mirrors ``smi_plans._compose._check_axis_order`` so the GUI can surface the same
        guardrail the runtime would.
        """
        warns: List[str] = []
        active = [a for a in self.axes if a.n_points() > 0]
        for i, a in enumerate(active):
            outer_moves = 1
            for outer in active[: i + 1]:
                outer_moves *= a_n(outer)
            for k in range(i + 1, len(active)):
                inner = active[k]
                if inner.speed > a.speed:
                    moves = outer_moves
                    for mid in active[i + 1: k + 1]:
                        moves *= a_n(mid)
                    warns.append(
                        "slow axis '{}' is nested inside faster axis '{}' — it will move "
                        "{}× (put slower axes outermost)".format(inner.label, a.label, moves)
                    )
        return warns

    def filename_tokens(self) -> List[str]:
        """The ``{field}`` tokens the chosen axes will make available in filenames."""
        toks: List[str] = []
        for a in self.axes:
            tok = _axis_token(a)
            if tok:
                toks.append(tok)
        return toks

    def summary(self) -> str:
        """One-line human summary of the axis stack (outer → inner)."""
        if not self.axes:
            return "single point, one run/sample"
        stack = " × ".join("{}[{}]".format(a.label, a.n_points()) for a in self.axes)
        return "{}, one run/sample".format(stack)


def a_n(axis: AxisSpec) -> int:
    return max(1, axis.n_points())


# ---------------------------------------------------------------------------
# Energy grid expansion (shared by spec + codegen)
# ---------------------------------------------------------------------------
def _arange(lo: float, hi: float, step: float) -> List[float]:
    if step == 0:
        return [lo]
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    return [round(lo + i * step, 6) for i in range(max(0, n))]


def energy_grid_values(grid: Dict[str, Any]) -> List[float]:
    """Expand an energy ``grid`` dict into absolute eV points (deduped, sorted).

    Two shapes are supported:

    * **Boundaries + density (preferred, flexible N regions):**
      ``{"boundaries": [b0, b1, ..., bN], "steps": [s0, s1, ..., s(N-1)]}`` — ``N`` contiguous
      regions in ABSOLUTE eV, each ``[b_i, b_{i+1})`` stepped by ``s_i`` (the last region
      includes its final boundary).  This is the ``np.arange(b0,b1,s0)+np.arange(b1,b2,s1)+…``
      pattern: e.g. a sulfur scan ``boundaries=[2445,2470,2480,2490,2501]``,
      ``steps=[5,0.25,1,5]``.  Add/remove boundaries to get 3, 4, 5+ regions.
    * **Edge-relative segments (back-compat):** ``{"edge": 2472, "pre"/"near"/"post":
      [lo,hi,step]}`` where the segment ranges are relative to ``edge``.  Any segment may be
      omitted.
    """
    # Preferred: boundaries + per-interval density (absolute eV).
    bounds = grid.get("boundaries")
    steps = grid.get("steps")
    if bounds and steps and len(bounds) >= 2 and len(steps) >= len(bounds) - 1:
        # Each region is np.arange(b_i, b_{i+1}, s_i) -- half-open [b_i, b_{i+1}), exactly the
        # ``np.arange(...)+np.arange(...)`` chain.  Half-open everywhere means a shared boundary
        # is owned by the next region (visited once), and the upper boundary is the bound, not a
        # visited point (so e.g. stop=2501 with step 5 reaches 2500), matching np.arange.
        pts: List[float] = []
        for i in range(len(bounds) - 1):
            lo, hi, step = float(bounds[i]), float(bounds[i + 1]), float(steps[i])
            pts.extend(v for v in _arange(lo, hi, step) if v < hi - 1e-9)
        return sorted(set(round(p, 6) for p in pts))

    # Back-compat: edge-relative pre/near/post segments.
    edge = float(grid.get("edge", 0.0))
    pts = []
    for seg_name in ("pre", "near", "post"):
        rng = grid.get(seg_name)
        if rng and len(rng) == 3:
            pts.extend(edge + v for v in _arange(float(rng[0]), float(rng[1]), float(rng[2])))
    return sorted(set(round(p, 6) for p in pts))


def _axis_token(a: AxisSpec) -> str:
    """Match the auto filename tokens smi_plans._compose.acquire() builds from each axis."""
    t = a.type
    if t == "energy":
        return "energy{energy_set}"
    if t == "incidence":
        return "incident_angle{incident_angle}"
    if t == "potential":
        return "potential{potential_v}"
    if t == "rh":
        return "rh{rh}"
    if t == "time":
        return "frame{frame}"
    if t == "manual":
        name = a.params.get("record_name") or a.params.get("name", "manual")
        return "{0}{{{0}}}".format(name)
    return ""


__all__ = [
    "SPEC_VERSION", "SPEED_FAST", "SPEED_MEDIUM", "SPEED_SLOW",
    "AxisSpec", "BeamSpec", "ApparatusSpec", "ManualSetupStep", "SamplesSpec",
    "ExperimentSpec", "energy_grid_values",
]
