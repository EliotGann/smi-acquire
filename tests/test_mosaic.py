"""Tests for microscope map-background mosaic behavior."""

from __future__ import annotations

import numpy as np
from bokeh.plotting import figure

from smi_acquire.microscope.calibration import CalibrationModel
from smi_acquire.microscope.mosaic import MosaicBackground
from smi_acquire.microscope.overlays import BeamOverlay


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


class _Stream:
    def __init__(self):
        self._rgb = np.zeros((20, 30, 3), dtype=np.uint8)
        self._rgb[..., 0] = 100

    def grab_rgb(self):
        return self._rgb

    def current_dims(self):
        return type("Dims", (), {"width": 30, "height": 20})()


def _unpack_rgba(packed):
    return packed.view(np.uint8).reshape(*packed.shape, 4)


def test_mosaic_capture_and_redraw_offsets_with_motor_delta():
    fig = figure(width=200, height=150)
    stage = _Stage()
    stream = _Stream()
    beam = BeamOverlay((15, 10), 4, 2)
    mosaic = MosaicBackground(fig, stream, stage, beam, CalibrationModel([[-10, 0], [0, -10]]))

    assert mosaic.capture_current(force=True)
    stage.x.position = 1.0
    mosaic._redraw()

    assert len(mosaic.cds.data["image"]) == 1
    # dp = A @ (current - tile) = [-10, 0], so cached center shifts left by 10 px.
    assert mosaic.cds.data["x"][0] == -10.0
    assert mosaic.cds.data["y"][0] == 0.0


def test_mosaic_clear_removes_cached_views():
    fig = figure(width=200, height=150)
    mosaic = MosaicBackground(
        fig, _Stream(), _Stage(), BeamOverlay((15, 10), 4, 2), CalibrationModel([[-10, 0], [0, -10]])
    )
    mosaic.capture_current(force=True)
    mosaic.clear()
    assert mosaic.cds.data["image"] == []


def test_mosaic_overlaps_are_averaged_not_alpha_stacked():
    fig = figure(width=200, height=150)
    stage = _Stage()
    stream = _Stream()
    mosaic = MosaicBackground(
        fig, stream, stage, BeamOverlay((15, 10), 4, 2), CalibrationModel([[-10, 0], [0, -10]])
    )

    stream._rgb[..., :] = 50
    mosaic.capture_current(force=True)
    stream._rgb[..., :] = 150
    mosaic.capture_current(force=True)

    assert len(mosaic.cds.data["image"]) == 1
    rgba = _unpack_rgba(mosaic.cds.data["image"][0])
    assert rgba[..., 0].mean() == 100
    assert set(np.unique(rgba[..., 3])) == {mosaic._capture_alpha}


class _HiResStream:
    """A native-resolution camera whose display frame is strided down (like the real PVA cam)."""

    def __init__(self, native=(480, 640), display_max_dim=160):
        h, w = native
        self._rgb = np.zeros((h, w, 3), dtype=np.uint8)
        self._rgb[..., 0] = 100
        self.display_max_dim = display_max_dim
        from smi_acquire.microscope.camera_stream import stride_for_max_dim
        self._stride = stride_for_max_dim(w, h, display_max_dim)
        self._disp_w = len(range(0, w, self._stride))
        self._disp_h = len(range(0, h, self._stride))

    def grab_rgb(self):
        return self._rgb  # native resolution, as the real stream returns

    def current_dims(self):
        w, h = self._disp_w, self._disp_h
        return type("Dims", (), {"width": w, "height": h})()


def test_mosaic_caches_at_display_resolution_not_native():
    # Regression: on high-res cameras the cached tile must match the strided *display* frame,
    # not the native frame — otherwise the map view renders several times larger than live.
    fig = figure(width=200, height=150)
    stream = _HiResStream(native=(480, 640), display_max_dim=160)  # stride 4 → 160x120 display
    mosaic = MosaicBackground(
        fig, stream, _Stage(), BeamOverlay((80, 60), 4, 2), CalibrationModel([[-10, 0], [0, -10]])
    )

    assert mosaic.capture_current(force=True)
    tile = mosaic._tiles[-1]
    # Tile must be at display resolution (160x120), NOT native (640x480).
    assert (tile.w, tile.h) == (stream._disp_w, stream._disp_h)
    assert tile.gray.shape == (stream._disp_h, stream._disp_w)

