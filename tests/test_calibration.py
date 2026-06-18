"""
Phase 6d: rotation-aware click-to-move calibration + the stage's chi geometry.

The piezo/Huber x/y motor frame rotates with the SMI ``chi`` axis as seen by the camera; the
calibration is fit at chi = 0, so click-to-move rotation-corrects for the live chi.  These tests
pin the geometry (no hardware).
"""

from __future__ import annotations

import types

import numpy as np
import pytest

from smi_acquire.microscope.calibration import CalibrationModel, _rot


# ---------------------------------------------------------------------------
# rotation matrix
# ---------------------------------------------------------------------------
def test_rot_identity_at_zero():
    assert np.allclose(_rot(0.0), np.eye(2))


def test_rot_90_degrees():
    # active rotation by +90°: (1,0) -> (0,1)
    v = _rot(90.0) @ np.array([1.0, 0.0])
    assert np.allclose(v, [0.0, 1.0], atol=1e-9)


# ---------------------------------------------------------------------------
# click_to_motor_delta — chi = 0 reduces to the plain affine inverse
# ---------------------------------------------------------------------------
def test_click_zero_chi_matches_plain_inverse():
    A = [[-100.0, 0.0], [0.0, -100.0]]
    cal = CalibrationModel(A)
    click, beam = (300.0, 200.0), (320.0, 240.0)
    got = cal.click_to_motor_delta(click, beam, chi_deg=0.0)
    expect = cal.pixel_to_motor_delta((beam[0] - click[0], beam[1] - click[1]))
    assert np.allclose(got, expect)


# ---------------------------------------------------------------------------
# the physical correctness property: applying the motor delta in the ROTATED
# frame must produce exactly the requested pixel shift (beam - click).
# Rotated forward map is A @ R(chi); click_to_motor_delta returns R(-chi) @ A_inv @ dp,
# so A @ R(chi) @ dm == dp for any chi.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("chi", [0.0, 5.0, -12.5, 30.0, 90.0, 175.0])
def test_rotated_frame_round_trip(chi):
    A = np.array([[-0.1381, -0.00315], [0.0030, -0.13845]])
    cal = CalibrationModel(A)
    click, beam = (412.0, 510.0), (516.0, 336.0)
    dp = np.array([beam[0] - click[0], beam[1] - click[1]])
    dm = cal.click_to_motor_delta(click, beam, chi_deg=chi)
    # the camera sees the motor move through the rotated frame: A @ R(chi) @ dm
    produced_dp = A @ (_rot(chi) @ dm)
    assert np.allclose(produced_dp, dp, atol=1e-6)


def test_nonzero_chi_changes_the_delta():
    cal = CalibrationModel([[-100.0, 0.0], [0.0, -100.0]])
    click, beam = (300.0, 200.0), (320.0, 240.0)
    d0 = cal.click_to_motor_delta(click, beam, chi_deg=0.0)
    d90 = cal.click_to_motor_delta(click, beam, chi_deg=90.0)
    assert not np.allclose(d0, d90)
    # at 90°, R(-90): (dx,dy) -> (dy,-dx)
    assert np.allclose(d90, _rot(-90.0) @ d0, atol=1e-9)


# ---------------------------------------------------------------------------
# SampleStage.chi_for_stack geometry
# ---------------------------------------------------------------------------
class _FakeMotor:
    _registry: dict = {}

    def __init__(self, pv, name=None):
        self.pv = pv
        self.name = name
        self.position = 0.0
        _FakeMotor._registry[pv] = self

    def wait_for_connection(self, timeout=5.0):
        return True


def _full_stage(monkeypatch):
    from smi_acquire.microscope import devices as dev
    monkeypatch.setattr(dev, "EpicsMotor", _FakeMotor)
    _FakeMotor._registry.clear()
    epics = types.SimpleNamespace(
        motors={"x": "PV:pzX", "y": "PV:pzY", "z": "PV:pzZ"},
        piezo_motors={"x": "PV:pzX", "y": "PV:pzY", "z": "PV:pzZ",
                      "th": "PV:pzTH", "chi": "PV:pzCHI"},
        stage_motors={"x": "PV:hX", "y": "PV:hY", "z": "PV:hZ",
                      "theta": "PV:hTHETA", "chi": "PV:hCHI", "phi": "PV:hPHI"},
    )
    return dev.SampleStage.from_config(epics, name="stage")


def test_chi_for_piezo_sums_huber_and_piezo_chi(monkeypatch):
    stage = _full_stage(monkeypatch)
    stage.huber.chi.position = 3.0
    stage.piezo.chi.position = 1.5
    assert stage.chi_for_stack("piezo") == pytest.approx(4.5)


def test_chi_for_huber_uses_only_huber_chi(monkeypatch):
    stage = _full_stage(monkeypatch)
    stage.huber.chi.position = 3.0
    stage.piezo.chi.position = 1.5
    assert stage.chi_for_stack("huber") == pytest.approx(3.0)


def test_chi_zero_when_axes_absent(monkeypatch):
    from smi_acquire.microscope import devices as dev
    monkeypatch.setattr(dev, "EpicsMotor", _FakeMotor)
    _FakeMotor._registry.clear()
    epics = types.SimpleNamespace(
        motors={"x": "PV:x", "y": "PV:y", "z": "PV:z"},
        piezo_motors={}, stage_motors={})
    stage = dev.SampleStage.from_config(epics, name="stage")
    assert stage.chi_for_stack("piezo") == 0.0
    assert stage.chi_for_stack("huber") == 0.0
