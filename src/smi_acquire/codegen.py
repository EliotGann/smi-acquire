"""
smi_acquire.codegen
===================

Render an :class:`~smi_acquire.spec.ExperimentSpec` into a **runnable, copy-pasteable**
``smi_plans`` beamline script.

This is the deliverable the user consumes today. The output is idiomatic
``smi_plans._compose`` code — ``acquire`` / ``acquire_bar`` wrapping a stack of ``ScanAxis``
builders — so the generated script automatically obeys the SMI tenets (one run/sample,
recorded context, ``{token}`` filenames, generators, slow axes outermost). It mirrors the
shape laid out in ``smi-plans/skills/smi-plans-gui-builder.md``.

Pure string assembly over the pure-data spec; imports neither bluesky nor Panel, so it is
trivially testable off-beamline (and every generated script is compiled in the test suite).
"""

from __future__ import annotations

from typing import Any, List

from .spec import AxisSpec, ExperimentSpec
from .registry import heater_identifier

#: Fallback path inserted into generated scripts only when smi_plans is NOT already importable
#: in the target session. At the beamline smi_plans is a proper (editable) install, so the
#: default is to emit no path hack at all (``templates_path=None`` / ``add_syspath=False``).
DEFAULT_TEMPLATES_PATH = "/home/xf12id/git/smi/smi-plans/src"

SPEED_CONST = {0: "SPEED_FAST", 1: "SPEED_MEDIUM", 2: "SPEED_SLOW"}


# ---------------------------------------------------------------------------
# small python-literal helpers
# ---------------------------------------------------------------------------
def _num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v)


def _numlist(vals) -> str:
    return "[" + ", ".join(_num(v) for v in vals) + "]"


def _pyval(v: Any) -> str:
    if v is None:
        return "None"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_pyval(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join("{!r}: {}".format(k, _pyval(val)) for k, val in v.items()) + "}"
    return repr(v)


# ---------------------------------------------------------------------------
# sample list block
# ---------------------------------------------------------------------------
_STACK_COLS = ["piezo_x", "piezo_y", "piezo_z", "piezo_th",
               "hexa_x", "hexa_y", "hexa_z", "hexa_th"]


def render_samplelist(spec: ExperimentSpec, *, var: str = "bar") -> str:
    """Emit a ``SampleList.from_columns(...)`` block from the spec's sample rows.

    Only columns actually populated are emitted. ``incident_angles`` is emitted shared when
    identical across rows, else per-sample. Falls back to a single placeholder sample.
    """
    rows = spec.samples.rows
    if not rows:
        rows = [{"name": "sample1"}]

    names = [r.get("name", "sample{}".format(i + 1)) for i, r in enumerate(rows)]
    lines = ["{} = SampleList.from_columns(".format(var)]
    lines.append("    names={!r},".format(names))

    for c in _STACK_COLS:
        vals = [r.get(c) for r in rows]
        if any(v is not None for v in vals):
            lines.append("    {}={},".format(c, _numlist(vals)))

    ia = [list(r.get("incident_angles", []) or []) for r in rows]
    if any(ia):
        if all(a == ia[0] for a in ia) and ia[0]:
            lines.append("    incident_angles={},".format(_numlist(ia[0])))
        else:
            lines.append("    incident_angles={},".format(_pyval(ia)))

    if any(r.get("md") for r in rows):
        lines.append("    md={},".format(_pyval([dict(r.get("md", {})) for r in rows])))
    elif spec.project_name:
        lines.append("    md={{'project_name': {!r}}},".format(spec.project_name))

    lines.append(")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# axis builders → source text
# ---------------------------------------------------------------------------
def _energy_values_src(axis: AxisSpec) -> str:
    """Source for an energy axis's value list.

    We emit the *exact* expanded list (via :meth:`AxisSpec.values`, which expands a ``grid``)
    rather than an ``np.arange`` expression, so the generated script visits precisely the
    points the spec counted — keeping the GUI's event estimate and the dry-run in lockstep.
    The user can always rewrite it as an ``np`` expression by hand.
    """
    return _numlist(axis.values())


def _render_axis(axis: AxisSpec) -> str:
    """Return the source for one axis: an expression, or ``*expr`` for spatial (a list)."""
    t = axis.type
    p = axis.params
    if t == "energy":
        extra = ""
        if p.get("flux_reseek"):
            extra = ", flux_signal=xbpm2.sumX, flux_threshold=50"
        return "energy_axis({}, settle={}{})".format(
            _energy_values_src(axis), _num(p.get("settle", 2.0)), extra)
    if t == "temperature":
        kw = ", soak={}".format(_num(p.get("soak", 60.0)))
        if p.get("first_soak") is not None:
            kw += ", first_soak={}".format(_num(p["first_soak"]))
        return "temperature_axis(heater, {}{})".format(_numlist(axis.values()), kw)
    if t == "incidence":
        return "incidence_axis(piezo.th, th0, {})".format(_numlist(axis.values()))
    if t == "motor":
        name = p.get("name", "motor")
        device = p.get("device", "waxs")
        speed = SPEED_CONST.get(int(p.get("speed", 0)), "SPEED_FAST")
        return "motor_axis({!r}, {}, {}, speed={})".format(
            name, device, _numlist(axis.values()), speed)
    if t == "spatial":
        args = []
        if p.get("x"):
            args.append("x_motor={}.x, x={}".format(_mot(p), _numlist(p["x"])))
        if p.get("y"):
            args.append("y_motor={}.y, y={}".format(_mot(p), _numlist(p["y"])))
        args.append("snake={}".format(bool(p.get("snake", True))))
        return "*spatial_grid_axes({})".format(", ".join(args))
    if t == "potential":
        return "potential_axis(set_potential, {}, equilibration={})".format(
            _numlist(axis.values()), _num(p.get("equilibration", 5.0)))
    if t == "rh":
        return "rh_axis(set_rh, {})".format(_numlist(axis.values()))
    if t == "time":
        return "time_axis({}, period={})".format(
            int(p.get("n_frames", 1)), _num(p.get("period", 0.0)))
    if t == "manual":
        name = p.get("name", "manual")
        prompt = p.get("prompt", "Set the next condition")
        vals = ", values={}".format(_pyval(axis.values())) if axis.values() else ""
        return "manual_axis({!r}, {!r}{})".format(name, prompt, vals)
    raise ValueError("unknown axis type: {!r}".format(t))


def _mot(params) -> str:
    return params.get("motor_object", "piezo")


# ---------------------------------------------------------------------------
# imports tracking
# ---------------------------------------------------------------------------
def _needed_builders(spec: ExperimentSpec) -> List[str]:
    builders = {
        "energy": "energy_axis", "temperature": "temperature_axis",
        "incidence": "incidence_axis", "motor": "motor_axis",
        "spatial": "spatial_grid_axes", "potential": "potential_axis",
        "rh": "rh_axis", "time": "time_axis", "manual": "manual_axis",
    }
    used = {builders[a.type] for a in spec.axes if a.type in builders}
    # speed constants used by motor axes
    if any(a.type == "motor" for a in spec.axes):
        used.update({"SPEED_FAST", "SPEED_MEDIUM", "SPEED_SLOW"})
    return sorted(used)


# ---------------------------------------------------------------------------
# the full script
# ---------------------------------------------------------------------------
def render(spec: ExperimentSpec, *, templates_path: str | None = None, run: bool = True,
           add_syspath: bool = False) -> str:
    """ExperimentSpec → a complete, copy-pasteable ``smi_plans`` script string.

    ``smi_plans`` is normally already importable in the beamline session (it is an editable
    install), so by default **no** ``sys.path`` manipulation is emitted. Pass
    ``add_syspath=True`` (optionally with ``templates_path``) to prepend the smi_plans ``src``
    directory in the generated script — useful only when pasting into a bare interpreter.
    """
    ap = spec.apparatus
    multi = len(spec.samples.rows) != 1
    builder = "acquire_bar" if multi else "acquire"

    # ---- decide what the body needs, so imports are exact -----------------
    setup_lines: List[str] = []
    if ap.align_routine:
        setup_lines.append("    yield from {}({})".format(ap.align_routine, _num(ap.align_angle)))
    for att in ap.attenuators_in:
        setup_lines.append("    yield from bps.mv({}.close_cmd, 1)".format(att))
    for step in spec.manual_setup:
        sigs = ", ".join(v["name"] for v in step.values)
        setup_lines.append("    yield from manual_step({!r}, signals=[{}])".format(
            step.prompt, sigs))
    has_setup = bool(setup_lines)
    needs_manual_step = bool(spec.manual_setup)
    needs_bps = bool(ap.attenuators_in)
    needs_signal = any(step.values for step in spec.manual_setup)

    # ---- imports ----------------------------------------------------------
    compose_imports = [builder] + _needed_builders(spec)
    if needs_manual_step:
        compose_imports.append("manual_step")
    L: List[str] = [
        '"""Generated by smi-acquire (interrogation builder). Review before running."""',
        "import numpy as np",
    ]
    if add_syspath:
        tpath = templates_path or DEFAULT_TEMPLATES_PATH
        L.append("import sys; sys.path.append({!r})".format(tpath))
    L += [
        "from smi_plans._compose import ({})".format(", ".join(sorted(set(compose_imports)))),
        "from smi_plans._core import saxs_waxs_dets",
        "from smi_plans import SampleList",
    ]
    if needs_bps:
        L.append("import bluesky.plan_stubs as bps")
    if needs_signal:
        L.append("from ophyd import Signal")
    if spec.apparatus.heater:
        L.append("from smi_plans.technique_C_temperature import {}".format(
            "linkam_heater" if spec.apparatus.heater == "linkam" else "lakeshore_heater"))
    L.append("")

    # ---- sample bar -------------------------------------------------------
    L.append(render_samplelist(spec))
    L.append("")

    # ---- beam / q ---------------------------------------------------------
    if spec.beam.arc_aware:
        L.append("dets = saxs_waxs_dets()        # arc-aware: SAXS dropped if WAXS arc parked")
    else:
        L.append("dets = [{}]".format(", ".join(spec.beam.detectors)))
    L.append("reads = [{}]".format(", ".join(spec.beam.reads)))
    heater_call = heater_identifier(spec.apparatus.heater)
    if heater_call:
        L.append("heater = {}".format(heater_call))

    # ---- manual-setup signals ---------------------------------------------
    for step in spec.manual_setup:
        for v in step.values:
            L.append("{0} = Signal(name={0!r}, value=0.0)".format(v["name"]))
    L.append("")

    # ---- setup() ----------------------------------------------------------
    if has_setup:
        L.append("def setup():")
        L.extend(setup_lines)
        L.append("")

    # ---- axis stack -------------------------------------------------------
    need_th0 = any(a.type == "incidence" for a in spec.axes)
    L.append("def axes_for(s):")
    if need_th0:
        L.append("    th0 = piezo.th.position")
    if spec.axes:
        L.append("    return [")
        for a in spec.axes:
            L.append("        {},".format(_render_axis(a)))
        L.append("    ]")
    else:
        L.append("    return []")
    L.append("")

    # ---- the run ----------------------------------------------------------
    L.append("det_exposure_time({0}, {0})".format(_num(spec.beam.exposure_s)))
    call_kwargs = [
        "reads=reads",
        "geometry={!r}".format(ap.geometry),
        "scan_name={!r}".format(spec.scan_name),
    ]
    md = dict(spec.md)
    if spec.project_name and "project_name" not in md:
        md["project_name"] = spec.project_name
    if md:
        call_kwargs.append("md={}".format(_pyval(md)))
    baseline = [v["name"] for step in spec.manual_setup for v in step.values]

    if multi:
        if has_setup:
            call_kwargs.insert(0, "setup_for=lambda s: setup()")
        if baseline:
            call_kwargs.append("baseline_for=lambda s: [{}]".format(", ".join(baseline)))
        call = "acquire_bar(bar, dets, axes_for, {})".format(", ".join(call_kwargs))
    else:
        if has_setup:
            call_kwargs.insert(0, "setup=setup")
        if baseline:
            call_kwargs.append("baseline=[{}]".format(", ".join(baseline)))
        call = "acquire(bar[0].name, dets, axes_for(bar[0]), sample=bar[0], {})".format(
            ", ".join(call_kwargs))

    L.append("# ---- RUN THIS ----")
    L.append("RE({})".format(call) if run else "plan = {}".format(call))

    text = "\n".join(L)
    # collapse accidental triple blank lines
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text.rstrip() + "\n"


# ---------------------------------------------------------------------------
# project-level: render one experiment over its target subset
# ---------------------------------------------------------------------------
def render_experiment(project, experiment, store, **kwargs) -> str:
    """Render one :class:`~smi_acquire.project.Experiment` over its target subset.

    ``store`` is an :class:`smi_acquire.store.AcquireStore`; the experiment's target is resolved
    against the shared sample store to produce the sample rows.
    """
    return render(project.experiment_spec(experiment, store), **kwargs)


# ---------------------------------------------------------------------------
# future: queueserver item (the spec is already shaped for it)
# ---------------------------------------------------------------------------
def to_queueserver_item(spec: ExperimentSpec) -> dict:
    """A ``bluesky-queueserver`` plan item carrying the whole spec dict.

    The future seam: a worker-side ``acquire_from_spec(spec_dict)`` plan resolves names →
    devices. We keep the spec pure data so this stays an additive change.
    """
    return {
        "name": "acquire_from_spec",
        "args": [],
        "kwargs": {"spec": spec.to_dict()},
        "item_type": "plan",
    }


__all__ = ["render", "render_samplelist", "render_experiment", "to_queueserver_item",
           "DEFAULT_TEMPLATES_PATH"]
