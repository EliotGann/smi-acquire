"""
Headless tests for the interrogation-driven core.

The pure-Python layer (spec / interview / codegen) is tested everywhere; the dry-run tests
require the beamline env (bluesky + smi_plans) and skip cleanly off-beamline.
"""

from __future__ import annotations

import ast

import pytest

from smi_acquire import interview, codegen, registry
from smi_acquire.spec import (AxisSpec, BeamSpec, ExperimentSpec, SamplesSpec,
                              energy_grid_values)


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


def test_energy_boundaries_density_matches_np_arange_chain():
    """boundaries+steps reproduces the np.arange(...)+np.arange(...) sulfur scan exactly."""
    import numpy as np
    vals = energy_grid_values(
        {"boundaries": [2445, 2470, 2480, 2490, 2501], "steps": [5, 0.25, 1, 5]})
    expected = (np.arange(2445, 2470, 5).tolist() + np.arange(2470, 2480, 0.25).tolist()
                + np.arange(2480, 2490, 1).tolist() + np.arange(2490, 2501, 5).tolist())
    assert vals == sorted(set(round(v, 6) for v in expected))


def test_energy_boundaries_half_open_no_double_count():
    # shared boundaries are owned by the next region (visited once); upper bound not visited
    vals = energy_grid_values({"boundaries": [0, 10, 20], "steps": [5, 2]})
    assert vals == [0.0, 5.0, 10.0, 12.0, 14.0, 16.0, 18.0]   # 10 from region 2, 20 excluded


def test_energy_boundaries_via_axisspec_values():
    ax = AxisSpec(type="energy",
                  params={"grid": {"boundaries": [2470, 2476, 2530], "steps": [0.25, 5]}})
    vals = ax.values()
    assert vals[0] == 2470.0
    assert ax.n_points() == len(vals)


def test_energy_updown_there_and_back():
    """updown follows the up-sweep with the reversed down-sweep (ends at start, peak twice)."""
    g = {"boundaries": [2470, 2476, 2480], "steps": [2, 1]}
    up = AxisSpec(type="energy", params={"grid": g}).values()
    ud = AxisSpec(type="energy", params={"grid": g, "updown": True}).values()
    assert ud == up + up[::-1]
    assert len(ud) == 2 * len(up)
    assert ud[0] == ud[-1]                  # ends back at the starting energy
    assert ud[len(up) - 1] == ud[len(up)]   # turnaround (peak) visited twice
    # event count reflects the doubling
    assert AxisSpec(type="energy", params={"grid": g, "updown": True}).n_points() == len(ud)


def test_incidence_range_expands_inclusive():
    ax = AxisSpec(type="incidence", params={"range": [0.1, 0.4, 0.05]})
    assert ax.values() == [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4]
    assert ax.n_points() == 7


def test_explicit_values_beat_range():
    ax = AxisSpec(type="incidence", params={"values": [0.1, 0.2], "range": [0.1, 0.4, 0.05]})
    assert ax.values() == [0.1, 0.2]


def test_range_works_for_any_value_axis():
    # the range shorthand is generic (motor/potential/etc.), not incidence-only
    ax = AxisSpec(type="motor", params={"name": "arc", "device": "waxs",
                                        "range": [0, 20, 5]})
    assert ax.values() == [0, 5, 10, 15, 20]


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


def test_codegen_det_exposure_time_is_yielded_from():
    """det_exposure_time is now a plan: it must be `yield from`'d inside run_plan(), never a
    bare top-level call (which would create an unconsumed generator -> exposure never set)."""
    sp = ExperimentSpec(axes=[interview.default_axis("energy")])
    sp.samples.rows = [{"name": "a"}]
    src = codegen.render(sp)
    assert "yield from det_exposure_time(" in src
    # no bare det_exposure_time statement (every occurrence is a yield-from)
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("det_exposure_time("):
            raise AssertionError("bare det_exposure_time call: " + s)
    assert "def run_plan():" in src
    assert "RE(run_plan())" in src
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
# named lists (Redis-first): a named energy axis references the store by name
# ---------------------------------------------------------------------------
def _named_energy_spec():
    g = {"boundaries": [2470.0, 2472.0, 2476.0], "steps": [1.0, 0.25]}
    sp = ExperimentSpec(axes=[AxisSpec("energy", {"grid": g, "list_name": "Fe_K_XANES"})])
    sp.samples.rows = [{"name": "s1"}]
    return sp


def test_codegen_named_energy_uses_resolve_list():
    src = codegen.render(_named_energy_spec())
    assert "resolve_list('Fe_K_XANES', kind=\"energy\", store=lists)" in src
    assert "from smi_plans import resolve_list, ListStore" in src
    assert "lists = ListStore.from_redis()" in src
    # the literal eV list must NOT be pasted into the energy_axis call
    assert "energy_axis([2470" not in src
    ast.parse(src)


def test_codegen_unnamed_energy_stays_literal():
    g = {"boundaries": [2470.0, 2472.0, 2476.0], "steps": [1.0, 0.25]}
    sp = ExperimentSpec(axes=[AxisSpec("energy", {"grid": g})])   # no list_name
    sp.samples.rows = [{"name": "s1"}]
    src = codegen.render(sp)
    assert "resolve_list(" not in src
    assert "ListStore" not in src
    assert "energy_axis([2470" in src      # literal list inlined
    ast.parse(src)


def test_dryrun_render_inlines_named_list_values():
    """The dry-run render must be store-free: no resolve_list / ListStore, values inlined."""
    src = codegen.render(_named_energy_spec(), for_dryrun=True)
    assert "resolve_list(" not in src
    assert "ListStore" not in src
    assert "energy_axis([2470" in src
    ast.parse(src)


def test_named_energy_dry_runs_without_redis():
    """A spec with a named energy list must dry-run (the validator inlines the values)."""
    from smi_acquire import dryrun
    rep = dryrun.dry_run(_named_energy_spec())
    if rep.error and "smi_plans not importable" in rep.error:
        import pytest
        pytest.skip("smi_plans not importable in this env")
    assert rep.ok, rep.error
    assert rep.runs == 1


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


def test_dryrun_grid_with_waxs_reads_no_collision():
    """The WAXS data-key collision: a grid scan with waxs in reads + pil900KW det must NOT raise
    'Data keys collide' -- acquire de-dups (waxs IS pil900KW.motors). The dry-run sim now
    faithfully reproduces the overlap so this would catch a regression."""
    pytest.importorskip("bluesky")
    pytest.importorskip("ophyd")
    from smi_acquire import dryrun
    sp = ExperimentSpec(
        scan_name="map",
        beam=BeamSpec(arc_aware=True, reads=["energy", "waxs", "xbpm2", "xbpm3"]),
        axes=[AxisSpec(type="spatial",
                       params={"x": [0, 30, 60], "y": [0, 30], "snake": True,
                               "motor_object": "piezo"})],
        samples=SamplesSpec(rows=[{"name": "s1", "piezo_x": 1.0}]))
    rep = dryrun.dry_run(sp)
    if rep.error and "smi_plans not importable" in rep.error:
        pytest.skip("smi_plans not available")
    assert rep.ok, rep.error
    assert rep.runs == 1
    assert rep.events == 6      # 3x2 grid


def test_dryrun_arc_axis_uses_waxs_arc():
    """A WAXS-arc motor axis moves waxs.arc (NOT waxs, which is not movable)."""
    pytest.importorskip("bluesky")
    pytest.importorskip("ophyd")
    from smi_acquire import codegen, dryrun
    sp = ExperimentSpec(
        scan_name="arc", beam=BeamSpec(arc_aware=True),
        axes=[AxisSpec(type="motor",
                       params={"name": "arc", "device": "waxs.arc",
                               "values": [0, 20], "speed": 2})],
        samples=SamplesSpec(rows=[{"name": "s1", "piezo_x": 1.0}]))
    src = codegen.render(sp)
    assert "motor_axis('arc', waxs.arc," in src
    assert "motor_axis('arc', waxs," not in src     # never the bare waxs
    rep = dryrun.dry_run(sp)
    if rep.error and "smi_plans not importable" in rep.error:
        pytest.skip("smi_plans not available")
    assert rep.ok, rep.error
    assert rep.events == 2

