"""Linear scan mode (under the Scan tab).

A 1-D line scan of a fixed **distance** and **step**, along a chosen **direction** (X or Y),
replicated at a set of sample centers. The centers are the bookmarks currently enabled for
scanning — their ``in_scan`` flag, set via the ``scan`` column of the shared **Bookmarks**
list (far right).

- Tick **scan** on bookmarks in the **Bookmarks** list to place — and scan — a centered line
  at each.
- **Add point** (latching): turn it on, then click the image to drop auto-named ``pos1`` /
  ``pos2`` … bookmarks at chosen spots. **Clear placed points** removes the ``pos*`` points.

Enable the magenta overlay independently with the **Linear** toggle in the Quick scripts
panel. Lines lock to motor coords so they track the sample as you jog. The generated bluesky
plan (in the Quick scripts panel under the image) loops over all enabled centers with the
configured alignment mode swaps.
"""

from __future__ import annotations

import logging

import numpy as np
import panel as pn
from bokeh.models import ColumnDataSource
from bokeh.plotting import figure as Figure  # noqa: N812 — type alias

from ..calibration import CalibrationModel
from ..config import AppConfig
from ..devices import SampleStage
from ..geometry import pixel_to_motor
from ..overlays import BeamOverlay
from ..scripts import Bookmark, line_scans_at_bookmarks
from ..widgets.toggle import make_latching_toggle, style_latching_toggle

log = logging.getLogger(__name__)


class LinearScanMode:
    name = "Linear"
    # Overlay family / color for the persistent script panel (magenta = line scans).
    script_kind = "Linear"
    script_color = "#d63bd6"
    script_explanation = (
        "`scan` along one axis centered on each target (bookmarks and/or clicked spots), "
        "wrapped in the configured alignment mode swaps."
    )

    def __init__(
        self,
        fig: Figure,
        stage: SampleStage,
        beam_overlay: BeamOverlay,
        calibration: CalibrationModel,
        cfg: AppConfig,
        image_size_hint_provider,
        bookmark_store,
    ) -> None:
        self.fig = fig
        self.stage = stage
        self.beam = beam_overlay
        self.calibration = calibration
        self.cfg = cfg
        self._dims = image_size_hint_provider
        # Central bookmark store (InteractiveMode). Scan targets are its in_scan bookmarks.
        self._store = bookmark_store
        self._active = False
        # Per-kind overlay on/off (Quick-scripts panel toggle). When off, the line overlay is
        # cleared so each scan kind shows independently.
        self._enabled = False

        self._on_change = lambda: None

        # Line segments (one per center) + scan-point dots.
        self._seg_cds = ColumnDataSource(data={"x0": [], "y0": [], "x1": [], "y1": []})
        self._seg_renderer = fig.segment(
            x0="x0", y0="y0", x1="x1", y1="y1",
            source=self._seg_cds, color="magenta", line_width=3,
        )
        self._point_cds = ColumnDataSource(data={"x": [], "y": []})
        self._point_renderer = fig.scatter(
            "x", "y", source=self._point_cds, size=4, color="magenta", alpha=0.85,
            marker="circle",
        )

        self.tools = ()

        # ---- side panel widgets ----
        units = cfg.ui.motor_units
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        default_dist = 200.0 if is_um else 0.2
        increment = 5.0 if is_um else 0.005

        self._distance = pn.widgets.FloatInput(
            name=f"distance ({units})", value=default_dist, step=increment, width=150,
        )
        self._step = pn.widgets.FloatInput(
            name=f"step ({units})", value=cfg.ui.default_step, step=increment, width=130,
        )
        self._direction = pn.widgets.Select(
            name="direction", options=["X", "Y"], value="X", width=110,
        )
        for w in (self._distance, self._step):
            w.param.watch(self._on_param_change, "value")
        self._direction.param.watch(self._on_param_change, "value")

        # "Add point": while ON, image clicks drop pos* bookmarks (shared) at the click.
        self._add_toggle = make_latching_toggle("Add point", value=False, width=170)
        self._add_toggle.param.watch(self._on_add_toggle, "value")
        self._clear_placed_btn = pn.widgets.Button(
            name="Clear placed points", button_type="default", width=170,
        )
        self._clear_placed_btn.on_click(lambda _e: self._store.clear_placed_points())
        self._count = pn.widgets.StaticText(value="", width=380)

        self.panel = pn.Column(
            pn.pane.Markdown("### Linear scan"),
            pn.pane.Markdown(
                "Pick a distance, step, and direction. Tick **scan** on bookmarks in the "
                "**Bookmarks** list to place a line on each, and/or turn on **Add point** and "
                "click the image to drop new spots. Enable the overlay with the **Linear** "
                "toggle in the Quick scripts panel."
            ),
            pn.Row(self._distance, self._step, self._direction),
            pn.Row(self._add_toggle, self._clear_placed_btn),
            self._count,
        )

        self._refresh_pixels()

        # Keep our overlays + script in sync whenever the shared bookmark set changes.
        self._store.add_bookmark_listener(self.on_bookmarks_changed)

    # ---- Mode protocol --------------------------------------------------------------

    def set_on_change(self, callback) -> None:
        self._on_change = callback or (lambda: None)

    def set_overlay_enabled(self, enabled: bool) -> None:
        """Show/hide this kind's overlay independently (Quick-scripts panel toggle)."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._refresh_pixels()

    def activate(self) -> None:
        self._active = True
        self.tick()

    def deactivate(self) -> None:
        self._active = False
        if self._add_toggle.value:
            self._add_toggle.value = False
        # Leave renderers up so the planned lines stay visible from the other tabs.

    def on_tap(self, x: float, y: float) -> None:
        if not self._add_toggle.value:
            return
        m = self._pixel_to_motor(x, y)
        if m is None:
            return
        try:
            z = float(self.stage.z.position)
        except Exception:
            z = 0.0
        # Adds a pos* bookmark (in_scan=True); the store notifies us back to redraw.
        self._store.add_point(m[0], m[1], z)

    def tick(self) -> None:
        self._refresh_pixels()

    def tick_table(self) -> None:
        return

    def on_bookmarks_changed(self) -> None:
        self._refresh_pixels()
        self._notify()

    # ---- script panel hooks ---------------------------------------------------------

    def script_text(self) -> str:
        params = self._params()
        if params is None:
            return "# set a positive distance and step to populate this script\n"
        dist, num, axis = params
        return line_scans_at_bookmarks(
            self._scan_targets(),
            motor_axis=axis.lower(),
            distance=dist,
            num=num,
            scripts_cfg=self.cfg.scripts,
        )

    def total_points(self) -> int:
        params = self._params()
        if params is None:
            return 0
        _dist, num, _axis = params
        return len(self._scan_targets()) * num

    # ---- placement ------------------------------------------------------------------

    def _on_add_toggle(self, _event) -> None:
        style_latching_toggle(self._add_toggle)

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass

    def _scan_targets(self) -> list[Bookmark]:
        """Bookmarks currently enabled for scanning (shared ``in_scan`` state)."""
        return list(self._store.get_scan_targets())

    def _centers(self) -> list[tuple[str, float, float, float]]:
        return [(b.name, b.x, b.y, b.z) for b in self._scan_targets()]

    # ---- core ------------------------------------------------------------------------

    def _pixel_to_motor(self, px: float, py: float) -> tuple[float, float] | None:
        try:
            m_now = (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            m_now = (0.0, 0.0)
        try:
            A_inv = np.linalg.inv(self.calibration.matrix)
        except Exception:
            return None
        return pixel_to_motor((float(px), float(py)), self.beam.center, m_now, A_inv)

    def _on_param_change(self, _event) -> None:
        self._refresh_pixels()
        self._notify()

    def _params(self) -> tuple[float, int, str] | None:
        dist = float(self._distance.value or 0.0)
        step = float(self._step.value or 0.0)
        if dist <= 0 or step <= 0:
            return None
        num = max(2, int(round(dist / step)) + 1)
        return dist, num, str(self._direction.value)

    def _line_motor_endpoints(
        self, cx: float, cy: float, dist: float, axis: str
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        half = dist / 2.0
        if axis == "X":
            return (cx - half, cy), (cx + half, cy)
        return (cx, cy - half), (cx, cy + half)

    def _refresh_pixels(self) -> None:
        centers = self._centers()
        params = self._params()
        if not self._enabled or not centers or params is None:
            self._seg_cds.data = {"x0": [], "y0": [], "x1": [], "y1": []}
            self._point_cds.data = {"x": [], "y": []}
            if not self._enabled:
                self._count.value = "overlay off — enable 'Linear' in the Quick scripts panel"
            elif params is None:
                self._count.value = "set a positive distance and step"
            else:
                self._count.value = "no scan targets — tick a bookmark above, or use 'Add point'"
            return

        dist, num, axis = params
        m_now = self._safe_motor_now()
        beam_px = self.beam.center
        A = self.calibration.matrix

        x0s: list[float] = []
        y0s: list[float] = []
        x1s: list[float] = []
        y1s: list[float] = []
        pxs: list[float] = []
        pys: list[float] = []

        def project(mx: float, my: float) -> tuple[float, float]:
            dp = A @ np.array([m_now[0] - mx, m_now[1] - my], dtype=float)
            return beam_px[0] + float(dp[0]), beam_px[1] + float(dp[1])

        for _name, cx, cy, _z in centers:
            (m0, m1) = self._line_motor_endpoints(cx, cy, dist, axis)
            p0 = project(*m0)
            p1 = project(*m1)
            x0s.append(p0[0])
            y0s.append(p0[1])
            x1s.append(p1[0])
            y1s.append(p1[1])
            half = dist / 2.0
            if axis == "X":
                sample_motors = [(float(sx), cy) for sx in np.linspace(cx - half, cx + half, num)]
            else:
                sample_motors = [(cx, float(sy)) for sy in np.linspace(cy - half, cy + half, num)]
            for sx, sy in sample_motors:
                px, py = project(sx, sy)
                pxs.append(px)
                pys.append(py)

        self._seg_cds.data = {"x0": x0s, "y0": y0s, "x1": x1s, "y1": y1s}
        self._point_cds.data = {"x": pxs, "y": pys}

        self._count.value = (
            f"{len(centers)} target(s) × {num} pts along {axis} = "
            f"{len(centers) * num:,} pts total"
        )

    def _safe_motor_now(self) -> tuple[float, float]:
        try:
            return (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            return (0.0, 0.0)
