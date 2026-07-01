"""
Phase 6a: the stacked SampleStage exposes the full piezo + Huber axis set and captures all of
them into a Position. Uses a fake EpicsMotor (no live EPICS) so it runs in CI.
"""

from __future__ import annotations

import types

import pytest


class _FakeMotor:
    """Minimal stand-in for ophyd.EpicsMotor used by SampleStage.from_config."""
    _registry: dict = {}

    def __init__(self, pv, name=None):
        self.pv = pv
        self.name = name
        self.position = 0.0
        _FakeMotor._registry[pv] = self

    def wait_for_connection(self, timeout=5.0):
        return True


@pytest.fixture
def stage(monkeypatch):
    """Build a SampleStage from the bundled config with EpicsMotor patched to a fake."""
    from smi_acquire.microscope import devices as dev
    monkeypatch.setattr(dev, "EpicsMotor", _FakeMotor)
    _FakeMotor._registry.clear()

    # A config with the full stacked stage (mirrors config/microscope.yaml).
    epics = types.SimpleNamespace(
        motors={"x": "PV:pzX", "y": "PV:pzY", "z": "PV:pzZ"},
        piezo_motors={"x": "PV:pzX", "y": "PV:pzY", "z": "PV:pzZ",
                      "th": "PV:pzTH", "chi": "PV:pzCHI"},
        stage_motors={"x": "PV:hX", "y": "PV:hY", "z": "PV:hZ",
                      "theta": "PV:hTHETA", "chi": "PV:hCHI", "phi": "PV:hPHI"},
    )
    return dev.SampleStage.from_config(epics, name="stage")


def test_primary_axes_present(stage):
    assert stage.x.pv == "PV:pzX"
    assert stage.y.pv == "PV:pzY"
    assert stage.z.pv == "PV:pzZ"


def test_full_axis_set(stage):
    fields = set(stage.all_axes())
    assert fields == {
        "piezo_x", "piezo_y", "piezo_z", "piezo_th", "piezo_chi",
        "stage_x", "stage_y", "stage_z", "stage_theta", "stage_chi", "stage_phi",
    }


def test_primary_and_piezo_share_one_motor_object(stage):
    """The primary x and piezo x must be the SAME EpicsMotor (one CA channel, not two)."""
    assert stage.all_axes()["piezo_x"] is stage.x
    assert stage.piezo.x is stage.x


def test_read_all_axes_captures_positions(stage):
    stage.piezo.x.position = 2.5
    stage.piezo.chi.position = 1.2
    stage.huber.phi.position = 15.0
    stage.huber.z.position = -3.0
    cap = stage.read_all_axes()
    assert cap["piezo_x"] == 2.5
    assert cap["piezo_chi"] == 1.2
    assert cap["stage_phi"] == 15.0
    assert cap["stage_z"] == -3.0


def test_position_from_captured_axes(stage):
    from smi_acquire.store import AcquireStore
    stage.piezo.x.position = 2.5
    stage.huber.phi.position = 15.0
    pos = AcquireStore.position_from_axes(stage.read_all_axes())
    assert pos.piezo_x == 2.5
    assert pos.stage_phi == 15.0
    assert pos.frame == "holder"


def test_minimal_config_falls_back_to_xyz(monkeypatch):
    """With only primary motors configured, all_axes maps x/y/z onto piezo_*."""
    from smi_acquire.microscope import devices as dev
    monkeypatch.setattr(dev, "EpicsMotor", _FakeMotor)
    _FakeMotor._registry.clear()
    epics = types.SimpleNamespace(
        motors={"x": "PV:x", "y": "PV:y", "z": "PV:z"},
        piezo_motors={}, stage_motors={})
    st = dev.SampleStage.from_config(epics, name="stage")
    assert set(st.all_axes()) == {"piezo_x", "piezo_y", "piezo_z"}
    assert st.piezo is None and st.huber is None


def test_dev_config_pvs_are_published_by_fake_ioc():
    """The safe dev microscope config must only reference PVs from the bundled fake IOC."""
    from smi_acquire.microscope.config import load_config
    from smi_acquire.sim.fake_ioc import SwaxsSimIOC

    cfg = load_config("config/microscope.yaml")
    ioc = SwaxsSimIOC(prefix="SWAXS:SIM:")
    published = set(ioc.pvdb)

    expected = [
        cfg.epics.camera_prefix + cfg.epics.cam_suffix + suffix
        for suffix in ("ColorMode_RBV", "AcquireTime", "AcquireTime_RBV")
    ]
    expected += [
        cfg.epics.camera_prefix + "image1:" + suffix
        for suffix in ("ArraySize0_RBV", "ArraySize1_RBV", "ArraySize2_RBV", "ArrayData")
    ]
    expected += list(cfg.epics.motors.values())
    expected += list(cfg.epics.piezo_motors.values())
    expected += list(cfg.epics.stage_motors.values())

    assert sorted(set(expected) - published) == []
