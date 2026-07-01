"""Tests for microscope Quick scripts."""

from __future__ import annotations

import ast

from smi_acquire.microscope.config import ScriptsConfig
from smi_acquire.microscope.scripts import (Bookmark, bookmark_list_scan_snippet,
                                           line_scans_at_bookmarks,
                                           polygon_scans_at_bookmarks,
                                           square_grid_scans_at_bookmarks)


def test_bookmark_quick_script_uses_load_holder_when_possible():
    src = bookmark_list_scan_snippet(
        [Bookmark("a", 1, 2, 3, holder="bar1"), Bookmark("b", 4, 5, 6, holder="bar1")],
        ScriptsConfig(),
    )
    assert "load_holder('bar1')" in src
    assert "acquire_bar" in src
    assert "bps.mv" not in src  # positioning is delegated through acquire_bar/goto_sample
    ast.parse(src)


def test_bookmark_quick_script_falls_back_to_inline_samplelist():
    src = bookmark_list_scan_snippet([Bookmark("a", 1, 2, 3)], ScriptsConfig())
    assert "SampleList.from_columns" in src
    assert "piezo_x" in src and "piezo_y" in src and "piezo_z" in src
    ast.parse(src)


def test_square_quick_script_uses_spatial_grid_axes_with_center():
    src = square_grid_scans_at_bookmarks(
        [Bookmark("a", 1, 2, 3, holder="bar1")], 1.0, 2.0, 3, 5, ScriptsConfig())
    assert "load_holder('bar1')" in src
    assert "spatial_grid_axes" in src
    assert "center=(cx, cy)" in src
    assert "name_tokens=('x{x}', 'y{y}')" in src
    assert "bp.grid_scan" not in src
    ast.parse(src)


def test_line_quick_script_uses_motor_axis_with_centered_values():
    src = line_scans_at_bookmarks(
        [Bookmark("a", 1, 2, 3, holder="bar1")], "x", 1.0, 5, ScriptsConfig())
    assert "load_holder('bar1')" in src
    assert "motor_axis('x', piezo.x" in src
    assert "name_tokens=('x{x}',)" in src
    assert "bp.scan" not in src
    ast.parse(src)


def test_polygon_quick_script_uses_backend_region_when_holder_backed():
    src = polygon_scans_at_bookmarks(
        [Bookmark("a", 1, 2, 3, holder="bar1")],
        offsets=[(0.0, 0.0)],
        polygon_offsets=[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0)],
        step_x=0.25,
        step_y=0.25,
        scripts_cfg=ScriptsConfig(),
    )
    assert "polygon_region_bar" in src
    assert "scan_regions" in src
    assert "load_holder('bar1'" in src
    assert "bp.list_scan" not in src
    ast.parse(src)
