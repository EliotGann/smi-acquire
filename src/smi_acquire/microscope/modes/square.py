"""Square (rectangular) scan mode — under the Scan tab.

A user-provided rectangle (width × height) replicated at a set of sample centers. The centers
are the bookmarks currently enabled for scanning — their ``in_scan`` flag, set via the
``scan`` column of the shared **Bookmarks** list (far right).

- Tick **scan** on bookmarks in the **Bookmarks** list to place — and scan — an identical
  rectangle centered on each.
- **Add point** (latching): turn it on, then click the image to drop auto-named ``pos1`` /
  ``pos2`` … bookmarks at chosen spots (added enabled-for-scan). Turn it off to stop placing.
  **Clear placed points** removes the ``pos*`` points.

Enable the orange overlay independently with the **Square** toggle in the Quick scripts panel.
The rectangle + dot grid lock to motor coords so they track the sample as you jog. The
generated bluesky plan (shown in the Quick scripts panel under the image) loops over all
enabled centers, naming each scan after its bookmark.
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
from ..scripts import Bookmark, square_grid_scans_at_bookmarks
from ..widgets.toggle import make_latching_toggle, style_latching_toggle

log = logging.getLogger(__name__)


class SquareScanMode:
    name = "Square"
    # Overlay family / color for the persistent script panel (orange = grid scans).
    script_kind = "Square"
    script_color = "#e8920c"
    script_explanation = (
        "`grid_scan` over a rectangle centered on each target (bookmarks and/or clicked "
        "spots). Each scan is named after its target."
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
        # The central bookmark store (InteractiveMode). Scan targets are its bookmarks with
        # ``in_scan`` ticked; click placement adds points to it.
        self._store = bookmark_store
        self._active = False
        # Per-kind overlay on/off (driven by the Quick-scripts panel toggle). When off, the
        # rectangle/grid overlay is cleared so each scan kind shows independently — turning on
        # the line never lights up the grid.
        self._enabled = False

        # Notified when this mode's scan set changes (so the script panel can refresh).
        self._on_change = lambda: None

        # Rectangle outline (4-corner patches — one patch per center).
        self._rect_cds = ColumnDataSource(data={"xs": [], "ys": []})
        self._rect_renderer = fig.patches(
            xs="xs", ys="ys", source=self._rect_cds,
            fill_color="orange", fill_alpha=0.12,
            line_color="orange", line_width=2, line_dash="dashed",
        )
        # Grid points (locked to motor coords) for every center.
        self._grid_pixel_cds = ColumnDataSource(data={"x": [], "y": []})
        self._grid_renderer = fig.scatter(
            "x", "y", source=self._grid_pixel_cds,
            marker="circle", size=4, color="orange", alpha=0.85,
        )

        self.tools = ()  # no toolbar tools needed

        # ---- side panel widgets ----
        units = cfg.ui.motor_units
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        default_dim = 500.0 if is_um else 0.5
        increment = 5.0 if is_um else 0.005

        self._width = pn.widgets.FloatInput(
            name=f"width ({units})", value=default_dim, step=increment, width=140,
        )
        self._height = pn.widgets.FloatInput(
            name=f"height ({units})", value=default_dim, step=increment, width=140,
        )
        self._step_x = pn.widgets.FloatInput(
            name=f"step x ({units})", value=cfg.ui.default_step, step=increment, width=130,
        )
        self._step_y = pn.widgets.FloatInput(
            name=f"step y ({units})", value=cfg.ui.default_step, step=increment, width=130,
        )
        for w in (self._width, self._height, self._step_x, self._step_y):
            w.param.watch(self._on_param_change, "value")

        # "Add point": while ON, image clicks drop pos* bookmarks (shared) at the click.
        self._add_toggle = make_latching_toggle("Add point", value=False, width=170)
        self._add_toggle.param.watch(self._on_add_toggle, "value")
        self._clear_placed_btn = pn.widgets.Button(
            name="Clear placed points", button_type="default", width=170,
        )
        self._clear_placed_btn.on_click(lambda _e: self._store.clear_placed_points())
        self._count = pn.widgets.StaticText(value="", width=380)

        self.panel = pn.Column(
            pn.pane.Markdown("### Square (rectangle) scan"),
            pn.pane.Markdown(
                "Pick a width, height, and per-axis step. Tick **scan** on samples in the "
                "**Sample list** (sidebar) to place the rectangle on each, and/or turn on **Add "
                "point** and click the image to drop new spots. Enable the overlay with the "
                "**Square** toggle in the Quick scripts panel."
            ),
            pn.Row(self._width, self._height),
            pn.Row(self._step_x, self._step_y),
            pn.Row(self._add_toggle, self._clear_placed_btn),
            self._count,
        )

        self._refresh_pixels()

        # Keep our overlays + script in sync whenever the shared bookmark set changes.
        self._store.add_bookmark_listener(self.on_bookmarks_changed)

    # ---- Mode protocol --------------------------------------------------------------

    def set_on_change(self, callback) -> None:
        """Register a no-arg callback fired when this mode's scan set changes."""
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
        # Don't leave "Add point" armed across tabs.
        if self._add_toggle.value:
            self._add_toggle.value = False
        # Leave renderers up so the planned grid stays visible from the other tabs.

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
        """Called by InteractiveMode when bookmarks are added / removed / toggled."""
        self._refresh_pixels()
        self._notify()

    # ---- script panel hooks ---------------------------------------------------------

    def script_text(self) -> str:
        nx, ny = self._counts()
        dims = self._dims_valid()
        w = dims[0] if dims else 0.0
        h = dims[1] if dims else 0.0
        return square_grid_scans_at_bookmarks(
            self._scan_targets(), w, h, nx, ny, scripts_cfg=self.cfg.scripts,
        )

    def total_points(self) -> int:
        dims = self._dims_valid()
        if dims is None:
            return 0
        nx, ny = self._counts()
        return len(self._scan_targets()) * nx * ny

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

    def _dims_valid(self) -> tuple[float, float, float, float] | None:
        w = float(self._width.value or 0.0)
        h = float(self._height.value or 0.0)
        sx = float(self._step_x.value or 0.0)
        sy = float(self._step_y.value or 0.0)
        if w <= 0 or h <= 0 or sx <= 0 or sy <= 0:
            return None
        return w, h, sx, sy

    def _counts(self) -> tuple[int, int]:
        dims = self._dims_valid()
        if dims is None:
            return 1, 1
        w, h, sx, sy = dims
        nx = max(1, int(round(w / sx)) + 1)
        ny = max(1, int(round(h / sy)) + 1)
        return nx, ny

    def _grid_motor_points(self, cx: float, cy: float, z: float) -> list[tuple[float, float, float]]:
        dims = self._dims_valid()
        if dims is None:
            return []
        w, h, _, _ = dims
        nx, ny = self._counts()
        xs = np.linspace(cx - w / 2, cx + w / 2, nx)
        ys = np.linspace(cy - h / 2, cy + h / 2, ny)
        return [(float(x), float(y), float(z)) for y in ys for x in xs]

    def _refresh_pixels(self) -> None:
        centers = self._centers()
        dims = self._dims_valid()
        if not self._enabled or not centers or dims is None:
            self._rect_cds.data = {"xs": [], "ys": []}
            self._grid_pixel_cds.data = {"x": [], "y": []}
            if not self._enabled:
                self._count.value = "overlay off — enable 'Square' in the Quick scripts panel"
            else:
                self._count.value = self._status_when_empty(bool(centers), dims is not None)
            return

        w, h, _, _ = dims
        m_now = self._safe_motor_now()
        beam_px = self.beam.center
        A = self.calibration.matrix

        rect_xs: list[list[float]] = []
        rect_ys: list[list[float]] = []
        grid_xs: list[float] = []
        grid_ys: list[float] = []
        for _name, cx, cy, z in centers:
            corners_motor = [
                (cx - w / 2, cy - h / 2),
                (cx + w / 2, cy - h / 2),
                (cx + w / 2, cy + h / 2),
                (cx - w / 2, cy + h / 2),
            ]
            cxs: list[float] = []
            cys: list[float] = []
            for mx, my in corners_motor:
                dp = A @ np.array([m_now[0] - mx, m_now[1] - my], dtype=float)
                cxs.append(beam_px[0] + float(dp[0]))
                cys.append(beam_px[1] + float(dp[1]))
            rect_xs.append(cxs)
            rect_ys.append(cys)
            for mx, my, _z in self._grid_motor_points(cx, cy, z):
                dp = A @ np.array([m_now[0] - mx, m_now[1] - my], dtype=float)
                grid_xs.append(beam_px[0] + float(dp[0]))
                grid_ys.append(beam_px[1] + float(dp[1]))

        self._rect_cds.data = {"xs": rect_xs, "ys": rect_ys}
        self._grid_pixel_cds.data = {"x": grid_xs, "y": grid_ys}

        nx, ny = self._counts()
        per = nx * ny
        self._count.value = (
            f"{len(centers)} target(s) × {per:,} pts ({nx} × {ny}) = "
            f"{len(centers) * per:,} pts total"
        )

    def _status_when_empty(self, has_centers: bool, dims_ok: bool) -> str:
        if not dims_ok:
            return "set valid dimensions and steps"
        return "no scan targets — tick a bookmark above, or use 'Add point'"

    def _safe_motor_now(self) -> tuple[float, float]:
        try:
            return (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            return (0.0, 0.0)
