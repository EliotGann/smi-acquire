"""Polygon utilities for Area mode.

Generates grid points that fall inside a user-drawn polygon at independent x / y step sizes.
The polygon can be in any 2D coordinate frame (we use it in motor-space mm); the caller is
responsible for any frame conversions.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.prepared import prep


def grid_in_polygon(
    polygon_xy: list[tuple[float, float]],
    step_x: float,
    step_y: float,
    max_points: int = 100_000,
) -> tuple[list[tuple[float, float]], bool]:
    """Generate (x, y) grid points lying inside the polygon at spacing (step_x, step_y).

    Parameters
    ----------
    polygon_xy
        Polygon vertices in order. Min 3 vertices.
    step_x, step_y
        Spacings between grid lines along each axis. Must be > 0.
    max_points
        Hard upper bound. If the polygon's bbox would obviously generate more candidate
        points than 5× this, we bail out with an empty list and ``truncated=True`` so the
        caller can warn the user instead of doing minutes of work. Otherwise points are
        generated lazily and ``truncated=True`` indicates we stopped at the cap.

    Returns
    -------
    (points, truncated)
        ``points`` is the list of (x, y) tuples that lie strictly inside the polygon.
        ``truncated`` is True if generation was capped (by max_points or pre-bail).
    """
    if len(polygon_xy) < 3 or step_x <= 0 or step_y <= 0:
        return [], False

    poly = Polygon(polygon_xy)
    if not poly.is_valid:
        poly = poly.buffer(0)
        if poly.is_empty or not poly.is_valid:
            return [], False

    xmin, ymin, xmax, ymax = poly.bounds
    estimate = ((xmax - xmin) / step_x + 1) * ((ymax - ymin) / step_y + 1)
    if estimate > 5 * max_points:
        return [], True

    xs = np.arange(xmin, xmax + step_x / 2, step_x)
    ys = np.arange(ymin, ymax + step_y / 2, step_y)
    prepared = prep(poly)

    points: list[tuple[float, float]] = []
    for x in xs:
        for y in ys:
            if prepared.contains(Point(float(x), float(y))):
                points.append((float(x), float(y)))
                if len(points) >= max_points:
                    return points, True
    return points, False


def polygon_motor_to_pixel(
    motor_xy: list[tuple[float, float]],
    beam_px: tuple[float, float],
    m_now: tuple[float, float],
    affine: np.ndarray,
) -> list[tuple[float, float]]:
    """Project sample-frame motor coords back to pixel coords given the current motor pos.

    A point at sample (motor) coord ``V`` appears at pixel ``beam_px + A · (m_now − V)``:
    when the motors are at ``V``, that point sits exactly at the beam; as motors leave ``V``,
    the point drifts in the camera frame by ``A · (m_now − V)`` (the empirical calibration).
    """
    A = np.asarray(affine, dtype=float)
    out: list[tuple[float, float]] = []
    for mx, my in motor_xy:
        dp = A @ np.array([m_now[0] - mx, m_now[1] - my], dtype=float)
        out.append((beam_px[0] + float(dp[0]), beam_px[1] + float(dp[1])))
    return out


def pixel_to_motor(
    pixel_xy: tuple[float, float],
    beam_px: tuple[float, float],
    m_now: tuple[float, float],
    affine_inv: np.ndarray,
) -> tuple[float, float]:
    """Sample-frame motor coordinate of whatever feature is currently at ``pixel_xy``.

    Inverting ``pixel = beam_px + A · (m_now − V)`` for ``V`` gives
    ``V = m_now − A⁻¹ · (pixel − beam_px)``.
    """
    dp = np.array([pixel_xy[0] - beam_px[0], pixel_xy[1] - beam_px[1]], dtype=float)
    dm = np.asarray(affine_inv, dtype=float) @ dp
    return float(m_now[0] - dm[0]), float(m_now[1] - dm[1])
