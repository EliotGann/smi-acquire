"""
Tests for the Project data model — the sample-list spine, sample-sets, experiments-target-sets,
JSON + spreadsheet round-trip, and per-experiment codegen / dry-run.
"""

from __future__ import annotations

import ast

import pytest

from smi_acquire import interview, codegen
from smi_acquire.project import (Project, Sample, Position, Experiment, Target, Bookmark)


def _demo_project() -> Project:
    proj = Project(name="311234_Doe")
    hot = proj.ensure_set("hot")
    cold = proj.ensure_set("cold")
    proj.samples += [
        Sample(name="A", position=Position(1.0, 2.0, 0.0), incident_angles=[0.1, 0.2],
               metadata={"polymer": "P3HT"}, set_ids=[hot.id]),
        Sample(name="B", position=Position(-3.0, 4.0, 0.5), set_ids=[cold.id]),
        Sample(name="C", position=Position(), set_ids=[hot.id]),  # no position yet
    ]
    return proj


# ---------------------------------------------------------------------------
# spine basics
# ---------------------------------------------------------------------------
def test_new_sample_and_assign_position():
    proj = Project()
    s = proj.new_sample_from("S1", Position(10, 20, 0))
    assert s.has_position() and proj.sample_by_id(s.id) is s
    proj.assign_position(s.id, Position(11, 21, 1))
    assert proj.sample_by_id(s.id).position.x == 11


def test_visible_markers_include_positioned_samples_and_refs():
    proj = _demo_project()
    proj.references.append(Bookmark(name="fiducial", position=Position(0, 0, 0)))
    markers = proj.visible_markers()
    names = {m.name for m in markers}
    assert "A" in names and "B" in names      # positioned + visible
    assert "C" not in names                    # no position
    assert "fiducial" in names                 # reference


def test_hidden_set_hides_its_samples():
    proj = _demo_project()
    proj.set_by_name("hot").visible = False
    names = {m.name for m in proj.visible_markers()}
    assert "A" not in names and "B" in names


# ---------------------------------------------------------------------------
# experiments target sample-sets
# ---------------------------------------------------------------------------
def test_experiment_targets_set():
    proj = _demo_project()
    hot = proj.set_by_name("hot")
    exp = Experiment(name="anneal", axes=[interview.default_axis("temperature")],
                     target=Target(kind="set", set_id=hot.id))
    proj.experiments.append(exp)
    resolved = proj.resolve_target(exp)
    assert {s.name for s in resolved} == {"A", "C"}


def test_experiment_to_spec_builds_rows_from_positions():
    proj = _demo_project()
    exp = Experiment(name="e", axes=[interview.default_axis("incidence")],
                     target=Target(kind="samples", sample_ids=[proj.samples[0].id]))
    spec = proj.experiment_spec(exp)
    assert spec.samples.rows[0]["name"] == "A"
    assert spec.samples.rows[0]["piezo_x"] == 1.0
    assert spec.samples.rows[0]["incident_angles"] == [0.1, 0.2]


def test_render_experiment_compiles():
    proj = _demo_project()
    exp = Experiment(name="e", scan_name="anneal",
                     axes=[interview.default_axis("temperature"),
                           interview.default_axis("incidence")],
                     target=Target(kind="set", set_id=proj.set_by_name("hot").id))
    exp.apparatus.heater = "linkam"
    exp.apparatus.geometry = "reflection"
    src = codegen.render_experiment(proj, exp)
    ast.parse(src)
    assert "acquire_bar(" in src  # A and C → multi-sample


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
    proj = _demo_project()
    proj.references.append(Bookmark(name="f1", position=Position(0, 0, 0)))
    proj.experiments.append(Experiment(name="e", axes=[interview.default_axis("energy")],
                                       target=Target(kind="all")))
    d = proj.to_dict()
    assert Project.from_dict(d).to_dict() == d


def test_project_spreadsheet_roundtrip():
    pytest.importorskip("pandas")
    proj = _demo_project()
    df = proj.to_dataframe()
    assert set(df["name"]) == {"A", "B", "C"}
    assert "md.polymer" in df.columns
    back = Project.from_dataframe(df, name="reimport")
    names = {s.name for s in back.samples}
    assert names == {"A", "B", "C"}
    a = next(s for s in back.samples if s.name == "A")
    assert a.position.x == 1.0 and a.metadata.get("polymer") == "P3HT"
    assert back.set_by_name("hot") is not None    # set membership survived


# ---------------------------------------------------------------------------
# dry-run per experiment (needs bluesky + smi_plans)
# ---------------------------------------------------------------------------
def test_dry_run_experiment_one_run_per_targeted_sample():
    pytest.importorskip("bluesky")
    from smi_acquire import dryrun
    proj = _demo_project()
    exp = Experiment(name="e", scan_name="anneal",
                     axes=[interview.default_axis("incidence")],
                     target=Target(kind="set", set_id=proj.set_by_name("hot").id))
    exp.apparatus.geometry = "reflection"
    rep = dryrun.dry_run_experiment(proj, exp)
    if rep.error and "smi_plans not importable" in rep.error:
        pytest.skip("smi_plans not available")
    assert rep.ok, rep.error
    assert rep.runs == 2     # samples A and C are in 'hot'
