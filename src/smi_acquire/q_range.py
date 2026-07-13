"""Small q-range geometry model for the Plan wizard.

The values here are intentionally approximate: this widget is for experiment planning and
communication, not detector calibration. It models the beamline geometry users reason about:
SAXS centered on the direct beam, WAXS on a horizontal arc, and a 2 mm beamstop.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan, pi, sin, sqrt, tan


HC_EV_A = 12398.419843320026
PIL2M_ACTIVE_WIDTH_MM = 253.7
PIL2M_ACTIVE_HEIGHT_MM = 288.8
WAXS_PIXEL_SIZE_MM = 0.172
WAXS_DISTANCE_MM = 273.0
WAXS_DISPLAY_SHAPE = (1475, 619)
WAXS_BEAM_ROW_PX = 217.0
WAXS_BEAM_COL_PX = 319.0
WAXS_PANEL_COL_RANGES = ((0, 206), (206, 413), (413, 619))
WAXS_PANEL_OFFSETS_DEG = (-7.0, 0.0, 7.0)
WAXS_DISALLOWED_CENTER_RANGE = (10.0, 14.0)

_WAXS_TILE_HORIZONTAL = None
_WAXS_VERTICAL = None


@dataclass(frozen=True)
class QRangeConfig:
    energy_ev: float = 13500.0
    saxs_distance_m: float = 5.0
    waxs_start_deg: float = 0.0
    waxs_end_deg: float = 20.0
    waxs_tiled: bool = False
    beamstop_diameter_mm: float = 2.0


@dataclass(frozen=True)
class AngularRange:
    low_deg: float
    high_deg: float


@dataclass(frozen=True)
class DetectorQRange:
    detector: str
    label: str
    angular: AngularRange
    q_low: float
    q_high: float


@dataclass(frozen=True)
class DirectionalQRange:
    detector: str
    label: str
    axis: str
    angular: AngularRange
    q_low: float
    q_high: float


def wavelength_a(energy_ev: float) -> float:
    """Return X-ray wavelength in Angstrom for an energy in eV."""
    energy = max(float(energy_ev), 1.0)
    return HC_EV_A / energy


def q_from_angle(angle_deg: float, energy_ev: float) -> float:
    """Return q in inverse Angstrom from a scattering angle in degrees."""
    angle = max(float(angle_deg), 0.0)
    return 4.0 * pi / wavelength_a(energy_ev) * sin(angle * pi / 360.0)


def detector_ranges(config: QRangeConfig, q_selection: str = "both") -> list[DetectorQRange]:
    """Approximate visible angular and q coverage for the selected detector set.

    ``waxs_start_deg`` and ``waxs_end_deg`` are WAXS central arc angles. Each WAXS setting covers
    roughly a 20 degree horizontal swath, so tiled coverage runs from start-10 to end+10. A 10%
    The full detector extent is reported; the UI may shade edge regions separately.
    """
    selected = q_selection if q_selection in {"both", "saxs", "waxs"} else "both"
    ranges: list[DetectorQRange] = []
    if selected in {"both", "saxs"}:
        angular = _saxs_angular_range(config)
        ranges.append(_detector_range("pil2M", "SAXS Pilatus 2M", angular, config.energy_ev))
    if selected in {"both", "waxs"}:
        angular = _waxs_angular_range(config)
        q_low, q_high = waxs_radial_q_range(config)
        ranges.append(DetectorQRange(
            detector="pil900KW",
            label="WAXS Pilatus 900kW",
            angular=angular,
            q_low=q_low,
            q_high=q_high,
        ))
    return ranges


def detector_directional_ranges(
    config: QRangeConfig,
    q_selection: str = "both",
) -> list[DirectionalQRange]:
    """Approximate qx/qy planning coverage for the selected detector set."""
    selected = q_selection if q_selection in {"both", "saxs", "waxs"} else "both"
    rows: list[DirectionalQRange] = []
    if selected in {"both", "saxs"}:
        rows.extend(_saxs_directional_ranges(config))
    if selected in {"both", "waxs"}:
        rows.extend(_waxs_directional_ranges(config))
    return rows


def side_view_waxs_range() -> AngularRange:
    """Fixed vertical WAXS collection in the side view."""
    return _waxs_vertical_range()


def waxs_tile_half_width_deg() -> float:
    """Approximate half-width of one WAXS tile from the smi-tiled 3-panel geometry."""
    r = _waxs_tile_horizontal_range()
    return max(abs(r.low_deg), abs(r.high_deg))


def waxs_center_allowed(center_deg: float) -> bool:
    """False for the WAXS arc center range that should not be planned."""
    low, high = WAXS_DISALLOWED_CENTER_RANGE
    center = float(center_deg)
    return not (low <= center <= high)


def d_spacing_nm_range(q_low: float, q_high: float) -> tuple[float | None, float | None]:
    """Return d-spacing range in nm as ``(d_min, d_max)`` for q in inverse Angstrom."""
    qlo = max(float(q_low), 0.0)
    qhi = max(float(q_high), 0.0)
    d_min = (2.0 * pi / qhi / 10.0) if qhi > 0 else None
    d_max = (2.0 * pi / qlo / 10.0) if qlo > 0 else None
    return d_min, d_max


def radial_angle_from_components(qx_angle_deg: float, qy_angle_deg: float) -> float:
    """Approximate radial scattering angle from horizontal/vertical component angles."""
    tx = tan(float(qx_angle_deg) * pi / 180.0)
    ty = tan(float(qy_angle_deg) * pi / 180.0)
    return atan(sqrt(tx * tx + ty * ty)) * 180.0 / pi


def waxs_radial_q_range(config: QRangeConfig) -> tuple[float, float]:
    """WAXS overall q range using radial sqrt(qx^2 + qy^2)-style geometry.

    The lower bound is limited by a 2 mm beamstop at the WAXS detector distance.
    """
    qx = _waxs_angular_range(config)
    qy = side_view_waxs_range()
    beamstop_radius_mm = max(float(config.beamstop_diameter_mm), 0.0) / 2.0
    beamstop_angle = atan(beamstop_radius_mm / WAXS_DISTANCE_MM) * 180.0 / pi

    qx_candidates = _nearest_angles_to_zero(qx)
    qy_candidates = _nearest_angles_to_zero(qy)
    low_angle = min(
        radial_angle_from_components(x, y)
        for x in qx_candidates
        for y in qy_candidates
    )
    low_angle = max(low_angle, beamstop_angle)
    high_angle = max(
        radial_angle_from_components(x, y)
        for x in (qx.low_deg, qx.high_deg)
        for y in (qy.low_deg, qy.high_deg)
    )
    return q_from_angle(low_angle, config.energy_ev), q_from_angle(high_angle, config.energy_ev)


def _saxs_angular_range(config: QRangeConfig) -> AngularRange:
    distance_mm = max(float(config.saxs_distance_m), 0.05) * 1000.0
    beamstop_radius_mm = max(float(config.beamstop_diameter_mm), 0.0) / 2.0
    # Pilatus 2M is about 254 x 289 mm. For q reach, use the radial distance to the
    # detector corner, because the highest q is at a corner.
    half_active_mm = ((PIL2M_ACTIVE_WIDTH_MM / 2.0) ** 2
                      + (PIL2M_ACTIVE_HEIGHT_MM / 2.0) ** 2) ** 0.5
    low = atan(beamstop_radius_mm / distance_mm) * 180.0 / pi
    high = atan(half_active_mm / distance_mm) * 180.0 / pi
    return AngularRange(low, max(high, low))


def _saxs_axis_angular_range(config: QRangeConfig, active_size_mm: float) -> AngularRange:
    distance_mm = max(float(config.saxs_distance_m), 0.05) * 1000.0
    beamstop_radius_mm = max(float(config.beamstop_diameter_mm), 0.0) / 2.0
    half_active_mm = float(active_size_mm) / 2.0
    low = atan(beamstop_radius_mm / distance_mm) * 180.0 / pi
    high = atan(half_active_mm / distance_mm) * 180.0 / pi
    return AngularRange(low, max(high, low))


def _saxs_directional_ranges(config: QRangeConfig) -> list[DirectionalQRange]:
    out = []
    for axis, size in (("qx", PIL2M_ACTIVE_WIDTH_MM), ("qy", PIL2M_ACTIVE_HEIGHT_MM)):
        angular = _saxs_axis_angular_range(config, size)
        out.append(DirectionalQRange(
            detector="pil2M",
            label="SAXS Pilatus 2M",
            axis=axis,
            angular=AngularRange(-angular.high_deg, angular.high_deg),
            q_low=q_from_angle(angular.low_deg, config.energy_ev),
            q_high=q_from_angle(angular.high_deg, config.energy_ev),
        ))
    return out


def _waxs_directional_ranges(config: QRangeConfig) -> list[DirectionalQRange]:
    qx = _waxs_angular_range(config)
    qy = side_view_waxs_range()
    return [
        DirectionalQRange(
            detector="pil900KW",
            label="WAXS Pilatus 900kW",
            axis="qx",
            angular=qx,
            q_low=q_from_angle(_smallest_abs_angle(qx), config.energy_ev),
            q_high=q_from_angle(max(abs(qx.low_deg), abs(qx.high_deg)), config.energy_ev),
        ),
        DirectionalQRange(
            detector="pil900KW",
            label="WAXS Pilatus 900kW",
            axis="qy",
            angular=qy,
            q_low=q_from_angle(_smallest_abs_angle(qy), config.energy_ev),
            q_high=q_from_angle(max(abs(qy.low_deg), abs(qy.high_deg)), config.energy_ev),
        ),
    ]


def _smallest_abs_angle(angular: AngularRange) -> float:
    if angular.low_deg <= 0.0 <= angular.high_deg:
        return 0.0
    return min(abs(angular.low_deg), abs(angular.high_deg))


def _nearest_angles_to_zero(angular: AngularRange) -> tuple[float, ...]:
    if angular.low_deg <= 0.0 <= angular.high_deg:
        return (0.0,)
    return (angular.low_deg,) if abs(angular.low_deg) <= abs(angular.high_deg) else (angular.high_deg,)


def _waxs_angular_range(config: QRangeConfig) -> AngularRange:
    start = min(float(config.waxs_start_deg), float(config.waxs_end_deg))
    end = max(float(config.waxs_start_deg), float(config.waxs_end_deg))
    if not bool(config.waxs_tiled):
        start = end = float(config.waxs_start_deg)
    half_width = waxs_tile_half_width_deg()
    full_low = start - half_width
    full_high = end + half_width
    return AngularRange(full_low, full_high)


def _waxs_tile_horizontal_range() -> AngularRange:
    global _WAXS_TILE_HORIZONTAL
    if _WAXS_TILE_HORIZONTAL is None:
        h, _v = _waxs_base_angle_maps()
        _WAXS_TILE_HORIZONTAL = AngularRange(float(h.min()), float(h.max()))
    return _WAXS_TILE_HORIZONTAL


def _waxs_vertical_range() -> AngularRange:
    global _WAXS_VERTICAL
    if _WAXS_VERTICAL is None:
        _h, v = _waxs_base_angle_maps()
        # Keep this app's existing display convention: WAXS goes mostly upward in qy.
        _WAXS_VERTICAL = AngularRange(float(-v.max()), float(-v.min()))
    return _WAXS_VERTICAL


def _waxs_base_angle_maps():
    import numpy as np

    ny, nx = WAXS_DISPLAY_SHAPE
    pixel = WAXS_PIXEL_SIZE_MM
    rows = np.arange(ny, dtype=float)
    panel_c0s, panel_c1s, panel_mids, alphas = [], [], [], []
    for (c0, c1), off in zip(WAXS_PANEL_COL_RANGES, WAXS_PANEL_OFFSETS_DEG):
        panel_c0s.append(c0)
        panel_c1s.append(c1)
        panel_mids.append(0.5 * (c0 + c1 - 1))
        alphas.append(off * pi / 180.0)

    ref_idx = min(range(len(alphas)), key=lambda i: abs(WAXS_PANEL_OFFSETS_DEG[i]))
    bc_u = -(WAXS_BEAM_COL_PX - panel_mids[ref_idx]) * pixel
    alpha_ref = alphas[ref_idx]
    centers_x = [0.0] * len(alphas)
    centers_z = [0.0] * len(alphas)
    centers_x[ref_idx] = -bc_u * np.cos(alpha_ref)
    centers_z[ref_idx] = WAXS_DISTANCE_MM - bc_u * np.sin(alpha_ref)

    for i in range(ref_idx - 1, -1, -1):
        fold = panel_c1s[i] - 0.5
        u1 = -(fold - panel_mids[i + 1]) * pixel
        u0 = -(fold - panel_mids[i]) * pixel
        centers_x[i] = (centers_x[i + 1] + u1 * np.cos(alphas[i + 1])
                        - u0 * np.cos(alphas[i]))
        centers_z[i] = (centers_z[i + 1] + u1 * np.sin(alphas[i + 1])
                        - u0 * np.sin(alphas[i]))
    for i in range(ref_idx + 1, len(alphas)):
        fold = panel_c0s[i] - 0.5
        u1 = -(fold - panel_mids[i - 1]) * pixel
        u0 = -(fold - panel_mids[i]) * pixel
        centers_x[i] = (centers_x[i - 1] + u1 * np.cos(alphas[i - 1])
                        - u0 * np.cos(alphas[i]))
        centers_z[i] = (centers_z[i - 1] + u1 * np.sin(alphas[i - 1])
                        - u0 * np.sin(alphas[i]))

    h_parts = []
    v_parts = []
    for idx, (c0, c1) in enumerate(WAXS_PANEL_COL_RANGES):
        cols = np.arange(c0, c1, dtype=float)
        rr, cc = np.meshgrid(rows, cols, indexing="ij")
        u = -(cc - panel_mids[idx]) * pixel
        y_det = -(rr - WAXS_BEAM_ROW_PX) * pixel
        x_det = centers_x[idx] + u * np.cos(alphas[idx])
        z_det = centers_z[idx] + u * np.sin(alphas[idx])
        h_parts.append(np.degrees(np.arctan2(x_det, z_det)).ravel())
        v_parts.append(np.degrees(np.arctan2(y_det, z_det)).ravel())
    return np.concatenate(h_parts), np.concatenate(v_parts)


def _detector_range(
    detector: str,
    label: str,
    angular: AngularRange,
    energy_ev: float,
) -> DetectorQRange:
    low = max(angular.low_deg, 0.0)
    high = max(angular.high_deg, low)
    return DetectorQRange(
        detector=detector,
        label=label,
        angular=angular,
        q_low=q_from_angle(low, energy_ev),
        q_high=q_from_angle(high, energy_ev),
    )


__all__ = [
    "AngularRange",
    "DetectorQRange",
    "DirectionalQRange",
    "QRangeConfig",
    "d_spacing_nm_range",
    "detector_directional_ranges",
    "detector_ranges",
    "q_from_angle",
    "radial_angle_from_components",
    "side_view_waxs_range",
    "waxs_radial_q_range",
    "waxs_center_allowed",
    "waxs_tile_half_width_deg",
    "wavelength_a",
]
