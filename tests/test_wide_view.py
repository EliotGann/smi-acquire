"""Tests for the wide/top x-z microscope view."""

from __future__ import annotations

from smi_acquire.microscope.config import AppConfig
from smi_acquire.microscope.scripts import Bookmark
from smi_acquire.microscope.wide_view import WideCameraView, _alpha_for_distance


class _Motor:
    def __init__(self, pos=0.0):
        self.position = pos

    def set(self, value):
        self.position = float(value)
        return None


class _Stage:
    def __init__(self):
        self.x = _Motor(0.0)
        self.y = _Motor(0.0)
        self.z = _Motor(0.0)


def test_alpha_for_out_of_plane_distance_fades_but_stays_visible():
    assert _alpha_for_distance(0.0, 1.0) > _alpha_for_distance(2.0, 1.0)
    assert _alpha_for_distance(100.0, 1.0) == 0.12


def test_wide_commit_moves_x_and_z(monkeypatch):
    from smi_acquire.microscope import wide_view as mod

    class _Camera:
        @classmethod
        def from_config(cls, *_a, **_k):
            return cls()

    class _Stream:
        def __init__(self, *_a, **_k):
            pass

        def tick(self):
            pass

    monkeypatch.setattr(mod, "Camera", _Camera)
    monkeypatch.setattr(mod, "CameraStream", _Stream)
    stage = _Stage()
    cfg = AppConfig.model_validate({
        "epics": {"camera_prefix": "SIM:", "motors": {"x": "x", "y": "y", "z": "z"}},
        "wide_camera": {
            "center_px": [10, 10],
            "image_size_hint": [20, 20],
            "calibration": [[-10, 0], [0, -10]],
        },
    })
    view = WideCameraView(cfg, stage)
    view._commit(0, 0)
    # click_to_motor_delta target center [10,10] from click [0,0] -> A_inv @ [10,10] = [-1,-1]
    assert stage.z.position == -1.0
    assert stage.x.position == -1.0


def test_wide_markers_use_xz_and_fade_by_y(monkeypatch):
    from smi_acquire.microscope import wide_view as mod

    class _Camera:
        @classmethod
        def from_config(cls, *_a, **_k):
            return cls()

    class _Stream:
        def __init__(self, *_a, **_k):
            pass

        def tick(self):
            pass

    monkeypatch.setattr(mod, "Camera", _Camera)
    monkeypatch.setattr(mod, "CameraStream", _Stream)
    stage = _Stage()
    stage.y.position = 2.0
    cfg = AppConfig.model_validate({
        "epics": {"camera_prefix": "SIM:", "motors": {"x": "x", "y": "y", "z": "z"}},
        "wide_camera": {"center_px": [10, 10], "image_size_hint": [20, 20],
                        "calibration": [[-10, 0], [0, -10]], "out_of_plane_fade": 1.0},
    })
    view = WideCameraView(cfg, stage)
    view.set_samples([Bookmark("a", x=0, y=2, z=0), Bookmark("b", x=0, y=10, z=0)])
    assert view._markers_cds.data["x"] == [10.0, 10.0]
    assert view._markers_cds.data["y"] == [10.0, 10.0]
    assert view._markers_cds.data["alpha"][0] > view._markers_cds.data["alpha"][1]
    assert view._oop_cds.data["marker"] == ["triangle"]
    assert view._oop_cds.data["color"] == ["orange"]


def test_wide_out_of_plane_direction_uses_y_delta(monkeypatch):
    from smi_acquire.microscope import wide_view as mod

    class _Camera:
        @classmethod
        def from_config(cls, *_a, **_k):
            return cls()

    class _Stream:
        def __init__(self, *_a, **_k):
            pass

        def tick(self):
            pass

    monkeypatch.setattr(mod, "Camera", _Camera)
    monkeypatch.setattr(mod, "CameraStream", _Stream)
    stage = _Stage()
    stage.y.position = 1.0
    cfg = AppConfig.model_validate({
        "epics": {"camera_prefix": "SIM:", "motors": {"x": "x", "y": "y", "z": "z"}},
        "wide_camera": {"center_px": [10, 10], "image_size_hint": [20, 20],
                        "calibration": [[-10, 0], [0, -10]], "out_of_plane_fade": 1.0},
    })
    view = WideCameraView(cfg, stage)
    view.set_samples([Bookmark("above", x=0, y=2, z=0), Bookmark("below", x=0, y=0, z=0)])
    assert view._oop_cds.data["marker"] == ["triangle", "inverted_triangle"]
    assert view._oop_cds.data["color"] == ["orange", "deepskyblue"]
