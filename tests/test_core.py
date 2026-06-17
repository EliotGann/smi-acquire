"""
Headless tests for the interrogation-driven core.

The pure-Python layer (spec / interview / codegen) is tested everywhere; the dry-run tests
require the beamline env (bluesky + smi_plans) and skip cleanly off-beamline.
"""

from __future__ import annotations

import ast

import pytest

from smi_acquire import interview, codegen, registry
from smi_acquire.spec import AxisSpec, ExperimentSpec, energy_grid_values


# ---------------------------------------------------------------------------
# spec model
# ---------------------------------------------------------------------------
def test_spec_roundtrip():
    sp = interview.seed_spec_from_intake(
        {"project_name": "p", "geometry": "reflection", "q": "both",
         "varying": ["temperature", "energy", "incidence"], "heater": "linkam",
         "align": True, "sample_mode": "bar"},
        [{"name": "s1", "piezo_x": 1, "incident_angles": [0.1, 0.2]}])
    d = sp.to_dict()
    assert ExperimentSpec.from_dict(d).to_dict() == d


def test_energy_grid_expansion_is_exact():
    vals = energy_grid_values({"edge": 2472, "near": [-2, 2, 0.25], "post": [2, 60, 5]})
    assert vals[0] == 2470.0
    assert 2472.0 in vals
    # near: 2470..2474 step .25 = 17 pts; post: 2474..2529 step 5 = 12 pts; 2474 shared
    assert len(vals) == 17 + 12 - 1


def test_event_estimate_multiplies_axes():
    sp = ExperimentSpec(axes=[
        AxisSpec("temperature", {"values": [30, 60, 90]}),
        AxisSpec("incidence", {"values": [0.1, 0.2]}),
    ])
    assert sp.events_per_sample() == 6


# ---------------------------------------------------------------------------
# interrogation
# ---------------------------------------------------------------------------
def test_seed_orders_slow_axes_outermost():
    sp = interview.seed_spec_from_intake(
        {"geometry": "reflection", "q": "both",
         "varying": ["spatial", "temperature", "energy"], "heater": "linkam"})
    # temperature (slow) must come before energy (medium) before spatial (fast)
    types = [a.type for a in sp.axes]
    assert types.index("temperature") < types.index("energy") < types.index("spatial")


def test_seed_sets_apparatus_from_environment():
    sp = interview.seed_spec_from_intake(
        {"geometry": "reflection", "varying": ["temperature"], "heater": "lakeshore",
         "align": True})
    assert sp.apparatus.heater == "lakeshore"
    assert sp.apparatus.align_routine == "alignement_gisaxs_hex"
    assert sp.apparatus.geometry == "reflection"


def test_manual_mode_adds_capture_step():
    sp = interview.seed_spec_from_intake(
        {"varying": [], "sample_mode": "manual", "manual_thickness": True})
    assert sp.manual_setup and sp.manual_setup[0].values[0]["name"] == "thickness_nm"


def test_order_warning_fires_when_slow_inside_fast():
    sp = ExperimentSpec(axes=[
        AxisSpec("spatial", {"x": [0, 1, 2]}),          # fast, outer (wrong)
        AxisSpec("temperature", {"values": [30, 60]}),  # slow, inner (wrong)
    ])
    assert sp.order_warnings()


# ---------------------------------------------------------------------------
# codegen — every generated script must be valid python
# ---------------------------------------------------------------------------
def _all_axis_specs():
    out = []
    for kind in registry.AXIS_KINDS:
        out.append(interview.default_axis(kind.type))
    return out


def test_codegen_compiles_for_every_axis_kind():
    for ax in _all_axis_specs():
        sp = ExperimentSpec(axes=[ax])
        sp.samples.rows = [{"name": "s1"}]
        src = codegen.render(sp)
        ast.parse(src)  # raises SyntaxError on bad codegen


def test_codegen_multi_sample_uses_acquire_bar():
    sp = ExperimentSpec(axes=[interview.default_axis("energy")])
    sp.samples.rows = [{"name": "a"}, {"name": "b"}]
    src = codegen.render(sp)
    assert "acquire_bar(" in src
    ast.parse(src)


def test_codegen_single_sample_uses_acquire():
    sp = ExperimentSpec(axes=[interview.default_axis("energy")])
    sp.samples.rows = [{"name": "only"}]
    src = codegen.render(sp)
    assert "acquire(bar[0].name" in src
    ast.parse(src)


def test_codegen_emits_heater_and_manual_imports():
    sp = interview.seed_spec_from_intake(
        {"geometry": "reflection", "varying": ["temperature"], "heater": "linkam",
         "sample_mode": "manual", "manual_thickness": True}, [{"name": "s1"}])
    src = codegen.render(sp)
    assert "linkam_heater" in src
    assert "manual_step" in src and "from ophyd import Signal" in src
    ast.parse(src)


# ---------------------------------------------------------------------------
# message purity — generated plans must contain ONLY messages
# (smi-plans tenet: never a bare device .put()/.get()/.set() inside a plan;
#  use yield from bps.mv / bps.rd. Mirrors smi-plans/tests/test_message_purity.py.)
# ---------------------------------------------------------------------------
# attribute owners where .get(...) is plain-Python dict/object access, not a device read.
_DICT_GET_OWNERS = {"md", "kwargs", "ctx", "context", "spec", "s", "d", "state",
                    "params", "cfg", "config", "self", "p"}


def _bare_device_calls(src):
    """Return [(lineno, code)] of bare .put()/.set()/.get() that look like device calls."""
    tree = ast.parse(src)
    lines = src.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr not in ("put", "set", "get"):
            continue
        if attr == "get":
            base = node.func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in _DICT_GET_OWNERS:
                continue
            # x.get("string-key", ...) is almost always a dict; device reads take no args.
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, str):
                continue
        hits.append((node.lineno, lines[node.lineno - 1].strip()))
    return hits


def _purity_specs():
    """Representative specs covering every axis kind plus heater + manual setup."""
    specs = []
    for ax in _all_axis_specs():
        sp = ExperimentSpec(axes=[ax])
        sp.samples.rows = [{"name": "s1"}]
        specs.append(sp)
    specs.append(interview.seed_spec_from_intake(
        {"geometry": "reflection", "q": "both",
         "varying": ["temperature", "energy", "incidence", "spatial"], "heater": "linkam",
         "align": True, "sample_mode": "manual", "manual_thickness": True},
        [{"name": "a"}, {"name": "b"}]))
    return specs


def test_generated_scripts_are_message_pure():
    offenders = {}
    for sp in _purity_specs():
        src = codegen.render(sp)
        hits = _bare_device_calls(src)
        if hits:
            offenders[sp.scan_name or "spec"] = hits
    assert not offenders, (
        "Generated script contains a bare device .put()/.get()/.set() (plans must be "
        "message-pure — use yield from bps.mv / bps.rd):\n"
        + "\n".join("  {}:{}  {}".format(n, ln, code)
                    for n, hits in offenders.items() for ln, code in hits))


# ---------------------------------------------------------------------------
# dry-run (needs bluesky + smi_plans)
# ---------------------------------------------------------------------------
def test_dryrun_one_run_per_sample():
    pytest.importorskip("bluesky")
    pytest.importorskip("ophyd")
    from smi_acquire import dryrun
    sp = interview.seed_spec_from_intake(
        {"geometry": "reflection", "q": "both", "varying": ["temperature", "incidence"],
         "heater": "linkam", "align": True, "sample_mode": "bar"},
        [{"name": "s1", "incident_angles": [0.1, 0.2]},
         {"name": "s2", "incident_angles": [0.1, 0.2]}])
    rep = dryrun.dry_run(sp)
    if rep.error and "smi_plans not importable" in rep.error:
        pytest.skip("smi_plans not available")
    assert rep.ok, rep.error
    assert rep.runs == sp.n_samples()
    assert rep.events == sp.total_events()
