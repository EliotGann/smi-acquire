from __future__ import annotations

from smi_acquire.q_range import (
    QRangeConfig,
    d_spacing_nm_range,
    detector_directional_ranges,
    detector_ranges,
    q_from_angle,
    radial_angle_from_components,
    side_view_waxs_range,
    waxs_center_allowed,
    waxs_radial_q_range,
    waxs_tile_half_width_deg,
)


def test_q_from_angle_increases_with_energy_and_angle():
    assert q_from_angle(20.0, 13500.0) > q_from_angle(10.0, 13500.0)
    assert q_from_angle(20.0, 16000.0) > q_from_angle(20.0, 8000.0)


def test_detector_ranges_include_saxs_and_tiled_waxs():
    ranges = detector_ranges(
        QRangeConfig(energy_ev=13500.0, saxs_distance_m=5.0, waxs_start_deg=0.0,
                     waxs_end_deg=40.0, waxs_tiled=True),
        "both",
    )

    by_detector = {r.detector: r for r in ranges}
    assert set(by_detector) == {"pil2M", "pil900KW"}
    assert by_detector["pil2M"].q_low < by_detector["pil2M"].q_high
    assert round(by_detector["pil2M"].angular.high_deg, 2) == 2.20
    assert round(by_detector["pil900KW"].angular.low_deg, 2) == -11.49
    assert round(by_detector["pil900KW"].angular.high_deg, 2) == 51.49


def test_saxs_outer_angle_increases_at_shorter_distance():
    near = detector_ranges(QRangeConfig(saxs_distance_m=1.6), "saxs")[0]
    far = detector_ranges(QRangeConfig(saxs_distance_m=9.3), "saxs")[0]

    assert near.angular.high_deg > far.angular.high_deg
    assert near.q_high > far.q_high


def test_side_view_waxs_range_is_fixed_vertical_acceptance():
    angular = side_view_waxs_range()

    assert round(angular.low_deg, 2) == -7.91
    assert round(angular.high_deg, 2) == 38.82


def test_waxs_tile_width_comes_from_smi_tiled_geometry():
    assert round(waxs_tile_half_width_deg(), 2) == 11.49


def test_directional_ranges_label_qx_qy_for_each_detector():
    rows = detector_directional_ranges(QRangeConfig(waxs_start_deg=0, waxs_end_deg=20), "both")

    axes_by_detector = {}
    for row in rows:
        axes_by_detector.setdefault(row.detector, set()).add(row.axis)
    assert axes_by_detector == {"pil2M": {"qx", "qy"}, "pil900KW": {"qx", "qy"}}


def test_d_spacing_is_reported_in_nm_from_inverse_angstrom_q():
    d_min, d_max = d_spacing_nm_range(0.1, 2.0)

    assert round(d_min, 4) == 0.3142
    assert round(d_max, 3) == 6.283


def test_waxs_overall_q_uses_radial_components_and_beamstop():
    cfg = QRangeConfig(energy_ev=13500.0, waxs_start_deg=15.0, waxs_end_deg=15.0)
    q_low, q_high = waxs_radial_q_range(cfg)
    rows = {r.axis: r for r in detector_directional_ranges(cfg, "waxs")}

    assert q_low > 0.0
    component_high = max(rows["qx"].q_high, rows["qy"].q_high)
    assert q_high > component_high
    assert radial_angle_from_components(3.0, 4.0) > 4.0


def test_waxs_range_defaults_to_single_center_not_tiled():
    single = detector_ranges(QRangeConfig(waxs_start_deg=0.0, waxs_end_deg=50.0), "waxs")[0]
    tiled = detector_ranges(
        QRangeConfig(waxs_start_deg=0.0, waxs_end_deg=50.0, waxs_tiled=True), "waxs"
    )[0]

    assert single.angular.high_deg < tiled.angular.high_deg


def test_waxs_center_disallows_10_to_14_degrees():
    assert waxs_center_allowed(9.9)
    assert not waxs_center_allowed(10.0)
    assert not waxs_center_allowed(12.0)
    assert not waxs_center_allowed(14.0)
    assert waxs_center_allowed(14.1)
