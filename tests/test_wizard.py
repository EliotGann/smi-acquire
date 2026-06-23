"""Tests for the headless scan-building wizard model (no Panel/bluesky)."""

from __future__ import annotations

from smi_acquire import wizard
from smi_acquire.wizard import WizardState
from smi_acquire.project import Experiment
from smi_acquire.spec import SPEED_SLOW, SPEED_FAST


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------
def test_steps_in_order():
    assert wizard.STEPS == ["measure", "change", "configure", "compose", "review"]


def test_next_back_navigation():
    st = WizardState()
    assert st.step == "measure"
    st.next()
    assert st.step == "change"
    st.next()
    assert st.step == "configure"
    st.back()
    assert st.step == "change"
    st.goto(4)
    assert st.step == "review"
    st.goto(99)
    assert st.step == "review"   # clamped
    st.goto(-5)
    assert st.step == "measure"


def test_measure_step_requires_geometry_and_q():
    st = WizardState(geometry="", q="")
    assert not st.can_advance()
    st.geometry, st.q = "transmission", "both"
    assert st.can_advance()


# ---------------------------------------------------------------------------
# changes (scan axes) — toggle, order, apparatus implications
# ---------------------------------------------------------------------------
def test_add_change_orders_slow_outermost():
    st = WizardState()
    st.add_change("spatial")       # fast
    st.add_change("temperature")   # slow
    st.add_change("energy")        # medium
    # slow (temperature) outermost, fast (spatial) innermost
    assert st.axes[0].type == "temperature"
    assert st.axes[-1].type == "spatial"


def test_toggle_change_on_off():
    st = WizardState()
    assert st.toggle_change("energy") is True
    assert st.has_change("energy")
    assert st.toggle_change("energy") is False
    assert not st.has_change("energy")


def test_temperature_change_sets_heater():
    st = WizardState()
    st.add_change("temperature")
    assert st.heater == "linkam"
    st.remove_change("temperature")
    assert st.heater is None


def test_move_axis_reorders():
    st = WizardState()
    st.add_change("temperature")
    st.add_change("energy")
    order0 = [a.type for a in st.axes]
    st.move_axis(0, 1)
    assert [a.type for a in st.axes] != order0
    st.auto_order()
    assert st.axes[0].type == "temperature"   # slow back to outermost


# ---------------------------------------------------------------------------
# derived spec
# ---------------------------------------------------------------------------
def test_beam_spec_from_q():
    st = WizardState(q="saxs")
    bs = st.beam_spec()
    assert bs.detectors == ["pil2M"]
    assert bs.arc_aware is False
    assert "pin_diode" in bs.reads

    st2 = WizardState(q="both")
    assert st2.beam_spec().arc_aware is True
    assert set(st2.beam_spec().detectors) == {"pil2M", "pil900KW"}


def test_apparatus_reflection_alignment():
    st = WizardState(geometry="reflection", align_routine="alignement_gisaxs_hex",
                     align_angle=0.15)
    ap = st.apparatus_spec()
    assert ap.geometry == "reflection"
    assert ap.align_routine == "alignement_gisaxs_hex"
    assert ap.align_angle == 0.15


def test_to_spec_events_and_warnings():
    st = WizardState(geometry="reflection")
    st.add_change("temperature")   # 3 setpoints default
    st.add_change("incidence")     # 2 angles default
    spec = st.to_spec()
    assert spec.events_per_sample() == 3 * 2
    # correctly ordered (slow outer) -> no warnings
    assert st.order_warnings() == []


def test_to_experiment_and_target():
    st = WizardState(geometry="reflection")
    st.add_change("temperature")
    st.target_kind = "holder"
    st.target_holder_id = "holder123"
    exp = st.to_experiment(name="my exp")
    assert isinstance(exp, Experiment)
    assert exp.target.kind == "holder"
    assert exp.target.holder_id == "holder123"
    assert [a.type for a in exp.axes] == ["temperature"]


def test_project_name_flows_to_experiment_and_acquire_md():
    """Regression: project_name must reach the acquire run's md (was dropped via Experiment)."""
    from smi_acquire.spec import ExperimentSpec
    from smi_acquire import codegen

    st = WizardState(geometry="transmission", project_name="311234_Doe")
    st.add_change("time")
    exp = Experiment(name="e")
    st.apply_to_experiment(exp)
    assert exp.project_name == "311234_Doe"

    # to_spec on the experiment carries it (even with an empty Project-name fallback)
    spec = exp.to_spec([], project_name="")  # no samples -> placeholder row in codegen
    assert isinstance(spec, ExperimentSpec)
    assert spec.project_name == "311234_Doe"

    src = codegen.render(spec)
    # the acquire/acquire_bar call is wrapped in run_plan() (so det_exposure_time can be
    # yield-from'd); the project_name lands in its md
    assert "md={'project_name': '311234_Doe'}" in src
    call = [ln for ln in src.splitlines() if "acquire" in ln and "md={'project_name'" in ln][0]
    assert "md={'project_name': '311234_Doe'}" in call


def test_experiment_project_name_json_roundtrip():
    exp = Experiment(name="e", project_name="P1")
    back = Experiment.from_dict(exp.to_dict())
    assert back.project_name == "P1"


def test_from_experiment_reads_project_name():
    exp = Experiment(name="e", project_name="P2")
    st = WizardState.from_experiment(exp)
    assert st.project_name == "P2"


def test_round_trip_from_experiment():
    st = WizardState(geometry="reflection", q="saxs", exposure_s=2.0)
    st.add_change("energy")
    st.add_change("temperature")
    exp = st.to_experiment(name="rt")
    st2 = WizardState.from_experiment(exp)
    assert st2.geometry == "reflection"
    assert st2.q == "saxs"
    assert st2.exposure_s == 2.0
    assert {a.type for a in st2.axes} == {"energy", "temperature"}


# ---------------------------------------------------------------------------
# registry visuals (icons/colors present for every changeable)
# ---------------------------------------------------------------------------
def test_every_changeable_has_icon_and_color():
    for kind in wizard.changeables():
        assert kind.icon and kind.icon != ""
        assert kind.color.startswith("#")


def test_speed_ordering_constants_used():
    # temperature is slow, spatial is fast (sanity on the registry speeds)
    assert wizard.axis_kind("temperature").speed == SPEED_SLOW
    assert wizard.axis_kind("spatial").speed == SPEED_FAST
