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
