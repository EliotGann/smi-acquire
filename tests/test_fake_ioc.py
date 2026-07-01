"""Tests for the development fake IOC camera image."""

from __future__ import annotations

import numpy as np

from smi_acquire.sim.fake_ioc import _render_frame, _render_wide_frame


def test_fake_camera_field_changes_with_motor_motion():
    """Moving the virtual piezo should pan to a different part of the synthetic field."""
    a = _render_frame(0.0, 0.0, 0.0, 0.0)
    b = _render_frame(0.0, 1.0, 0.0, 0.0)
    assert a.shape == (480, 640, 3)
    assert b.shape == a.shape
    assert np.mean(np.abs(a.astype(float) - b.astype(float))) > 5.0


def test_fake_camera_field_has_global_variation():
    """Distant virtual field positions should not look like the same repeated tile."""
    a = _render_frame(0.0, -40.0, -4.0, 0.0)
    b = _render_frame(0.0, 40.0, 4.0, 0.0)
    assert np.mean(np.abs(a.astype(float) - b.astype(float))) > 20.0


def test_fake_camera_has_local_features_for_alignment():
    """The generated image should have enough local contrast for calibration/focus workflows."""
    img = _render_frame(0.0, 0.0, 0.0, 0.0)
    gray = img.mean(axis=2)
    assert gray.std() > 25.0
    assert gray.max() - gray.min() > 100.0


def test_fake_wide_camera_is_grayscale_and_moves_with_xz():
    a = _render_wide_frame(0.0, 0.0, 0.0)
    b = _render_wide_frame(0.0, 1.0, 2.0)
    assert a.shape == (360, 640)
    assert b.shape == a.shape
    assert np.mean(np.abs(a.astype(float) - b.astype(float))) > 5.0
