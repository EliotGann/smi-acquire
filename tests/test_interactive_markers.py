"""Tests for on-axis marker projection behavior."""

from __future__ import annotations

from bokeh.plotting import figure

from smi_acquire.microscope.calibration import CalibrationModel
from smi_acquire.microscope.config import AppConfig
from smi_acquire.microscope.modes.interactive import InteractiveMode
from smi_acquire.microscope.overlays import BeamOverlay
from smi_acquire.microscope.scripts import Bookmark


class _Motor:
    def __init__(self, position=0.0):
        self.position = position


class _Stage:
    def __init__(self):
        self.x = _Motor(0.0)
        self.y = _Motor(0.0)
        self.z = _Motor(0.0)

    def chi_for_stack(self, _stack):
        return 0.0


def _mode(map_active=False):
    cfg = AppConfig.model_validate({
        "epics": {"camera_prefix": "SIM:", "motors": {"x": "x", "y": "y", "z": "z"}}
    })
    return InteractiveMode(
        figure(width=200, height=150),
        _Stage(),
        BeamOverlay((10, 10), 4, 2),
        CalibrationModel([[10, 0], [0, 10]]),
        cfg,
        lambda: (20, 20),
        map_active_provider=lambda: map_active,
    )


def test_off_frame_markers_are_hidden_without_map_background():
    mode = _mode(map_active=False)
    mode.set_samples([Bookmark("far", x=-10, y=0, z=0)])
    assert mode._markers_cds.data["name"] == []


def test_off_frame_markers_persist_when_map_background_active():
    mode = _mode(map_active=True)
    mode.set_samples([Bookmark("far", x=-10, y=0, z=0)])
    assert mode._markers_cds.data["name"] == ["far"]
    assert mode._markers_cds.data["x"][0] == 110.0


def test_on_axis_out_of_plane_direction_uses_z_delta():
    mode = _mode(map_active=True)
    mode.stage.z.position = 1.0
    mode.set_samples([Bookmark("above", x=0, y=0, z=2), Bookmark("below", x=0, y=0, z=0)])
    assert mode._oop_cds.data["marker"] == ["triangle", "inverted_triangle"]
    assert mode._oop_cds.data["color"] == ["orange", "deepskyblue"]
