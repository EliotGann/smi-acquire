import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from smi_acquire import samples, techniques, guidance, codegen  # noqa: E402


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------
def _bar():
    recs = [
        {"name": "s1", "piezo_x": -56000, "piezo_y": 4000, "incident_angles": "0.1 0.2",
         "md": '{"project_name": "demo"}'},
        {"name": "s2", "piezo_x": -45000, "piezo_y": 4000, "incident_angles": "0.1 0.2",
         "md": ""},
    ]
    return samples.records_to_samples(recs)


def test_records_roundtrip():
    bar = _bar()
    assert len(bar) == 2
    assert bar[0].piezo_x == -56000.0
    assert bar[0].incident_angles == [0.1, 0.2]
    assert bar[0].md["project_name"] == "demo"
    # round-trip back to records preserves names
    recs = samples.samples_to_records(bar)
    assert [r["name"] for r in recs] == ["s1", "s2"]


def test_records_to_samples_skips_blank_names():
    bar = samples.records_to_samples([{"name": ""}, {"name": "ok", "piezo_x": 1}])
    assert [s.name for s in bar] == ["ok"]


def test_duplicate_names_rejected():
    with pytest.raises(ValueError):
        samples.records_to_samples([{"name": "x"}, {"name": "x"}])


# ---------------------------------------------------------------------------
# Technique registry
# ---------------------------------------------------------------------------
def test_every_bar_technique_renders_a_call():
    for letter, spec in techniques.TECHNIQUES.items():
        call = spec.render_call(spec.defaults())
        assert call.startswith("{}.{}(".format(spec.alias, spec.entry))
        # the sample variable must be the first positional argument
        assert "(bar" in call


def test_paramspec_rendering():
    p = techniques.ParamSpec("waxs_arc", "arc", "floats", [0, 20])
    assert p.render() == "[0, 20]"
    p2 = techniques.ParamSpec("g", "g", "choice", "transmission")
    assert p2.render() == "'transmission'"
    p3 = techniques.ParamSpec("a", "a", "token", "alignement_gisaxs_hex")
    assert p3.render() == "alignement_gisaxs_hex"
    p4 = techniques.ParamSpec("d", "d", "optfloat", None)
    assert p4.render() == "None"
    assert p4.render(30) == "30.0"
    p5 = techniques.ParamSpec("r", "r", "tuple", (-60, 60, 121))
    assert p5.render() == "(-60, 60, 121)"


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------
def test_generate_script_A_runs_compile():
    script = codegen.generate_script(_bar(), "A", {"edge": 2822.0, "t": 1.0})
    assert "from smi_plans import technique_A_energy_edge as A" in script
    assert "energies = A.energy_grid(2822" in script
    assert "RE(A.nexafs_bar(bar, energies" in script
    compile(script, "<A>", "exec")  # generated script must be valid Python


def test_generate_script_B_arc_economy_switch():
    base = codegen.generate_script(_bar(), "B", {"arc_economy": False})
    eco = codegen.generate_script(_bar(), "B", {"arc_economy": True})
    assert "giwaxs_bar(" in base
    assert "giwaxs_bar_arc_economy(" in eco
    compile(eco, "<B>", "exec")


def test_generate_script_all_letters_compile():
    bar = _bar()
    for letter in techniques.all_letters():
        script = codegen.generate_script(bar, letter)
        compile(script, "<{}>".format(letter), "exec")


def test_samplelist_block_emits_used_columns_only():
    block = codegen.render_samplelist(_bar())
    assert "names=['s1', 's2']" in block
    assert "piezo_x=" in block
    assert "piezo_z" not in block          # never set -> not emitted
    assert "incident_angles=[0.1, 0.2]" in block   # shared -> single list


def test_queueserver_item_shape():
    item = codegen.to_queueserver_item(_bar(), "A", {"t": 2.0})
    assert item["name"] == "nexafs_bar"
    assert item["item_type"] == "plan"
    assert isinstance(item["kwargs"]["samples"], list)
    assert item["kwargs"]["t"] == 2.0


# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------
def test_recommend_energy_edge():
    recs = guidance.recommend({"control_variable": "photon_energy",
                               "geometry": "transmission"})
    assert recs[0]["letter"] == "A"
    assert recs[0]["reasons"]


def test_recommend_specialty_overrides():
    recs = guidance.recommend({"control_variable": "rotation",
                               "specialty": ["cd_metrology"]})
    assert recs[0]["letter"] == "I"


def test_recommend_keywords():
    recs = guidance.recommend({}, keywords="humidity swelling")
    assert any(r["letter"] == "G" for r in recs)
