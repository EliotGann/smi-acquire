"""
smi_acquire.techniques
======================

A declarative registry of the A--O SMI-SWAXS technique archetypes (mirrors
``smi_plans/technique_*.py`` and the ``_analysis/USE_CASE_TAXONOMY.md`` map).

Each :class:`TechniqueSpec` describes, for *one* archetype:

* which ``smi_plans`` module / ``*_bar`` (or ``*_run``) entry point a generated script calls;
* the **scientific knobs** a user sets (:class:`ParamSpec` list) -- enough to drive a form;
* which :class:`~smi_acquire.samples.Sample` columns are relevant (so the sample-table editor
  can show only the axes this technique uses);
* the *setup* lines and the *call* template used by :mod:`smi_acquire.codegen` to emit a
  runnable script;
* guidance tags + a "recommend when" sentence consumed by :mod:`smi_acquire.guidance`.

This module is **pure Python / data only** -- no bluesky, no Panel.  Both the headless tests
and every front-end (Panel / Qt / NiceGUI) consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Parameter specification
# ---------------------------------------------------------------------------
@dataclass
class ParamSpec:
    """One user-settable knob.

    ``kind`` controls both the widget a front-end builds and how the value is rendered into a
    Python literal by :meth:`render`:

    ===========  =======================================  ===========================
    kind         widget hint                              renders as
    ===========  =======================================  ===========================
    ``float``    number input                             ``1.0``
    ``int``      integer input                            ``10``
    ``bool``     checkbox                                 ``True`` / ``False``
    ``str``      text input                               ``'text'`` (quoted)
    ``choice``   dropdown (quoted str value)              ``'transmission'``
    ``token``    dropdown of *code identifiers*           ``alignement_gisaxs_hex`` (no quotes)
    ``floats``   text -> list of floats                   ``[0.1, 0.2]``
    ``tuple``    text -> tuple of numbers                 ``(-60, 60, 121)``
    ``optfloat`` number input, blank allowed              ``30.0`` or ``None``
    ===========  =======================================  ===========================
    """

    name: str
    label: str
    kind: str
    default: Any
    help: str = ""
    choices: Optional[List[str]] = None
    group: str = "Parameters"

    def render(self, value=None) -> str:
        v = self.default if value is None else value
        k = self.kind
        if k in ("float", "int"):
            return repr(v)
        if k == "bool":
            return "True" if v else "False"
        if k in ("str", "choice"):
            return repr(str(v))
        if k == "token":
            return str(v)  # bare identifier (device / routine name)
        if k == "optfloat":
            if v in (None, "", "None"):
                return "None"
            return repr(float(v))
        if k == "floats":
            seq = _as_number_list(v)
            return "[" + ", ".join(_num(x) for x in seq) + "]"
        if k == "tuple":
            seq = _as_number_list(v)
            return "(" + ", ".join(_num(x) for x in seq) + ")"
        return repr(v)


def _num(x):
    f = float(x)
    return str(int(f)) if f.is_integer() else repr(f)


def _as_number_list(v):
    if isinstance(v, (list, tuple)):
        return list(v)
    return [float(t) for t in str(v).replace(";", " ").replace(",", " ").split()]


# ---------------------------------------------------------------------------
# Technique specification
# ---------------------------------------------------------------------------
@dataclass
class TechniqueSpec:
    letter: str
    alias: str               # import alias used in generated scripts, e.g. "A"
    module: str              # smi_plans module, e.g. "technique_A_energy_edge"
    entry: str               # function called, e.g. "nexafs_bar"
    title: str
    summary: str
    recommend_when: str
    tags: List[str]
    sample_fields: List[str]
    params: List[ParamSpec]
    setup: List[str] = field(default_factory=list)   # template lines before the RE() call
    call_template: str = "bar"                        # args inside entry(...), {param} subst
    needs: List[str] = field(default_factory=list)    # human notes on required beamline objects
    notes: str = ""

    # -- rendering -----------------------------------------------------------
    def rendered(self, values: Dict[str, Any]) -> Dict[str, str]:
        return {p.name: p.render(values.get(p.name)) for p in self.params}

    def render_setup(self, values) -> List[str]:
        r = self.rendered(values)
        return [line.format(**r) for line in self.setup]

    def render_call(self, values) -> str:
        r = self.rendered(values)
        return "{a}.{e}({args})".format(a=self.alias, e=self.entry,
                                        args=self.call_template.format(**r))

    def defaults(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params}


# ---------------------------------------------------------------------------
# Common parameter fragments
# ---------------------------------------------------------------------------
def _t(default=1.0):
    return ParamSpec("t", "Exposure / averaging time (s)", "float", default,
                     "Frame exposure; applied to detectors (and pin_diode where relevant).")


def _geometry(default="transmission"):
    return ParamSpec("geometry", "Geometry", "choice", default,
                     "Reflection (grazing) or transmission.",
                     choices=["transmission", "reflection"])


def _dose_step():
    return ParamSpec("dose_step", "Fresh-spot step (um, blank=off)", "optfloat", None,
                     "If set, walk piezo.x by this much each frame to expose a fresh spot "
                     "(beam-damage mitigation).", group="Idioms")


PIEZO_FIELDS = ["name", "piezo_x", "piezo_y", "piezo_z", "md"]
PIEZO_GI_FIELDS = ["name", "piezo_x", "piezo_y", "piezo_z", "piezo_th",
                   "hexa_x", "incident_angles", "md"]


# ---------------------------------------------------------------------------
# The registry (A -- O)
# ---------------------------------------------------------------------------
TECHNIQUES: Dict[str, TechniqueSpec] = {}


def _reg(spec: TechniqueSpec):
    TECHNIQUES[spec.letter] = spec
    return spec


# --- A. Tender / NEXAFS energy edge ----------------------------------------
_reg(TechniqueSpec(
    letter="A", alias="A", module="technique_A_energy_edge", entry="nexafs_bar",
    title="Energy edge / NEXAFS / resonant (TReXS)",
    summary="Sweep DCM energy across an absorption edge collecting scattering and/or "
            "transmitted flux. The most common SMI mode.",
    recommend_when="You are scanning photon energy across an absorption edge "
                   "(S, Cl, P, Ca, transition-metal L, ...).",
    tags=["energy", "edge", "nexafs", "resonant", "transmission", "grazing"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("edge", "Edge energy (eV)", "float", 2822.0,
                  "Absorption-edge center; the grid is fine here, coarse in the wings.",
                  group="Energy grid"),
        ParamSpec("pre", "Pre-edge (start,stop,step eV)", "tuple", (-12, -2, 2.0),
                  "Offsets relative to the edge.", group="Energy grid"),
        ParamSpec("near", "Near-edge (start,stop,step eV)", "tuple", (-2, 2, 0.5),
                  "Fine sampling across the edge.", group="Energy grid"),
        ParamSpec("post", "Post-edge (start,stop,step eV)", "tuple", (2, 70, 5.0),
                  "Coarse post-edge.", group="Energy grid"),
        _t(1.0), _geometry("transmission"),
        ParamSpec("updown", "Up + down sweep (reversibility)", "bool", True,
                  "Follow the up-sweep with a reversed down-sweep in the SAME run."),
        ParamSpec("settle", "Settle after energy move (s)", "float", 2.0, ""),
        _dose_step(),
    ],
    setup=["energies = A.energy_grid({edge}, pre={pre}, near={near}, post={post})"],
    call_template="bar, energies, t={t}, geometry={geometry}, updown={updown}, "
                  "settle={settle}, dose_step={dose_step}",
    needs=["energy (DCM)", "att2_9 (for atten_in, optional)"],
))

# --- B. Grazing incidence (GISAXS / GIWAXS) --------------------------------
_reg(TechniqueSpec(
    letter="B", alias="B", module="technique_B_grazing", entry="giwaxs_bar",
    title="Grazing incidence (GISAXS / GIWAXS) + alignment",
    summary="Thin films at one or more incident angles across WAXS-arc positions, with "
            "per-sample alignment.",
    recommend_when="You have thin films / surfaces measured at grazing incidence and need "
                   "per-sample alignment.",
    tags=["grazing", "thin-film", "alignment", "arc", "reflection"],
    sample_fields=PIEZO_GI_FIELDS,
    params=[
        ParamSpec("align", "Alignment routine", "token", "alignement_gisaxs_hex",
                  "Profile-collection GI alignment plan-function.",
                  choices=["alignement_gisaxs_hex", "alignement_gisaxs_doblestack"]),
        ParamSpec("align_angle", "Alignment angle (deg)", "float", 0.1, ""),
        ParamSpec("waxs_arc", "WAXS arc positions (deg)", "floats", [0, 20],
                  "Slow in-vacuum axis; kept outermost.", group="Arc"),
        ParamSpec("default_incident_angles", "Default incident angles (deg)", "floats",
                  [0.1, 0.2], "Used when a sample row has no incident_angles."),
        _t(1.0), _dose_step(),
        ParamSpec("arc_economy", "Arc economy (multi-open-run)", "bool", False,
                  "Move waxs.arc only once for the WHOLE bar (opens one run per sample at "
                  "once). Use when arc travel dominates overhead.", group="Strategy"),
    ],
    call_template="bar, align={align}, align_angle={align_angle}, waxs_arc={waxs_arc}, "
                  "t={t}, dose_step={dose_step}, "
                  "default_incident_angles={default_incident_angles}",
    needs=["piezo / stage", "waxs (arc)", "an alignment routine"],
    notes="Set 'Arc economy' to switch the entry point to giwaxs_bar_arc_economy.",
))

# --- C. Temperature --------------------------------------------------------
_reg(TechniqueSpec(
    letter="C", alias="C", module="technique_C_temperature", entry="temperature_bar",
    title="Temperature ramp / anneal / melt",
    summary="Scattering vs temperature: ramp, isothermal hold, melting/ODT/crystallization.",
    recommend_when="Your control variable is temperature (Lakeshore / Linkam / Instec).",
    tags=["temperature", "in-situ", "ramp", "anneal", "kinetics"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("heater_factory", "Heater", "token", "lakeshore_heater",
                  "Which heater abstraction to build.",
                  choices=["lakeshore_heater", "linkam_heater"]),
        ParamSpec("setpoints", "Setpoints (deg)", "floats", [30, 60, 90, 120],
                  "Temperatures to step through.", group="Ramp"),
        _t(1.0), _geometry("transmission"),
        ParamSpec("soak", "Soak / equilibration after setpoint (s)", "float", 60.0, ""),
        ParamSpec("tol", "Setpoint tolerance (deg)", "float", 1.0, ""),
        ParamSpec("timeout", "Equilibration timeout (s)", "float", 7200.0, ""),
        _dose_step(),
    ],
    setup=["heater = C.{heater_factory}()"],
    call_template="bar, heater, setpoints={setpoints}, t={t}, geometry={geometry}, "
                  "soak={soak}, tol={tol}, timeout={timeout}, dose_step={dose_step}",
    needs=["ls (Lakeshore) or LThermal (Linkam)"],
))

# --- D. Mapping ------------------------------------------------------------
_reg(TechniqueSpec(
    letter="D", alias="D", module="technique_D_mapping", entry="map_bar",
    title="Microfocus raster mapping (spatial)",
    summary="Map a heterogeneous sample over an x/y grid, line, or spiral with a microbeam.",
    recommend_when="You are rastering a microbeam over a spatial region (line / grid / spiral).",
    tags=["mapping", "microfocus", "raster", "spatial"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("kind", "Map kind", "choice", "grid",
                  "line, grid, or spiral.", choices=["line", "grid", "spiral"]),
        ParamSpec("x_range", "X half-range (um)", "float", 500.0, "", group="Extent"),
        ParamSpec("y_range", "Y half-range (um)", "float", 500.0, "", group="Extent"),
        ParamSpec("x_num", "X points", "int", 21, "", group="Extent"),
        ParamSpec("y_num", "Y points", "int", 21, "", group="Extent"),
        _t(0.5), _geometry("transmission"),
    ],
    setup=[
        "# map_bar takes a per-sample map_plan(sample)->plan. Build one with the chosen kind:",
        "def map_plan(sample):",
        "    return D.map_grid_run(sample.name, piezo.x, -{x_range}, {x_range}, {x_num},",
        "                          piezo.y, -{y_range}, {y_range}, {y_num},",
        "                          t={t}, geometry={geometry})",
    ],
    call_template="bar, map_plan",
    needs=["piezo.x / piezo.y (microbeam)"],
    notes="The generated map_plan uses map_grid_run; swap for map_line_run / map_spiral_run "
          "as needed.",
))

# --- E. Transmission -------------------------------------------------------
_reg(TechniqueSpec(
    letter="E", alias="E", module="technique_E_transmission", entry="transmission_bar",
    title="Transmission SAXS/WAXS (capillaries, wells, solutions)",
    summary="Transmission scattering of solutions / capillaries / well-plates, with optional "
            "multi-spot averaging.",
    recommend_when="You are measuring in transmission (capillaries, well plates, solutions).",
    tags=["transmission", "solution", "capillary", "wellplate"],
    sample_fields=PIEZO_FIELDS,
    params=[
        _t(1.0),
        ParamSpec("points_fast", "Spots along fast axis", "int", 1,
                  "Multi-spot averaging count.", group="Multi-spot"),
        ParamSpec("points_slow", "Spots along slow axis", "int", 1, "", group="Multi-spot"),
        ParamSpec("d_fast", "Fast-spot spacing (um)", "float", 150.0, "", group="Multi-spot"),
        ParamSpec("d_slow", "Slow-spot spacing (um)", "float", 0.0, "", group="Multi-spot"),
        _dose_step(),
    ],
    call_template="bar, t={t}, points_fast={points_fast}, points_slow={points_slow}, "
                  "d_fast={d_fast}, d_slow={d_slow}, dose_step={dose_step}",
    needs=["pin_diode (transmission)"],
))

# --- F. Kinetics / time series ---------------------------------------------
_reg(TechniqueSpec(
    letter="F", alias="F", module="technique_F_kinetics", entry="time_series_bar",
    title="In-situ kinetics / time series",
    summary="Follow a process in time: drying, blade-coating, flow, tensile, UV, swelling.",
    recommend_when="Your variable is elapsed time (drying / flow / strain / growth).",
    tags=["kinetics", "time-series", "in-situ", "drying", "flow"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("n_frames", "Number of frames (blank=use duration)", "optfloat", 60,
                  "", group="Timing"),
        ParamSpec("duration", "Duration (s, blank=use n_frames)", "optfloat", None,
                  "", group="Timing"),
        ParamSpec("period", "Period between frames (s)", "float", 1.0, "", group="Timing"),
        _t(0.5), _geometry("transmission"), _dose_step(),
    ],
    call_template="bar, n_frames={n_frames}, duration={duration}, period={period}, "
                  "t={t}, geometry={geometry}, dose_step={dose_step}",
))

# --- G. Humidity / RH ------------------------------------------------------
_reg(TechniqueSpec(
    letter="G", alias="G", module="technique_G_humidity", entry="rh_step_series_bar",
    title="Humidity / solvent-vapor annealing (RH)",
    summary="Controlled-humidity swelling / SVA via dry/wet N2 mixing.",
    recommend_when="Your control variable is relative humidity / solvent vapor.",
    tags=["humidity", "rh", "sva", "in-situ"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("rh_setpoints", "RH setpoints (%)", "floats", [10, 30, 50, 70, 90],
                  "", group="RH"),
        ParamSpec("measure_at_rh", "Frames per RH step", "int", 1, "", group="RH"),
        _t(1.0), _geometry("transmission"),
        ParamSpec("equilibration_timeout", "Equilibration timeout (s)", "float", 1800.0, ""),
        ParamSpec("equilibration_tol", "RH tolerance (%)", "float", 2.0, ""),
        _dose_step(),
    ],
    call_template="bar, rh_setpoints={rh_setpoints}, measure_at_rh={measure_at_rh}, "
                  "t={t}, geometry={geometry}, "
                  "equilibration_timeout={equilibration_timeout}, "
                  "equilibration_tol={equilibration_tol}, dose_step={dose_step}",
    needs=["MFC dry/wet flow controllers"],
))

# --- H. Electrochemistry ---------------------------------------------------
_reg(TechniqueSpec(
    letter="H", alias="H", module="technique_H_echem", entry="potential_step_bar",
    title="Electrochemistry / operando doping",
    summary="Scattering / NEXAFS vs applied potential or chemical doping state.",
    recommend_when="Your control variable is applied potential / doping state.",
    tags=["echem", "operando", "potential", "doping"],
    sample_fields=PIEZO_GI_FIELDS,
    params=[
        ParamSpec("potentials", "Potentials (V)", "floats", [0.0, 0.2, 0.4, 0.6], "",
                  group="Potential"),
        ParamSpec("set_potential", "set_potential device/callable", "token", "set_potential",
                  "A callable/Signal that applies a potential.", group="Potential"),
        ParamSpec("measure_at_v", "Frames per potential", "int", 1, "", group="Potential"),
        _t(1.0), _geometry("reflection"),
        ParamSpec("equilibration", "Equilibration after step (s)", "float", 5.0, ""),
        _dose_step(),
    ],
    call_template="bar, potentials={potentials}, set_potential={set_potential}, "
                  "measure_at_v={measure_at_v}, t={t}, geometry={geometry}, "
                  "equilibration={equilibration}, dose_step={dose_step}",
    needs=["a potentiostat interface (set_potential)"],
))

# --- I. CD-SAXS ------------------------------------------------------------
_reg(TechniqueSpec(
    letter="I", alias="I", module="technique_I_cdsaxs", entry="cdsaxs_bar",
    title="CD-SAXS grating metrology",
    summary="Rock a nanograting through reciprocal space (prs / phi) for CD reconstruction.",
    recommend_when="You are doing CD-SAXS / CD-GISAXS grating metrology (prs rocking).",
    tags=["cdsaxs", "metrology", "grating", "prs", "rocking"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("prs_range", "prs rock (start,stop,points)", "tuple", (-60, 60, 121),
                  "Canonical -60..60 in 121 points.", group="Rocking"),
        _t(1.0),
        ParamSpec("phi_offset", "Reference phi offset (deg)", "float", 0.0, ""),
        ParamSpec("ref_brackets", "Bracket reference frames", "bool", True, ""),
    ],
    call_template="bar, prs_range={prs_range}, t={t}, phi_offset={phi_offset}, "
                  "ref_brackets={ref_brackets}",
    needs=["prs (phi rocking stage)", "pil2M"],
))

# --- J. XRR ----------------------------------------------------------------
_reg(TechniqueSpec(
    letter="J", alias="J", module="technique_J_xrr", entry="xrr_bar",
    title="X-ray reflectivity (XRR)",
    summary="Specular reflectivity vs incident angle (incl. resonant / liquid surfaces).",
    recommend_when="You are measuring specular reflectivity vs incident angle.",
    tags=["xrr", "reflectivity", "specular", "reflection"],
    sample_fields=PIEZO_GI_FIELDS,
    params=[
        ParamSpec("angles", "Incident angles (deg)", "floats",
                  [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0], "", group="Angles"),
        _t(1.0),
        ParamSpec("settle", "Settle after angle move (s)", "float", 1.0, ""),
    ],
    call_template="bar, angles={angles}, t={t}, settle={settle}",
    needs=["piezo.th / th axis", "attenuator ladder (recommended)"],
))

# --- K. Tomography / texture -----------------------------------------------
_reg(TechniqueSpec(
    letter="K", alias="K", module="technique_K_tomography", entry="tomography_bar",
    title="SAXS/WAXS tomography & texture",
    summary="Rotation series (prs) for tomographic reconstruction or texture/pole figures.",
    recommend_when="You are doing a rotation series for tomography or texture.",
    tags=["tomography", "texture", "prs", "rotation"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("prs_range", "prs rotation (start,stop,points)", "tuple", (-90, 90, 181),
                  "", group="Rotation"),
        _t(1.0),
    ],
    call_template="bar, prs_range={prs_range}, t={t}",
    needs=["prs (rotation stage)"],
))

# --- N. XPCS ---------------------------------------------------------------
_reg(TechniqueSpec(
    letter="N", alias="N", module="technique_N_xpcs", entry="xpcs_bar",
    title="XPCS / coherent speckle bursts",
    summary="High-frame-rate speckle bursts for g2 correlation (single-spot).",
    recommend_when="You are capturing coherent speckle time-series (XPCS).",
    tags=["xpcs", "coherent", "speckle", "burst"],
    sample_fields=PIEZO_FIELDS,
    params=[
        ParamSpec("frame_time", "Frame time (s)", "float", 0.01, "", group="Burst"),
        ParamSpec("n_frames", "Frames per burst", "int", 1000, "", group="Burst"),
        ParamSpec("period", "Period (s, blank=continuous)", "optfloat", None, "",
                  group="Burst"),
        _geometry("transmission"),
    ],
    call_template="bar, frame_time={frame_time}, n_frames={n_frames}, period={period}, "
                  "geometry={geometry}",
    needs=["a detector configured for burst capture"],
))


# --- Special, non-bar archetypes (D-map handled above; L / M / O are run/loop-based) -------
# These three are intentionally *not* bar-driven; the registry records them so the GUI can
# guide users and emit a single-run / loop template rather than a bar call.
SPECIAL = {
    "L": dict(
        title="In-situ 3D printing (external master)",
        module="technique_L_printing", alias="L", entry="printer_triggered_run",
        summary="Long-lived run that records a frame each time the printer fires (EPICS "
                "trigger), polled via a generator -- never a busy-wait.",
        recommend_when="The printer (or another external master) drives acquisition timing.",
        tags=["printing", "operando", "external-trigger"],
        template="RE(L.printer_triggered_run('print01', n_events=200, t=1.0))",
    ),
    "M": dict(
        title="Autonomous / closed-loop (ML / agent)",
        module="technique_M_autonomous", alias="M", entry="autonomous_loop",
        summary="A decision loop (the ONE sanctioned place RE() is called) that drives proper "
                "single-run measurements and reads results back from the broker.",
        recommend_when="An optimizer / ML agent decides the next measurement.",
        tags=["autonomous", "ml", "agent", "closed-loop"],
        template=("# autonomous_loop is run directly (it owns the RE), not inside RE():\n"
                  "M.autonomous_loop(suggest, analyze, max_iter=20, re=RE)"),
    ),
    "O": dict(
        title="Commissioning / calibration (staff)",
        module="technique_O_commissioning", alias="O", entry="agbh_calibration_run",
        summary="Staff utilities: AgBehenate SDD calibration, attenuator ladders, direct-beam "
                "scans.",
        recommend_when="You are beamline staff calibrating / commissioning.",
        tags=["commissioning", "calibration", "staff"],
        template="RE(O.agbh_calibration_run(name='AgBH', t=1.0))",
    ),
}


def all_letters():
    """A--O in order, including the special run/loop archetypes."""
    return sorted(set(list(TECHNIQUES) + list(SPECIAL)))


def get(letter: str) -> Optional[TechniqueSpec]:
    return TECHNIQUES.get(letter)


__all__ = ["ParamSpec", "TechniqueSpec", "TECHNIQUES", "SPECIAL",
           "all_letters", "get"]
