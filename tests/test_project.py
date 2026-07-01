"""
Tests for the local Project recipe model + the shared-store boundary.

Samples/holders now live in the redis db=2 ``SampleStore`` (here driven OFFLINE, in-memory);
the local :class:`Project` holds experiment *recipes* (which target a holder / all samples) and
reference fiducials.  Covers target resolution against the store, per-experiment codegen /
dry-run, JSON round-trip, and the recipe seed.
"""

from __future__ import annotations

import ast

import pytest

from smi_acquire import interview, codegen
from smi_acquire.project import Project, Experiment, Target, Reference
from smi_acquire.store import AcquireStore
from smi_plans import Position


def _demo_store() -> AcquireStore:
    """An offline store with two holders (hot/cold) and three samples (one unpositioned)."""
    acq = AcquireStore.connect(offline=True)
    hot = acq.ensure_holder("hot")
    cold = acq.ensure_holder("cold")
    acq.add_sample("A", holder_id=hot.id,
                   nominal=Position(frame="holder", piezo_x=1.0, piezo_y=2.0, piezo_z=0.0),
                   incident_angles=[0.1, 0.2], md={"polymer": "P3HT"})
    acq.add_sample("B", holder_id=cold.id,
                   nominal=Position(frame="holder", piezo_x=-3.0, piezo_y=4.0, piezo_z=0.5))
    acq.add_sample("C", holder_id=hot.id)   # no position yet
    return acq


# ---------------------------------------------------------------------------
# store basics (holders + samples + capture)
# ---------------------------------------------------------------------------
def test_add_sample_and_capture_position():
    acq = AcquireStore.connect(offline=True)
    pos = AcquireStore.position_from_axes({"x": 10.0, "y": 20.0, "z": 0.0})
    s = acq.add_sample("S1", nominal=pos)
    assert acq.sample_by_id(s.id).nominal.piezo_x == 10.0
    acq.assign_nominal(s.id, AcquireStore.position_from_axes({"x": 11.0, "y": 21.0, "z": 1.0}))
    assert acq.sample_by_id(s.id).nominal.piezo_x == 11.0


def test_offline_env_does_not_touch_sample_store_redis(monkeypatch):
    """The dev safety env must force an in-memory store, not probe the beamline Redis."""
    monkeypatch.setenv("SMI_ACQUIRE_OFFLINE", "1")
    monkeypatch.setattr("smi_acquire.store.SampleStore.from_redis",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("redis touched")))
    acq = AcquireStore.connect()
    assert not acq.live
    assert acq.location == "offline (in-memory)"


def test_holder_membership():
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    names = {s.name for s in acq.list_samples(holder_id=hot.id)}
    assert names == {"A", "C"}


def test_move_sample_between_holders():
    acq = _demo_store()
    cold = acq.holder_by_name("cold")
    a = next(s for s in acq.list_samples() if s.name == "A")
    acq.set_sample_holder(a.id, cold.id)
    assert {s.name for s in acq.list_samples(holder_id=cold.id)} == {"A", "B"}


# ---------------------------------------------------------------------------
# experiments target holders (resolved against the store)
# ---------------------------------------------------------------------------
def test_experiment_targets_holder():
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    exp = Experiment(name="anneal", axes=[interview.default_axis("temperature")],
                     target=Target(kind="holder", holder_id=hot.id))
    proj = Project()
    proj.experiments.append(exp)
    resolved = proj.resolve_target(exp, acq)
    assert {s.name for s in resolved} == {"A", "C"}


def test_resolve_target_orders_by_priority():
    """resolve_target returns samples sorted by md['priority'] (lower runs first; default 0)."""
    acq = _demo_store()                       # samples A, B, C
    def _set_pri(name, pri):
        s = next(x for x in acq.list_samples() if x.name == name)
        s.md = dict(s.md or {})
        s.md["priority"] = pri
        acq.update_sample(s)
    _set_pri("C", 1)
    _set_pri("A", 2)
    _set_pri("B", 3)
    proj = Project()
    exp = Experiment(name="e", target=Target(kind="all"))
    assert [s.name for s in proj.resolve_target(exp, acq)] == ["C", "A", "B"]
    # the generated spec's sample rows follow that order
    spec = proj.experiment_spec(exp, acq)
    assert [r["name"] for r in spec.samples.rows] == ["C", "A", "B"]


def test_resolve_target_default_priority_is_stable():
    """With no priority set (all default 0), order is the store's (stable, not reordered)."""
    acq = _demo_store()
    proj = Project()
    exp = Experiment(name="e", target=Target(kind="all"))
    got = [s.name for s in proj.resolve_target(exp, acq)]
    assert set(got) == {"A", "B", "C"}        # all present, no crash on missing priority


def test_experiment_to_spec_builds_rows_from_positions():
    acq = _demo_store()
    a = next(s for s in acq.list_samples() if s.name == "A")
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="samples", sample_ids=[a.id]))
    proj = Project()
    spec = proj.experiment_spec(exp, acq)
    assert spec.samples.rows[0]["name"] == "A"
    assert spec.samples.rows[0]["piezo_x"] == 1.0
    assert spec.samples.rows[0]["incident_angles"] == [0.1, 0.2]


def test_render_experiment_compiles():
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    exp = Experiment(name="e", scan_name="anneal",
                     axes=[interview.default_axis("temperature"),
                           interview.default_axis("incidence")],
                     target=Target(kind="holder", holder_id=hot.id))
    exp.apparatus.heater = "linkam"
    exp.apparatus.geometry = "reflection"
    proj = Project()
    src = codegen.render_experiment(proj, exp, acq)
    ast.parse(src)
    assert "acquire_bar(" in src  # A and C → multi-sample


# ---------------------------------------------------------------------------
# Redis-first samples: load_holder when all samples share one holder
# ---------------------------------------------------------------------------
def test_holder_target_emits_load_holder():
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="holder", holder_id=hot.id))
    proj = Project(name="proj1")
    spec = proj.experiment_spec(exp, acq)
    assert spec.samples.source == "holder"
    assert spec.samples.holder == "hot"
    src = codegen.render(spec)
    assert 'load_holder(\'hot\')' in src
    assert "from smi_plans import load_holder" in src
    assert "from_columns" not in src
    ast.parse(src)


def test_mixed_holders_fall_back_to_from_columns():
    """A 'samples' target spanning two holders can't load_holder -> from_columns fallback."""
    acq = _demo_store()                          # A,C on hot; B on cold
    ids = [s.id for s in acq.list_samples() if s.name in ("A", "B")]   # spans hot+cold
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="samples", sample_ids=ids))
    proj = Project(name="proj1")
    spec = proj.experiment_spec(exp, acq)
    assert spec.samples.source == "inline"
    src = codegen.render(spec)
    assert "from_columns" in src and "load_holder" not in src
    ast.parse(src)


def test_dryrun_render_uses_from_columns_even_for_holder():
    """The dry-run render must be store-free: from_columns even when the script uses load_holder."""
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="holder", holder_id=hot.id))
    proj = Project(name="proj1")
    spec = proj.experiment_spec(exp, acq)
    dsrc = codegen.render(spec, for_dryrun=True)
    assert "from_columns" in dsrc and "load_holder" not in dsrc
    ast.parse(dsrc)


def test_per_sample_project_name_flows_to_rows():
    """project_name varies per sample (Sample.md) and rides each row / the run md fallback."""
    acq = AcquireStore.connect(offline=True)
    h = acq.ensure_holder("bar1")
    acq.add_sample("A", holder_id=h.id, nominal=Position(frame="holder", piezo_x=1.0),
                   md={"project_name": "proj_A"})
    acq.add_sample("B", holder_id=h.id, nominal=Position(frame="holder", piezo_x=2.0))  # no project
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="holder", holder_id=h.id))
    proj = Project(name="fallback_proj")
    spec = proj.experiment_spec(exp, acq)
    # per-sample project list: A explicit, B falls back to the project name
    assert spec.samples.project_names == ["proj_A", "fallback_proj"]
    # dry-run (from_columns) carries per-sample project in row md
    dsrc = codegen.render(spec, for_dryrun=True)
    assert "proj_A" in dsrc and "fallback_proj" in dsrc
    ast.parse(dsrc)


def test_default_holder_makes_samples_load_holder_able():
    """A blank sample lands on the default holder, so a single-holder bar references it by name."""
    acq = AcquireStore.connect(offline=True)
    s = acq.add_sample("solo")                    # no holder given -> default holder
    assert s.holder_id is not None
    exp = Experiment(name="e", axes=[interview.default_axis("energy")],
                     target=Target(kind="all"))
    spec = Project().experiment_spec(exp, acq)
    assert spec.samples.source == "holder"        # all samples share the default holder
    ast.parse(codegen.render(spec))


def test_experiment_from_spec_roundtrips_recipe():
    spec = interview.seed_spec_from_intake(
        {"geometry": "reflection", "varying": ["temperature", "energy"], "heater": "linkam"})
    exp = Experiment.from_spec(spec, name="seeded")
    assert [a.type for a in exp.axes] == [a.type for a in spec.axes]
    assert exp.apparatus.heater == "linkam"


# ---------------------------------------------------------------------------
# round-trips
# ---------------------------------------------------------------------------
def test_project_json_roundtrip():
    proj = Project(name="311234_Doe")
    proj.references.append(Reference(name="f1", x=0.0, y=0.0, z=0.0))
    proj.experiments.append(Experiment(name="e", axes=[interview.default_axis("energy")],
                                        target=Target(kind="all")))
    d = proj.to_dict()
    assert Project.from_dict(d).to_dict() == d


def test_target_backcompat_set_to_holder():
    """An old project.json targeting a sample-'set' loads as a holder target."""
    t = Target.from_dict({"kind": "set", "set_id": "abc123"})
    assert t.kind == "holder" and t.holder_id == "abc123"


# ---------------------------------------------------------------------------
# dry-run per experiment (needs bluesky + smi_plans)
# ---------------------------------------------------------------------------
def test_dry_run_experiment_one_run_per_targeted_sample():
    pytest.importorskip("bluesky")
    from smi_acquire import dryrun
    acq = _demo_store()
    hot = acq.holder_by_name("hot")
    exp = Experiment(name="e", scan_name="anneal",
                     axes=[interview.default_axis("incidence")],
                     target=Target(kind="holder", holder_id=hot.id))
    exp.apparatus.geometry = "reflection"
    proj = Project()
    rep = dryrun.dry_run_experiment(proj, exp, acq)
    if rep.error and "smi_plans not importable" in rep.error:
        pytest.skip("smi_plans not available")
    assert rep.ok, rep.error
    assert rep.runs == 2     # samples A and C are in 'hot'
