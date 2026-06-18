"""
smi_acquire.registry
====================

The **catalog** the interrogation and the code generator both read: the SMI device names,
detector sets, heaters, alignment routines, and the menu of scan-axis *concerns* the user can
stack up.

Keeping this here (rather than scattered through the GUI) means: the interview is data-driven
(it asks about whatever the registry offers), the codegen maps spec *names* → the bare
identifiers the beamline IPython session exposes, and the dry-run sim knows the same names.

Everything is plain data. Device references are the identifiers the SMI ``profile_collection``
injects as globals (``piezo``, ``waxs``, ``energy``, ``pil2M`` …) — the spec stores the *name*,
the generator emits the *identifier*, and the simulated beamline provides a stand-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .spec import SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW


@dataclass(frozen=True)
class DeviceInfo:
    name: str                 # spec name == generated identifier (bare global at the beamline)
    kind: str                 # "detector" | "motor" | "signal" | "heater" | "stack"
    label: str
    speed: int = SPEED_FAST
    note: str = ""


# ---------------------------------------------------------------------------
# Detectors (the beam / q-range choice)
# ---------------------------------------------------------------------------
DETECTORS: List[DeviceInfo] = [
    DeviceInfo("pil2M",     "detector", "Pilatus 2M (SAXS)",       note="large-area SAXS"),
    DeviceInfo("pil900KW",  "detector", "Pilatus 900kW (WAXS arc)", note="WAXS, arc-mounted"),
    DeviceInfo("pil300KW",  "detector", "Pilatus 300kW (WAXS)",     note="WAXS"),
    DeviceInfo("rayonix",   "detector", "Rayonix (SAXS, optional)"),
    DeviceInfo("amptek",    "detector", "Amptek (fluorescence/SDD)"),
]

# Per-event context readables (the `reads`)
READS: List[DeviceInfo] = [
    DeviceInfo("energy",    "signal", "DCM energy"),
    DeviceInfo("waxs",      "motor",  "WAXS arc position", speed=SPEED_SLOW),
    DeviceInfo("xbpm2",     "signal", "XBPM2 (I0)"),
    DeviceInfo("xbpm3",     "signal", "XBPM3"),
    DeviceInfo("pin_diode", "signal", "transmission pin diode"),
]

# Motors usable as a generic `motor` axis
MOTORS: List[DeviceInfo] = [
    DeviceInfo("waxs.arc",  "motor", "WAXS arc",            speed=SPEED_SLOW,
               note="in-vacuum; keep outer. The settable arc is waxs.arc (NOT waxs; "
                    "waxs = pil900KW.motors is a readable). waxs stays in reads."),
    DeviceInfo("stage.phi", "motor", "Huber φ (sample rotation)", speed=SPEED_SLOW,
               note="STG_pseudo rotation axis (was 'prs'); records as stage_phi"),
    DeviceInfo("piezo",     "stack", "piezo fine stage (x/y/z/th)",        speed=SPEED_FAST,
               note="SmarAct; fast/precise; on top of the Huber stage"),
    DeviceInfo("stage",     "stack", "Huber coarse stage (x/y/z/θ/χ/φ)",   speed=SPEED_FAST,
               note="coarse range + orientation under the piezo"),
]

# Top-level GISAXS/GIWAXS alignment routines from the profile
# (smi_beamline/plans/alignment.py). Each is called as ``align(angle)`` in setup().
# NOTE: spellings are preserved EXACTLY as defined at the beamline (most are the
# beamline's "alignement" spelling; ``alignment_gisaxs`` is the one with the standard spelling).
ALIGNMENT_ROUTINES: List[str] = [
    "alignement_gisaxs_hex",            # regular, hexapod (common default)
    "alignement_gisaxs_hex_short",      # hexapod, short
    "alignement_gisaxs_hex_roughsample",  # hexapod, rough/unflat sample
    "alignment_gisaxs",                 # regular (non-hex)
    "alignement_gisaxs_short",          # short (non-hex)
    "alignement_gisaxs_rough",          # rough (non-hex)
    "alignement_gisaxs_doblestack",     # double-stack holder
    "alignement_gisaxs_multisample",    # align several samples in one pass
    "quickalign_gisaxs",                # quick: reflected beam only
    "fast_align",                       # fast height + theta re-optimize
]

ATTENUATORS: List[str] = ["att2_9", "att2_10", "att2_11", "att2_12"]

HEATERS: Dict[str, str] = {
    "linkam":    "Linkam hot/cold stage (LThermal)",
    "lakeshore": "Lakeshore cryo controller (ls)",
}


# ---------------------------------------------------------------------------
# The menu of scan-axis concerns (what the user can stack up)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AxisKind:
    type: str
    label: str
    speed: int
    blurb: str                      # one-line description shown in the interview
    needs: List[str] = field(default_factory=list)   # apparatus/context prerequisites
    icon: str = "●"                 # glyph for the iconic card (unicode; no image assets)
    color: str = "#607D8B"          # accent color for the card + its nested box


AXIS_KINDS: List[AxisKind] = [
    AxisKind("energy",      "Energy sweep (NEXAFS / edge)", SPEED_MEDIUM,
             "Scan DCM energy across an absorption edge; optional beam re-seek on flux drop.",
             icon="🌈", color="#7E57C2"),
    AxisKind("temperature", "Temperature ramp",            SPEED_SLOW,
             "Step a heater through setpoints, equilibrating at each (slow → outermost).",
             needs=["heater"], icon="🌡️", color="#EF5350"),
    AxisKind("incidence",   "Grazing incidence angle",     SPEED_MEDIUM,
             "Visit th0 + each incident angle (grazing geometry).",
             needs=["reflection"], icon="📐", color="#26A69A"),
    AxisKind("motor",       "Generic motor (arc / stage.φ / …)", SPEED_FAST,
             "Step any single motor through a list of positions.",
             icon="⚙️", color="#78909C"),
    AxisKind("spatial",     "Spatial sampling (spot / line / grid)", SPEED_FAST,
             "Fresh x/y locations — a single spot, a line, or a raster grid (innermost).",
             icon="🎯", color="#42A5F5"),
    AxisKind("potential",   "Applied potential (e-chem)",  SPEED_MEDIUM,
             "Step a potentiostat through voltages, equilibrating at each.",
             icon="🔋", color="#FFA726"),
    AxisKind("rh",          "Relative humidity (SVA)",     SPEED_SLOW,
             "Step relative-humidity setpoints (slow equilibration → outer).",
             icon="💧", color="#29B6F6"),
    AxisKind("time",        "Time series (kinetics)",      SPEED_FAST,
             "Repeat N frames at a fixed period (innermost).",
             icon="⏱️", color="#66BB6A"),
    AxisKind("manual",      "Manual / user-driven step",   SPEED_SLOW,
             "Prompt the operator at each point (hand-set condition, sample swap).",
             icon="✋", color="#8D6E63"),
]

AXIS_KIND_BY_TYPE: Dict[str, AxisKind] = {k.type: k for k in AXIS_KINDS}


def detector_names() -> List[str]:
    return [d.name for d in DETECTORS]


def read_names() -> List[str]:
    return [d.name for d in READS]


def motor_axis_names() -> List[str]:
    return [d.name for d in MOTORS]


def heater_identifier(kind: Optional[str]) -> Optional[str]:
    """The smi_plans heater *factory* call for a heater kind."""
    if kind == "linkam":
        return "linkam_heater()"
    if kind == "lakeshore":
        return "lakeshore_heater()"
    return None


__all__ = [
    "DeviceInfo", "AxisKind",
    "DETECTORS", "READS", "MOTORS", "ALIGNMENT_ROUTINES", "ATTENUATORS", "HEATERS",
    "AXIS_KINDS", "AXIS_KIND_BY_TYPE",
    "detector_names", "read_names", "motor_axis_names", "heater_identifier",
]
