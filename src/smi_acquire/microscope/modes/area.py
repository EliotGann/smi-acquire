"""Polygon scan mode (under the Scan tab).

A polygon is the source of truth in **motor space** — drawn in pixel space initially, but
immediately captured as a list of motor coordinates and replayed every tick. The on-image
polygon and the orange grid points are derived views that track the sample as motors move,
exactly the way bookmarks do.

Placement:

- Tick **scan** on bookmarks in the shared **Bookmarks** list: the drawn polygon's grid is
  replicated — and scanned — centered on each enabled bookmark.
- With no bookmarks enabled, the single drawn polygon/grid is scanned as-is. **Recenter at
  current beam position** relocates it under the beam; **Clear polygon** removes it.
- **Add point** (latching): turn it on to drop ``pos1`` / ``pos2`` … bookmarks by clicking the
  image (the polygon-draw tool is paused while it's on); **Clear placed points** removes them.

Enable the orange overlay independently with the **Polygon** toggle in the Quick scripts panel.

Workflow
--------
1. Switch to **Scan → Polygon** and turn on the **Polygon** toggle. The PolyDraw tool is
   auto-selected; click vertices, double-click to close, then drag to refine.
2. Type step_x / step_y; the grid auto-regenerates. The bluesky list_scan snippet (in the
   Quick scripts panel under the image) updates live.
"""

from __future__ import annotations

import logging

import numpy as np
import panel as pn
from bokeh.models import ColumnDataSource, PolyDrawTool, PolyEditTool
from bokeh.plotting import figure as Figure  # noqa: N812 — type alias

from ..calibration import CalibrationModel
from ..config import AppConfig
from ..devices import SampleStage
from ..geometry import grid_in_polygon, pixel_to_motor
from ..overlays import BeamOverlay
from ..scripts import area_list_scan_snippet, polygon_scans_at_bookmarks
from ..widgets.toggle import make_latching_toggle, style_latching_toggle

log = logging.getLogger(__name__)


class AreaMode:
    name = "Polygon"
    # Overlay family / color for the persistent script panel (orange = grid scans).
    script_kind = "Polygon"
    script_color = "#e8920c"
    script_explanation = (
        "`list_scan` over the polygon-filtered grid. With bookmarks ticked under **Scan at "
        "bookmarks**, the same shape is replicated onto each; otherwise the single drawn area "
        "is scanned."
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
        # Central bookmark store (InteractiveMode). The drawn shape is replicated onto its
        # in_scan bookmarks; "Add point" placement adds points to it.
        self._store = bookmark_store
        self._active = False
        # Per-kind overlay on/off (Quick-scripts panel toggle). When off, the polygon, its grid
        # and the per-bookmark replicas are cleared so each scan kind shows independently.
        self._enabled = False
        # Notified when this mode's scan set changes (so the script panel can refresh).
        self._on_change = lambda: None
        # Pushes the PolyDraw active/inactive state to the *client* (Bokeh doesn't reliably
        # sync server-side toolbar mutations after first render). Wired by the app to a
        # CustomJS trigger; called with True to select the draw tool, False to release it.
        self._draw_tool_sync = lambda _active: None

        # Polygon source of truth = motor coords. The pixel CDS is recomputed every tick.
        self._polygon_motor: list[tuple[float, float]] | None = None
        # Suppress _on_poly_change while we update the pixel CDS ourselves (avoids loops).
        self._suppress_poly_change = False

        # Grid (motor coords) — the actual scan path for the *drawn* polygon. Pixel CDS for
        # display is derived (and replicated across bookmarks in "each bookmark" mode).
        self._grid_motor: list[tuple[float, float, float]] = []
        self._grid_was_truncated = False

        # Polygon pixel CDS — driven by PolyDrawTool / PolyEditTool AND our motor projection.
        self._poly_cds = ColumnDataSource(data={"xs": [], "ys": []})
        self._poly_renderer = fig.patches(
            xs="xs", ys="ys", source=self._poly_cds,
            fill_color="orange", fill_alpha=0.15,
            line_color="orange", line_width=2,
        )

        # PolyEditTool draggable vertex helper.
        self._vertex_cds = ColumnDataSource(data={"x": [], "y": []})
        self._vertex_renderer = fig.scatter(
            "x", "y", source=self._vertex_cds, size=8, color="orange", marker="circle",
        )

        # Replica polygon outlines (one patch per bookmark) shown in "each bookmark" mode.
        self._replica_poly_cds = ColumnDataSource(data={"xs": [], "ys": []})
        self._replica_poly_renderer = fig.patches(
            xs="xs", ys="ys", source=self._replica_poly_cds,
            fill_color="orange", fill_alpha=0.08,
            line_color="orange", line_width=1, line_dash="dashed",
        )

        # Grid points pixel CDS — projection of the (possibly replicated) grid.
        self._grid_pixel_cds = ColumnDataSource(data={"x": [], "y": []})
        self._grid_renderer = fig.scatter(
            "x", "y", source=self._grid_pixel_cds,
            marker="circle", size=4, color="orange", alpha=0.75,
        )
        # Renderers stay visible across all tabs (like bookmarks); empty CDSes make them
        # effectively invisible until the user draws something.

        # A distinctive name so the client-side selector can find this exact tool inside the
        # live toolbar (model identity isn't reliable across Panel's embedding/proxying).
        self._draw_tool = PolyDrawTool(
            renderers=[self._poly_renderer], num_objects=1, name="swaxs_poly_draw",
        )
        self._edit_tool = PolyEditTool(
            renderers=[self._poly_renderer], vertex_renderer=self._vertex_renderer,
        )
        self.tools = (self._draw_tool, self._edit_tool)

        self._poly_cds.on_change("data", self._on_poly_change)

        # ---- side panel widgets ----
        units = cfg.ui.motor_units
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        increment = 1.0 if is_um else 0.005

        self._step_x = pn.widgets.FloatInput(
            name=f"step x ({units})", value=cfg.ui.default_step, step=increment, width=130,
        )
        self._step_y = pn.widgets.FloatInput(
            name=f"step y ({units})", value=cfg.ui.default_step, step=increment, width=130,
        )
        self._step_x.param.watch(self._on_step_change, "value")
        self._step_y.param.watch(self._on_step_change, "value")

        self._count = pn.widgets.StaticText(value="draw a polygon to start", width=380)
        self._recenter_btn = pn.widgets.Button(
            name="Recenter at current beam position", button_type="default", width=240,
        )
        self._recenter_btn.on_click(lambda _e: self._recenter_at_current())
        self._clear_btn = pn.widgets.Button(name="Clear polygon", width=140)
        self._clear_btn.on_click(self._on_clear)

        # "Add point": while ON, the PolyDraw tool is paused and image clicks drop pos*
        # bookmarks (shared) at the click instead of adding polygon vertices.
        self._add_toggle = make_latching_toggle("Add point", value=False, width=170)
        self._add_toggle.param.watch(self._on_add_toggle, "value")
        self._clear_placed_btn = pn.widgets.Button(
            name="Clear placed points", button_type="default", width=170,
        )
        self._clear_placed_btn.on_click(lambda _e: self._store.clear_placed_points())

        self.panel = pn.Column(
            pn.pane.Markdown("### Polygon scan"),
            pn.pane.Markdown(
                "Use the **Polygon Draw** tool (auto-selected on this tab) to click vertices "
                "(double-click to close), then **Polygon Edit** to drag vertices. Tick **scan** "
                "on bookmarks in the **Bookmarks** list to replicate the grid onto each; with "
                "none ticked, the single drawn area is scanned. Enable the overlay with the "
                "**Polygon** toggle in the Quick scripts panel."
            ),
            pn.Row(self._step_x, self._step_y),
            self._count,
            pn.Row(self._recenter_btn, self._clear_btn),
            pn.Row(self._add_toggle, self._clear_placed_btn),
        )

        self._refresh_grid_pixels()
        self._refresh_count()

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
        self.tick()

    def set_draw_tool_sync(self, callback) -> None:
        """Register a ``callback(active: bool)`` that selects/releases PolyDraw on the client."""
        self._draw_tool_sync = callback or (lambda _active: None)

    def activate(self) -> None:
        self._active = True
        # Auto-select the polygon draw tool so the user can start clicking vertices —
        # unless "Add point" is on, in which case clicks place bookmarks instead.
        self._sync_draw_tool()
        # Renderers are always visible; just refresh in case motors moved while away.
        self.tick()

    def deactivate(self) -> None:
        self._active = False
        # Don't leave "Add point" armed across tabs (also restores the draw tool next time).
        if self._add_toggle.value:
            self._add_toggle.value = False
        # Leave renderers up so users can see the planned area from the other tabs.

    def on_tap(self, x: float, y: float) -> None:
        # Polygon editing is normally handled by Bokeh tools, not our Tap dispatcher. But
        # while "Add point" is on we pause the draw tool and use taps to place bookmarks.
        if not self._add_toggle.value:
            return
        m = self._pixel_to_motor(x, y)
        if m is None:
            return
        try:
            z = float(self.stage.z.position)
        except Exception:
            z = 0.0
        self._store.add_point(m[0], m[1], z)

    def tick(self) -> None:
        """Re-project the polygon and grid into pixel space using the current motor pos."""
        if not self._enabled:
            self._clear_overlays()
            return
        if self._polygon_motor:
            self._refresh_polygon_pixels()
        self._refresh_grid_pixels()

    def tick_table(self) -> None:
        return

    def on_bookmarks_changed(self) -> None:
        """Called by InteractiveMode when bookmarks are added / removed / toggled."""
        self._refresh_grid_pixels()
        self._refresh_count()
        self._notify()

    # ---- script panel hooks ---------------------------------------------------------

    def script_text(self) -> str:
        if self._replicate():
            return polygon_scans_at_bookmarks(
                self._store.get_scan_targets(), self._grid_offsets(),
                scripts_cfg=self.cfg.scripts,
            )
        return area_list_scan_snippet(self._grid_motor, scripts_cfg=self.cfg.scripts)

    def total_points(self) -> int:
        if self._replicate():
            n_bm = len(self._store.get_scan_targets())
            return n_bm * len(self._grid_motor)
        return len(self._grid_motor)

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass

    # ---- placement ------------------------------------------------------------------

    def _replicate(self) -> bool:
        """Whether to replicate the drawn shape onto bookmarks (vs. scan the single area).

        True when at least one bookmark is enabled for scanning; otherwise the lone drawn
        polygon is used as-is.
        """
        return len(self._store.get_scan_targets()) > 0

    def _on_add_toggle(self, _event) -> None:
        style_latching_toggle(self._add_toggle)
        # Pause the PolyDraw tool while placing points so clicks don't add vertices.
        self._sync_draw_tool()

    def _sync_draw_tool(self) -> None:
        """Select PolyDraw when idle; deselect it while 'Add point' is on.

        Updates both the server-side ``active_drag`` and (via the app-wired callback) the
        client toolbar, because Bokeh doesn't reliably propagate server-side toolbar
        mutations to an already-rendered figure.
        """
        if not self._active:
            return
        want_draw = not self._add_toggle.value
        try:
            if want_draw:
                self.fig.toolbar.active_drag = self._draw_tool
            elif self.fig.toolbar.active_drag is self._draw_tool:
                self.fig.toolbar.active_drag = None
        except Exception:
            pass
        # Push the same decision to the client (the part that actually flips the button).
        try:
            self._draw_tool_sync(want_draw)
        except Exception:
            pass

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

    def _centroid(self) -> tuple[float, float] | None:
        if not self._grid_motor:
            return None
        cx = sum(p[0] for p in self._grid_motor) / len(self._grid_motor)
        cy = sum(p[1] for p in self._grid_motor) / len(self._grid_motor)
        return cx, cy

    def _grid_offsets(self) -> list[tuple[float, float]]:
        """Grid points expressed relative to the grid centroid (for per-bookmark replication)."""
        c = self._centroid()
        if c is None:
            return []
        cx, cy = c
        return [(p[0] - cx, p[1] - cy) for p in self._grid_motor]

    # ---- pixel projection (the "lock to motor" behavior) ----------------------------

    def _project_motor_to_pixel(self, motor: tuple[float, float]) -> tuple[float, float]:
        # Same sign convention as bookmarks (interactive.py): pixel = beam + A · (m_now − V).
        m_now = self._safe_motor_now()
        beam_px = self.beam.center
        dp = self.calibration.matrix @ np.array(
            [m_now[0] - motor[0], m_now[1] - motor[1]], dtype=float
        )
        return beam_px[0] + float(dp[0]), beam_px[1] + float(dp[1])

    def _refresh_polygon_pixels(self) -> None:
        """Recompute the (master) polygon CDS from the stored motor coords."""
        if not self._polygon_motor:
            return
        new_xs: list[float] = []
        new_ys: list[float] = []
        for mp in self._polygon_motor:
            px, py = self._project_motor_to_pixel(mp)
            new_xs.append(px)
            new_ys.append(py)
        self._suppress_poly_change = True
        try:
            self._poly_cds.data = {"xs": [new_xs], "ys": [new_ys]}
        finally:
            self._suppress_poly_change = False

    def _refresh_grid_pixels(self) -> None:
        """Recompute the grid points pixel CDS (and replica polygons in each-bookmark mode)."""
        if not self._enabled or not self._grid_motor:
            self._grid_pixel_cds.data = {"x": [], "y": []}
            self._replica_poly_cds.data = {"xs": [], "ys": []}
            return

        if self._replicate():
            self._refresh_grid_pixels_each_bookmark()
            return

        # Single placement: just project the drawn grid.
        self._replica_poly_cds.data = {"xs": [], "ys": []}
        xs: list[float] = []
        ys: list[float] = []
        for mx, my, _ in self._grid_motor:
            px, py = self._project_motor_to_pixel((mx, my))
            xs.append(px)
            ys.append(py)
        self._grid_pixel_cds.data = {"x": xs, "y": ys}

    def _refresh_grid_pixels_each_bookmark(self) -> None:
        bookmarks = self._store.get_scan_targets()
        offsets = self._grid_offsets()
        poly_off = self._polygon_offsets()
        if not bookmarks or not offsets:
            # Nothing to replicate — clear the replica grid but keep the master polygon up.
            self._grid_pixel_cds.data = {"x": [], "y": []}
            self._replica_poly_cds.data = {"xs": [], "ys": []}
            return

        xs: list[float] = []
        ys: list[float] = []
        rep_xs: list[list[float]] = []
        rep_ys: list[list[float]] = []
        for b in bookmarks:
            for dx, dy in offsets:
                px, py = self._project_motor_to_pixel((b.x + dx, b.y + dy))
                xs.append(px)
                ys.append(py)
            if poly_off:
                pxs: list[float] = []
                pys: list[float] = []
                for dx, dy in poly_off:
                    px, py = self._project_motor_to_pixel((b.x + dx, b.y + dy))
                    pxs.append(px)
                    pys.append(py)
                rep_xs.append(pxs)
                rep_ys.append(pys)
        self._grid_pixel_cds.data = {"x": xs, "y": ys}
        self._replica_poly_cds.data = {"xs": rep_xs, "ys": rep_ys}

    def _refresh_count(self) -> None:
        """Update the per-tab status line (grid point totals)."""
        if not self._grid_motor:
            self._count.value = "draw a polygon to start"
            return
        per = len(self._grid_motor)
        if self._replicate():
            n_bm = len(self._store.get_scan_targets())
            self._count.value = (
                f"{n_bm} bookmark(s) × {per:,} pts = {n_bm * per:,} pts total"
            )
        else:
            self._count.value = f"{per:,} pts (single drawn area — tick bookmarks to replicate)"

    def _polygon_offsets(self) -> list[tuple[float, float]]:
        """Polygon vertices relative to the grid centroid (for replica outlines)."""
        c = self._centroid()
        if c is None or not self._polygon_motor:
            return []
        cx, cy = c
        return [(v[0] - cx, v[1] - cy) for v in self._polygon_motor]

    # ---- user actions ---------------------------------------------------------------

    def _on_step_change(self, _event) -> None:
        self._regenerate_grid()

    def _on_poly_change(self, attr, old, new) -> None:
        # Triggered both by user edits AND by our own _refresh_polygon_pixels. The suppress
        # flag tells them apart.
        if self._suppress_poly_change:
            return

        xs_lists = new.get("xs") or []
        ys_lists = new.get("ys") or []
        if not xs_lists or not ys_lists or len(xs_lists[0]) < 3:
            self._polygon_motor = None
            self._grid_motor = []
            self._grid_pixel_cds.data = {"x": [], "y": []}
            self._replica_poly_cds.data = {"xs": [], "ys": []}
            self._count.value = "draw a polygon to start"
            self._notify()
            return

        # Snapshot motor coords from the current pixel polygon and the live motor position.
        m_now = self._safe_motor_now()
        beam_px = self.beam.center
        A_inv = np.linalg.inv(self.calibration.matrix)
        self._polygon_motor = [
            pixel_to_motor((float(px), float(py)), beam_px, m_now, A_inv)
            for px, py in zip(xs_lists[0], ys_lists[0])
        ]
        self._regenerate_grid()

    def _on_clear(self, _event) -> None:
        self._suppress_poly_change = True
        try:
            self._poly_cds.data = {"xs": [], "ys": []}
        finally:
            self._suppress_poly_change = False
        self._polygon_motor = None
        self._grid_motor = []
        self._grid_pixel_cds.data = {"x": [], "y": []}
        self._replica_poly_cds.data = {"xs": [], "ys": []}
        self._count.value = "draw a polygon to start"
        self._notify()

    def _recenter_at_current(self) -> None:
        """Shift the polygon (in motor space) so its centroid is at the current motor position."""
        if not self._polygon_motor:
            self._count.value = "no polygon to recenter — draw one first"
            return
        cx = sum(p[0] for p in self._polygon_motor) / len(self._polygon_motor)
        cy = sum(p[1] for p in self._polygon_motor) / len(self._polygon_motor)
        m_now = self._safe_motor_now()
        dx = m_now[0] - cx
        dy = m_now[1] - cy
        self._polygon_motor = [(p[0] + dx, p[1] + dy) for p in self._polygon_motor]
        self._regenerate_grid()
        self._refresh_polygon_pixels()

    # ---- grid generation ------------------------------------------------------------

    def _regenerate_grid(self) -> None:
        if not self._polygon_motor or len(self._polygon_motor) < 3:
            return
        step_x = float(self._step_x.value or 0.0)
        step_y = float(self._step_y.value or 0.0)
        max_pts = self.cfg.ui.max_grid_points
        points, truncated = grid_in_polygon(
            self._polygon_motor, step_x, step_y, max_pts
        )
        try:
            z_now = float(self.stage.z.position)
        except Exception:
            z_now = 0.0
        self._grid_motor = [(p[0], p[1], z_now) for p in points]
        self._grid_was_truncated = truncated

        self._refresh_grid_pixels()
        units = self.cfg.ui.motor_units
        if truncated and not points:
            self._count.value = (
                f"too dense — {step_x:.4g} {units} × {step_y:.4g} {units} bbox > "
                f"{5*max_pts:,} pts. Increase steps."
            )
        elif truncated:
            self._count.value = f"{len(points):,} pts/shape (capped at {max_pts:,})"
        else:
            self._refresh_count()
        self._notify()

    # ---- helpers --------------------------------------------------------------------

    def _clear_overlays(self) -> None:
        """Blank every orange CDS (master polygon, grid, replicas) — used when the kind is off."""
        self._suppress_poly_change = True
        try:
            self._poly_cds.data = {"xs": [], "ys": []}
        finally:
            self._suppress_poly_change = False
        self._grid_pixel_cds.data = {"x": [], "y": []}
        self._replica_poly_cds.data = {"xs": [], "ys": []}

    def _safe_motor_now(self) -> tuple[float, float]:
        try:
            return (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            return (0.0, 0.0)
