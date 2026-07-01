"""Interactive mode: click-to-move + the headless sample-marker / scan-target engine.

This mode owns two things:

    1. **Click-to-move** (the visible Move/explore panel): click an image feature to preview a
       motor delta (cyan crosshair); click again within tolerance to commit the move. Clicking
       far from the preview restarts it; Esc cancels.

    2. **The sample/reference marker + scan-target engine** (headless — *no table of its own*).
       The single visible sample list is the host app's master "Sample list" (the redis-backed
       spine). That list is the one source of truth: the host pushes its samples + references
       into this engine via :meth:`set_samples`, which drives the on-image lime/yellow markers
       and the per-sample ``in_scan`` flags the Scan tabs replicate onto. Moving the stage to a
       listed position is :meth:`goto`. There is intentionally no way to add a sample here that
       bypasses the master list.
"""

from __future__ import annotations

import re

import panel as pn
from bokeh.models import ColumnDataSource, LabelSet
from bokeh.plotting import figure as Figure  # noqa: N812 — type alias

from ..calibration import CalibrationModel
from ..config import AppConfig
from ..devices import SampleStage
from ..overlays import BeamOverlay
from ..scripts import Bookmark, bookmark_list_scan_snippet
from ..wide_view import _alpha_for_distance

# A second click within this many pixels of the first commits; further away starts a new preview.
_COMMIT_TOLERANCE_PX = 20.0
_PLANE_EPS = 1e-3

# Click-placed scan points are auto-named pos1, pos2, … — this matches (and only) those, so
# "Clear placed points" can remove them without touching user-named bookmarks.
_POS_NAME_RE = re.compile(r"^pos\d+$")


def _in_view(px: float, py: float, w: float, h: float, margin: float = 6.0) -> bool:
    return -margin <= px <= w + margin and -margin <= py <= h + margin


class InteractiveMode:
    name = "Move"
    # Overlay family / color for the persistent script panel (green = sample-list scan).
    script_kind = "Sample list"
    script_color = "#2ecc71"
    script_explanation = (
        "`list_scan` visiting every scan-ticked sample in the master Sample list (references "
        "excluded), one event per spot, filenames driven from each sample's name."
    )

    def __init__(
        self,
        fig: Figure,
        stage: SampleStage,
        beam_overlay: BeamOverlay,
        calibration: CalibrationModel,
        cfg: AppConfig,
        image_size_hint_provider,
        executor=None,
        huber_calibration: "CalibrationModel | None" = None,
        map_active_provider=None,
    ) -> None:
        self.fig = fig
        self.stage = stage
        self.beam = beam_overlay
        self.calibration = calibration            # piezo affine (the default click-to-move)
        self.huber_calibration = huber_calibration  # Huber affine (range-extender fallback)
        self.cfg = cfg
        self._dims = image_size_hint_provider
        self.executor = executor
        self._map_active_provider = map_active_provider or (lambda: False)
        # Which stack click-to-move drives: "piezo" (fast/precise default) or "huber" (coarse
        # range extender). The piezo rides on the Huber, so each calibration is fit at a given
        # Huber orientation (rotation coupling handled in the deferred 6d work).
        self._move_stack = "piezo"
        self._active = False

        # ---- explore (click-to-move) state ----------------------------------------
        self._pending: tuple[float, float] | None = None
        self._preview_cds = ColumnDataSource(data={"x": [], "y": []})
        self._line_cds = ColumnDataSource(data={"x": [], "y": []})
        self._preview_renderer = fig.scatter(
            "x", "y", source=self._preview_cds, marker="cross",
            size=22, color="cyan", line_width=3,
        )
        self._line_renderer = fig.line(
            "x", "y", source=self._line_cds, color="cyan", line_dash="dashed", line_width=2,
        )
        self._preview_renderer.visible = False
        self._line_renderer.visible = False

        # ---- sample/reference marker + scan-target state --------------------------
        # These lists mirror the host app's master sample list (the redis-backed spine) plus
        # its references. They are *not* edited here — the host pushes the current set in via
        # ``set_samples``. We keep them to drive the on-image markers and the scan-target flags.
        self._bookmarks: list[Bookmark] = []
        self._motor_at_bookmark: list[tuple[float, float, float]] = []
        # Listeners notified whenever the marker/scan set changes. The scan modes register here
        # so their per-sample overlays + scripts stay in sync.
        self._bookmark_listeners: list = []
        # Seed reference bookmarks from the config so a standalone microscope still shows them;
        # the host app overwrites this whole set on its first ``set_samples`` push.
        for ref in cfg.references:
            self._bookmarks.append(
                Bookmark(name=ref.name, x=ref.x, y=ref.y, z=ref.z, is_reference=True)
            )
            self._motor_at_bookmark.append((ref.x, ref.y, ref.z))

        # Two marker layers: regular sample bookmarks (lime circles) vs config references
        # (yellow diamonds) so they're visually distinguishable on the image.
        self._markers_cds = ColumnDataSource(data={"x": [], "y": [], "name": [], "alpha": []})
        self._ref_markers_cds = ColumnDataSource(data={"x": [], "y": [], "name": [], "alpha": []})
        self._oop_cds = ColumnDataSource(data={"x": [], "y": [], "marker": [], "alpha": [], "color": []})
        self._marker_renderer = fig.scatter(
            "x", "y", source=self._markers_cds, marker="circle",
            size=14, fill_color="lime", fill_alpha="alpha", line_color="lime",
            line_alpha="alpha", line_width=2,
        )
        self._ref_marker_renderer = fig.scatter(
            "x", "y", source=self._ref_markers_cds, marker="diamond",
            size=16, fill_color="yellow", fill_alpha="alpha", line_color="yellow",
            line_alpha="alpha", line_width=2,
        )
        self._oop_renderer = fig.scatter(
            "x", "y", marker="marker", source=self._oop_cds, size=12,
            fill_color="color", fill_alpha="alpha", line_color="color", line_alpha="alpha",
        )
        self._label_set = LabelSet(
            x="x", y="y", text="name", source=self._markers_cds,
            text_color="lime", text_font_size="10pt", x_offset=10, y_offset=-6,
        )
        self._ref_label_set = LabelSet(
            x="x", y="y", text="name", source=self._ref_markers_cds,
            text_color="yellow", text_font_size="10pt", x_offset=10, y_offset=-6,
        )
        fig.add_layout(self._label_set)
        fig.add_layout(self._ref_label_set)
        self._marker_renderer.visible = False
        self._ref_marker_renderer.visible = False
        self._oop_renderer.visible = False
        self._label_set.visible = False
        self._ref_label_set.visible = False

        # ---- side panel widgets ---------------------------------------------------
        self._proposed = pn.widgets.StaticText(value="click an image feature to preview a move", width=400)
        self._cancel_btn = pn.widgets.Button(name="cancel preview (Esc)", width=160)
        self._cancel_btn.on_click(lambda _e: self._clear_preview())

        # Click-to-move stack selector: piezo (default, fast/precise) vs the Huber coarse stage
        # (range extender; slower — turn it off when not needed). Only offered when the stage
        # exposes a Huber group.
        self._stack_toggle = pn.widgets.RadioButtonGroup(
            name="Click-to-move", options=["piezo", "Huber"], value="piezo",
            button_type="primary", width=200)
        self._stack_toggle.param.watch(self._on_stack_change, "value")
        _has_huber = getattr(self.stage, "huber", None) is not None
        self._stack_row = pn.Row(
            pn.pane.Markdown("**move with:**", width=80), self._stack_toggle,
            visible=_has_huber)

        # The Move/explore tab body (click-to-move only). The sample list itself is the host
        # app's master "Sample list" sidebar — there is no separate list here anymore.
        self.move_panel = pn.Column(
            pn.pane.Markdown("### Move / explore"),
            pn.pane.Markdown(
                "**Click** on the image to preview a move; **click again** within "
                f"{int(_COMMIT_TOLERANCE_PX)} px to commit. Click elsewhere or press Esc to restart."
            ),
            self._stack_row,
            self._proposed,
            self._cancel_btn,
        )
        # Back-compat: ``panel`` is the mode-tab body.
        self.panel = self.move_panel

    # ---- Mode protocol --------------------------------------------------------------

    def set_overlay_enabled(self, enabled: bool) -> None:
        """Per-kind on/off hook (Quick-scripts panel).

        The sample / reference markers double as on-image landmarks, so they stay visible
        regardless; this toggle governs only whether the **Bookmarks** quick-script is shown.
        Accepted for protocol uniformity with the scan modes.
        """
        return

    def activate(self) -> None:
        self._active = True
        self._marker_renderer.visible = True
        self._ref_marker_renderer.visible = True
        self._oop_renderer.visible = True
        self._label_set.visible = True
        self._ref_label_set.visible = True
        self._preview_renderer.visible = bool(self._pending)
        self._line_renderer.visible = bool(self._pending)
        self.tick()

    def deactivate(self) -> None:
        self._active = False
        # Keep sample + reference markers visible across all tabs — they're useful
        # landmarks regardless of what the user is doing right now.
        self._clear_preview()

    def on_tap(self, x: float, y: float) -> None:
        if not self._active:
            return

        if self._pending is None:
            self._show_preview(x, y)
            return

        px, py = self._pending
        # Commit if the new click is within tolerance of the preview, else re-preview here.
        if (x - px) ** 2 + (y - py) ** 2 <= _COMMIT_TOLERANCE_PX ** 2:
            self._commit(px, py)
        else:
            self._show_preview(x, y)

    def tick(self) -> None:
        """Refresh the image-marker CDSes. Runs at the UI poll rate (~10 Hz).

        Two CDSes are kept: regular samples (lime circles) and references (yellow diamonds).
        The sample list display itself is the host app's master list, not rebuilt here.
        """
        if not self._bookmarks:
            self._markers_cds.data = {"x": [], "y": [], "name": [], "alpha": []}
            self._ref_markers_cds.data = {"x": [], "y": [], "name": [], "alpha": []}
            self._oop_cds.data = {"x": [], "y": [], "marker": [], "alpha": [], "color": []}
            return

        try:
            m_now = (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            m_now = (0.0, 0.0)

        beam_px = self.beam.center
        w, h = self._dims()
        reg_x: list[float] = []
        reg_y: list[float] = []
        reg_n: list[str] = []
        reg_a: list[float] = []
        ref_x: list[float] = []
        ref_y: list[float] = []
        ref_n: list[str] = []
        ref_a: list[float] = []
        oop_x: list[float] = []
        oop_y: list[float] = []
        oop_marker: list[str] = []
        oop_alpha: list[float] = []
        oop_color: list[str] = []
        try:
            z_now = float(self.stage.z.position)
        except Exception:
            z_now = 0.0
        for bm, (mx_bm, my_bm, mz_bm) in zip(self._bookmarks, self._motor_at_bookmark):
            dp = self.calibration.motor_to_pixel_delta((m_now[0] - mx_bm, m_now[1] - my_bm))
            px = beam_px[0] + dp[0]
            py = beam_px[1] + dp[1]
            if not self._map_active_provider() and not _in_view(px, py, w, h):
                continue
            alpha = _alpha_for_distance(z_now - mz_bm, 1.0)
            dz = mz_bm - z_now
            if abs(dz) > _PLANE_EPS:
                oop_x.append(px + 14)
                oop_y.append(py - 10 if dz > 0 else py + 10)
                oop_marker.append("triangle" if dz > 0 else "inverted_triangle")
                oop_alpha.append(max(alpha, 0.35))
                oop_color.append("orange" if dz > 0 else "deepskyblue")
            if bm.is_reference:
                ref_x.append(px)
                ref_y.append(py)
                ref_n.append(bm.name)
                ref_a.append(alpha)
            else:
                reg_x.append(px)
                reg_y.append(py)
                reg_n.append(bm.name)
                reg_a.append(alpha)
        self._markers_cds.data = {"x": reg_x, "y": reg_y, "name": reg_n, "alpha": reg_a}
        self._ref_markers_cds.data = {"x": ref_x, "y": ref_y, "name": ref_n, "alpha": ref_a}
        self._oop_cds.data = {
            "x": oop_x, "y": oop_y, "marker": oop_marker,
            "alpha": oop_alpha, "color": oop_color,
        }

    def tick_table(self) -> None:
        """No-op: this mode no longer owns a table (the master Sample list is the host's).

        Kept for Mode-protocol uniformity — the builder's ~1 Hz ``active.tick_table()`` poll
        and the scan modes still rely on the method existing.
        """
        return

    # ---- host bridge: the master sample list drives this engine ----------------------

    def set_samples(self, entries) -> None:
        """Replace the store-sourced samples + references from the host's master list.

        ``entries`` is an iterable of :class:`~smi_acquire.microscope.scripts.Bookmark` (the
        host builds them from its redis spine + project references). Locally click-placed
        ``pos{N}`` scan points are **preserved** (they have no home in the master list yet), so
        the Scan-tab "Add point" placements survive a master-list refresh. Fires the usual
        change notification and redraws the markers.
        """
        placed_pairs = [
            (b, m)
            for b, m in zip(self._bookmarks, self._motor_at_bookmark)
            if _POS_NAME_RE.match(b.name)
        ]
        new_bm: list[Bookmark] = []
        new_mab: list[tuple[float, float, float]] = []
        for e in entries:
            new_bm.append(e)
            new_mab.append((float(e.x), float(e.y), float(e.z)))
        for b, m in placed_pairs:
            new_bm.append(b)
            new_mab.append(m)
        self._bookmarks = new_bm
        self._motor_at_bookmark = new_mab
        self._schedule_ui_refresh()

    def goto(self, name_or_index) -> None:
        """Move the stage (x/y/z) to a listed sample/reference, by name or list index.

        Routes through the executor when present (interlock-gated). Move errors are surfaced
        on the Move-panel proposal line. No-op if the target can't be found.
        """
        bm = self._resolve_bookmark(name_or_index)
        if bm is None:
            return
        self.goto_xyz(bm.x, bm.y, bm.z)

    def goto_xyz(self, x: float, y: float, z: float) -> None:
        """Move the stage (x/y/z) to explicit motor coordinates (interlock-gated).

        The unambiguous form used by the host's master list "go to position" button, where a
        sample and a reference could share a name. Errors surface on the Move-panel line.
        """
        try:
            self._move_axis(self.stage.x, float(x))
            self._move_axis(self.stage.y, float(y))
            self._move_axis(self.stage.z, float(z))
        except Exception as exc:  # noqa: BLE001
            if hasattr(self, "_proposed"):
                self._proposed.value = f"move failed: {exc}"

    def _resolve_bookmark(self, name_or_index):
        if isinstance(name_or_index, int):
            if 0 <= name_or_index < len(self._bookmarks):
                return self._bookmarks[name_or_index]
            return None
        return next((b for b in self._bookmarks if b.name == name_or_index), None)

    # ---- scan-target API (consumed by the square/linear/area scan modes) --------------

    def all_scan_candidates(self) -> list[Bookmark]:
        """Every regular (non-reference) sample — the full set of pickable scan targets.

        Used to populate the scan-tab target lists (both enabled and disabled entries).
        """
        return [b for b in self._bookmarks if not b.is_reference]

    def get_scan_targets(self) -> list[Bookmark]:
        """Regular samples currently enabled for scanning (``in_scan`` ticked).

        This is the live target set the scan modes replicate their shape onto and name the
        generated scans after. References and unticked samples are excluded.
        """
        return [b for b in self._bookmarks if not b.is_reference and b.in_scan]

    # Back-compat alias: the scan modes historically called this to get the bookmarks to
    # replicate onto. That is now precisely the enabled-for-scan set.
    get_scan_bookmarks = get_scan_targets

    def set_scan_selection(self, names) -> None:
        """Set each regular sample's ``in_scan`` from a collection of selected names.

        Driven by the master list's ``scan`` column: samples whose name is in ``names`` are
        enabled, the rest disabled. No-op-safe; notifies on any change.
        """
        wanted = set(names or [])
        changed = False
        for b in self._bookmarks:
            if b.is_reference:
                continue
            new_val = b.name in wanted
            if b.in_scan != new_val:
                b.in_scan = new_val
                changed = True
        if changed:
            self._schedule_ui_refresh()

    def add_point(self, x: float, y: float, z: float) -> str:
        """Add an auto-named ``pos{N}`` scan point (a normal bookmark, ``in_scan=True``).

        Used by the scan tabs' "Add point" placement: a click on the image drops a target
        here. Returns the assigned name. Fires the usual scan-target-changed notification.
        These placed points live only in this engine (not the master list) until cleared.
        """
        # Lowest unused pos-index, so clearing then re-adding restarts cleanly.
        used = {
            int(b.name[3:])
            for b in self._bookmarks
            if _POS_NAME_RE.match(b.name)
        }
        n = 1
        while n in used:
            n += 1
        name = f"pos{n}"
        self._bookmarks.append(Bookmark(name=name, x=float(x), y=float(y), z=float(z)))
        self._motor_at_bookmark.append((float(x), float(y), float(z)))
        self._schedule_ui_refresh()
        return name

    def clear_placed_points(self) -> None:
        """Remove every click-placed ``pos{N}`` point, leaving master-list samples untouched."""
        keep = [
            i for i, b in enumerate(self._bookmarks) if not _POS_NAME_RE.match(b.name)
        ]
        if len(keep) == len(self._bookmarks):
            return
        self._bookmarks = [self._bookmarks[i] for i in keep]
        self._motor_at_bookmark = [self._motor_at_bookmark[i] for i in keep]
        self._schedule_ui_refresh()

    def add_bookmark_listener(self, callback) -> None:
        """Register a no-arg callback fired whenever the sample/scan-target set changes."""
        self._bookmark_listeners.append(callback)

    def _notify_bookmarks_changed(self) -> None:
        for cb in self._bookmark_listeners:
            try:
                cb()
            except Exception:
                pass

    def _schedule_ui_refresh(self) -> None:
        """Redraw markers + notify listeners on the next document tick (lock-safe).

        Bokeh model mutations (CDS data, downstream CodeEditor/overlay updates) need the
        document lock; callers may run outside it, so defer to the next tick when in a session.
        """
        def _apply() -> None:
            self.tick()
            self._notify_bookmarks_changed()

        doc = pn.state.curdoc
        if doc is not None:
            doc.add_next_tick_callback(_apply)
        else:  # outside a server session (e.g. unit tests) — safe to mutate directly
            _apply()

    # ---- explore internals ----------------------------------------------------------

    def _show_preview(self, x: float, y: float) -> None:
        self._pending = (x, y)
        beam_px = self.beam.center
        chi = self._chi_compensation()
        dm = self._active_calibration().click_to_motor_delta((x, y), beam_px, chi_deg=chi)
        self._preview_cds.data = {"x": [x], "y": [y]}
        self._line_cds.data = {"x": [beam_px[0], x], "y": [beam_px[1], y]}
        self._preview_renderer.visible = True
        self._line_renderer.visible = True
        # Units differ by stack: piezo is µm, the Huber coarse stage is mm.
        u = "mm" if self._move_stack == "huber" else self.cfg.ui.motor_units
        chi_note = f"  (χ={chi:+.2f}°)" if abs(chi) > 1e-6 else ""
        self._proposed.value = (
            f"preview [{self._move_stack}] Δx={dm[0]:+.4f} {u}  Δy={dm[1]:+.4f} {u}{chi_note}  "
            f"→ click again to commit"
        )

    def _on_stack_change(self, event) -> None:
        self._move_stack = "huber" if str(event.new).lower() == "huber" else "piezo"
        self._clear_preview()
        if self._move_stack == "huber" and self.huber_calibration is None:
            self._proposed.value = ("Huber click-to-move has no calibration yet — fit it in "
                                    "Calibrate (with 'Huber' selected) for accuracy.")
        else:
            self._proposed.value = "click an image feature to preview a move ({})".format(
                self._move_stack)

    def _active_motors(self):
        """The (x, y) motors of the currently-selected click-to-move stack.

        Defaults to the primary stage.x/.y (the piezo); when 'huber' is selected and the Huber
        group exists, uses stage.huber.x/.y.  Falls back to the primary axes otherwise.
        """
        if self._move_stack == "huber":
            huber = getattr(self.stage, "huber", None)
            if huber is not None and hasattr(huber, "x") and hasattr(huber, "y"):
                return huber.x, huber.y
        return self.stage.x, self.stage.y

    def _active_calibration(self):
        """The affine for the selected stack (Huber one when driving Huber, else the piezo)."""
        if self._move_stack == "huber" and self.huber_calibration is not None:
            return self.huber_calibration
        return self.calibration

    def _move_axis(self, motor, target: float):
        """Move ``motor`` to ``target`` via the executor (interlock-gated) when present.

        Falls back to a direct ophyd ``set`` for the standalone microscope.  Raises on a busy
        interlock (the caller surfaces the message), so a move can't fight an external RunEngine.
        """
        if self.executor is not None:
            return self.executor.move_abs(motor, target)
        return motor.set(target)

    def _commit(self, x: float, y: float) -> None:
        beam_px = self.beam.center
        chi = self._chi_compensation()
        dm = self._active_calibration().click_to_motor_delta((x, y), beam_px, chi_deg=chi)
        mx, my = self._active_motors()
        try:
            target_x = float(mx.position) + float(dm[0])
            target_y = float(my.position) + float(dm[1])
            self._move_axis(mx, target_x)
            self._move_axis(my, target_y)
            self._proposed.value = (
                f"moving [{self._move_stack}]: x→{target_x:+.4f}, y→{target_y:+.4f}  "
                f"(use the Sample list → '★ new sample here' to record this spot)"
            )
        except Exception as exc:  # noqa: BLE001
            self._proposed.value = f"move failed: {exc}"
        self._clear_preview()

    def _chi_compensation(self) -> float:
        """Current in-plane chi (deg) to rotate the active stack's click mapping by (0 if N/A)."""
        try:
            return self.stage.chi_for_stack(self._move_stack)
        except Exception:
            return 0.0

    def _clear_preview(self) -> None:
        self._pending = None
        self._preview_cds.data = {"x": [], "y": []}
        self._line_cds.data = {"x": [], "y": []}
        self._preview_renderer.visible = False
        self._line_renderer.visible = False

    # ---- bookmark internals --------------------------------------------------------

    def script_text(self) -> str:
        """Sample list-scan plan for the persistent script panel. Drops references."""
        return bookmark_list_scan_snippet(self._bookmarks, scripts_cfg=self.cfg.scripts)

    def total_points(self) -> int:
        # Sample list scans aren't time-estimated (operator drives them interactively).
        return 0
