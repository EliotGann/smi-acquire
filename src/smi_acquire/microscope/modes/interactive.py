"""Interactive mode: combined click-to-move + bookmark capture.

Workflow:
    1. Click on a sample feature in the image → cyan preview crosshair appears + the proposed
       motor delta shows in the side panel.
    2. Click again on or near that preview → motors move.
    3. While the motors finish (or anytime after), type a name into the **bookmark name**
       input and press Enter to add a bookmark at the current motor position. Pressing ``b``
       from anywhere on the page focuses that input.

The Esc key blurs the input. Clicking far from the preview restarts the preview at the new
spot rather than committing to the old one.
"""

from __future__ import annotations

import re
import time

import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource, LabelSet
from bokeh.plotting import figure as Figure  # noqa: N812 — type alias

from ..calibration import CalibrationModel
from ..config import AppConfig, ReferenceBookmark
from ..devices import SampleStage
from ..overlays import BeamOverlay
from ..scripts import Bookmark, bookmark_list_scan_snippet

# A second click within this many pixels of the first commits; further away starts a new preview.
_COMMIT_TOLERANCE_PX = 20.0

# Click-placed scan points are auto-named pos1, pos2, … — this matches (and only) those, so
# "Clear placed points" can remove them without touching user-named bookmarks.
_POS_NAME_RE = re.compile(r"^pos\d+$")


def _in_view(px: float, py: float, w: float, h: float, margin: float = 6.0) -> bool:
    return -margin <= px <= w + margin and -margin <= py <= h + margin


class InteractiveMode:
    name = "Move"
    # Overlay family / color for the persistent script panel (green = bookmark list scan).
    script_kind = "Bookmarks"
    script_color = "#2ecc71"
    script_explanation = (
        "`list_scan` visiting every saved sample bookmark (references excluded), one event "
        "per spot, filenames driven from each bookmark's name."
    )

    def __init__(
        self,
        fig: Figure,
        stage: SampleStage,
        beam_overlay: BeamOverlay,
        calibration: CalibrationModel,
        cfg: AppConfig,
        image_size_hint_provider,
    ) -> None:
        self.fig = fig
        self.stage = stage
        self.beam = beam_overlay
        self.calibration = calibration
        self.cfg = cfg
        self._dims = image_size_hint_provider
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

        # ---- bookmark state -------------------------------------------------------
        self._bookmarks: list[Bookmark] = []
        self._motor_at_bookmark: list[tuple[float, float, float]] = []
        # Listeners notified whenever the bookmark set changes (add / remove / ref toggle).
        # The scan modes register here so their per-bookmark overlays + scripts stay in sync.
        self._bookmark_listeners: list = []
        # Seed reference bookmarks from the config so they survive sessions.
        for ref in cfg.references:
            self._bookmarks.append(
                Bookmark(name=ref.name, x=ref.x, y=ref.y, z=ref.z, is_reference=True)
            )
            self._motor_at_bookmark.append((ref.x, ref.y, ref.z))
        # Panel can fire the TextInput value watcher twice for one Enter press
        # (ValueChanged + a follow-up sync). Two guards:
        #   - ``_adding_pending``: in-flight bit cleared by the next-tick UI refresh.
        #   - ``_last_submission``: (name, monotonic_time) memo; rejects the same name within
        #     a 0.5 s window. User can intentionally re-add the same name after that window.
        self._adding_pending = False
        self._last_submission: tuple[str, float] | None = None

        # Two marker layers: regular sample bookmarks (lime circles) vs config references
        # (yellow diamonds) so they're visually distinguishable on the image.
        self._markers_cds = ColumnDataSource(data={"x": [], "y": [], "name": []})
        self._ref_markers_cds = ColumnDataSource(data={"x": [], "y": [], "name": []})
        self._marker_renderer = fig.scatter(
            "x", "y", source=self._markers_cds, marker="circle",
            size=14, fill_color="lime", fill_alpha=0.5, line_color="lime", line_width=2,
        )
        self._ref_marker_renderer = fig.scatter(
            "x", "y", source=self._ref_markers_cds, marker="diamond",
            size=16, fill_color="yellow", fill_alpha=0.5, line_color="yellow", line_width=2,
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
        self._label_set.visible = False
        self._ref_label_set.visible = False

        # ---- side panel widgets ---------------------------------------------------
        self._name_input = pn.widgets.TextInput(
            name="bookmark name",
            placeholder="type & Enter to bookmark current position  (press 'b' from anywhere to focus)",
            css_classes=["bookmark-name"],
            sizing_mode="stretch_width",
        )
        # TextInput fires its `value` param when the user presses Enter.
        self._name_input.param.watch(self._on_name_enter, "value")

        self._proposed = pn.widgets.StaticText(value="click an image feature to preview a move", width=400)
        self._cancel_btn = pn.widgets.Button(name="cancel preview (Esc)", width=160)
        self._cancel_btn.on_click(lambda _e: self._clear_preview())

        # Checkbox column for row selection. Two editable tickCross columns:
        #   - "scan": whether this bookmark is a scan target (shared with the scan-tab
        #     target lists). References can't be scanned, so their box is forced off.
        #   - "ref":  flips the bookmark between regular and reference (config-persisted).
        self._table = pn.widgets.Tabulator(
            value=pd.DataFrame(columns=["name", "x", "y", "z", "scan", "ref", "status"]),
            selectable="checkbox-single",
            show_index=False,
            height=240,
            theme="simple",
            pagination=None,
            widths={"name": 84, "x": 64, "y": 64, "z": 64, "scan": 48, "ref": 44, "status": 86},
            editors={"scan": {"type": "tickCross"}, "ref": {"type": "tickCross"}},
            formatters={"scan": {"type": "tickCross"}, "ref": {"type": "tickCross"}},
        )
        self._table.on_edit(self._on_cell_edit)
        self._goto_btn = pn.widgets.Button(name="move to selected", width=160)
        self._goto_btn.on_click(self._on_goto)
        self._remove_btn = pn.widgets.Button(name="remove selected", button_type="danger", width=160)
        self._remove_btn.on_click(self._on_remove)

        # The single, shared bookmark list (the only bookmark display). It is folded into the
        # **Move** tab alongside the host's capture-position controls (which provide the name
        # field + "new sample here" capture). The ``scan`` column is the per-kind scan-target
        # selection the scan modes replicate their shape onto.
        self.bookmark_panel = pn.Column(
            pn.pane.Markdown("### Bookmarks"),
            self._table,
            pn.Row(self._goto_btn, self._remove_btn),
            pn.pane.Markdown(
                "<span style='color:#888;font-size:12px'>Tick <b>scan</b> to make a bookmark a "
                "target for the enabled scan kinds; <b>ref</b> marks a fixed landmark.</span>"
            ),
        )

        # The Move/explore tab body (click-to-move only — the bookmark list lives on the far
        # right now, shared across all tabs).
        self.move_panel = pn.Column(
            pn.pane.Markdown("### Move / explore"),
            pn.pane.Markdown(
                "**Click** on the image to preview a move; **click again** within "
                f"{int(_COMMIT_TOLERANCE_PX)} px to commit. Click elsewhere or press Esc to restart."
            ),
            self._proposed,
            self._cancel_btn,
        )
        # Back-compat: ``panel`` is the mode-tab body.
        self.panel = self.move_panel

    # ---- Mode protocol --------------------------------------------------------------

    def set_overlay_enabled(self, enabled: bool) -> None:
        """Per-kind on/off hook (Quick-scripts panel).

        The bookmark / reference markers double as on-image landmarks, so they stay visible
        regardless; this toggle governs only whether the **Bookmarks** quick-script is shown.
        Accepted for protocol uniformity with the scan modes.
        """
        return

    def activate(self) -> None:
        self._active = True
        self._marker_renderer.visible = True
        self._ref_marker_renderer.visible = True
        self._label_set.visible = True
        self._ref_label_set.visible = True
        self._preview_renderer.visible = bool(self._pending)
        self._line_renderer.visible = bool(self._pending)
        self.tick()
        self.tick_table()

    def deactivate(self) -> None:
        self._active = False
        # Keep bookmark + reference markers visible across all tabs — they're useful
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
        """Fast refresh: image-marker CDSes. Runs at the UI poll rate (~10 Hz).

        We intentionally do NOT rebuild ``self._table.value`` here — replacing a Tabulator's
        DataFrame at 10 Hz tears down and rebuilds the row DOM fast enough that checkbox
        clicks never get a chance to register. The table is refreshed by ``tick_table`` at
        ~1 Hz (and explicitly on add/remove).

        Two CDSes are kept: regular bookmarks (lime) and reference bookmarks (yellow).
        """
        if not self._bookmarks:
            self._markers_cds.data = {"x": [], "y": [], "name": []}
            self._ref_markers_cds.data = {"x": [], "y": [], "name": []}
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
        ref_x: list[float] = []
        ref_y: list[float] = []
        ref_n: list[str] = []
        for bm, (mx_bm, my_bm, _) in zip(self._bookmarks, self._motor_at_bookmark):
            dp = self.calibration.motor_to_pixel_delta((m_now[0] - mx_bm, m_now[1] - my_bm))
            px = beam_px[0] + dp[0]
            py = beam_px[1] + dp[1]
            if not _in_view(px, py, w, h):
                continue
            if bm.is_reference:
                ref_x.append(px)
                ref_y.append(py)
                ref_n.append(bm.name)
            else:
                reg_x.append(px)
                reg_y.append(py)
                reg_n.append(bm.name)
        self._markers_cds.data = {"x": reg_x, "y": reg_y, "name": reg_n}
        self._ref_markers_cds.data = {"x": ref_x, "y": ref_y, "name": ref_n}

    def all_scan_candidates(self) -> list[Bookmark]:
        """Every regular (non-reference) bookmark — the full set of pickable scan targets.

        Used to populate the scan-tab target lists (both enabled and disabled entries).
        """
        return [b for b in self._bookmarks if not b.is_reference]

    def get_scan_targets(self) -> list[Bookmark]:
        """Regular bookmarks currently enabled for scanning (``in_scan`` ticked).

        This is the live target set the scan modes replicate their shape onto and name the
        generated scans after. References and unticked bookmarks are excluded.
        """
        return [b for b in self._bookmarks if not b.is_reference and b.in_scan]

    # Back-compat alias: the scan modes historically called this to get the bookmarks to
    # replicate onto. That is now precisely the enabled-for-scan set.
    get_scan_bookmarks = get_scan_targets

    def set_scan_selection(self, names) -> None:
        """Set each regular bookmark's ``in_scan`` from a collection of selected names.

        Drives the per-bookmark scan-target list widgets: bookmarks whose name is in
        ``names`` are enabled, the rest disabled. No-op-safe; notifies on any change.
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
        here. Returns the assigned name. Fires the usual bookmark-changed notification.
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
        """Remove every click-placed ``pos{N}`` point, leaving named bookmarks untouched."""
        keep = [
            i for i, b in enumerate(self._bookmarks) if not _POS_NAME_RE.match(b.name)
        ]
        if len(keep) == len(self._bookmarks):
            return
        self._bookmarks = [self._bookmarks[i] for i in keep]
        self._motor_at_bookmark = [self._motor_at_bookmark[i] for i in keep]
        self._schedule_ui_refresh()

    def add_bookmark_listener(self, callback) -> None:
        """Register a no-arg callback fired whenever the bookmark set changes."""
        self._bookmark_listeners.append(callback)

    def _notify_bookmarks_changed(self) -> None:
        for cb in self._bookmark_listeners:
            try:
                cb()
            except Exception:
                pass

    def tick_table(self) -> None:
        """Slow refresh: rebuild the Tabulator value. Runs at ~1 Hz and on add/remove."""
        try:
            m_now = (float(self.stage.x.position), float(self.stage.y.position))
        except Exception:
            m_now = (0.0, 0.0)
        beam_px = self.beam.center
        w, h = self._dims()
        rows: list[dict] = []
        for bm, (mx_bm, my_bm, _) in zip(self._bookmarks, self._motor_at_bookmark):
            dp = self.calibration.motor_to_pixel_delta((m_now[0] - mx_bm, m_now[1] - my_bm))
            px = beam_px[0] + dp[0]
            py = beam_px[1] + dp[1]
            in_view = _in_view(px, py, w, h)
            rows.append(
                {
                    "name": bm.name,
                    "x": round(bm.x, 4),
                    "y": round(bm.y, 4),
                    "z": round(bm.z, 4),
                    "scan": (not bm.is_reference) and bool(bm.in_scan),
                    "ref": bool(bm.is_reference),
                    "status": "in view" if in_view else f"px ({px:.0f},{py:.0f})",
                }
            )
        self._table.value = pd.DataFrame(
            rows, columns=["name", "x", "y", "z", "scan", "ref", "status"]
        )

    # ---- explore internals ----------------------------------------------------------

    def _show_preview(self, x: float, y: float) -> None:
        self._pending = (x, y)
        beam_px = self.beam.center
        dm = self.calibration.click_to_motor_delta((x, y), beam_px)
        self._preview_cds.data = {"x": [x], "y": [y]}
        self._line_cds.data = {"x": [beam_px[0], x], "y": [beam_px[1], y]}
        self._preview_renderer.visible = True
        self._line_renderer.visible = True
        u = self.cfg.ui.motor_units
        self._proposed.value = (
            f"preview Δx={dm[0]:+.4f} {u}  Δy={dm[1]:+.4f} {u}  → click again to commit"
        )

    def _commit(self, x: float, y: float) -> None:
        beam_px = self.beam.center
        dm = self.calibration.click_to_motor_delta((x, y), beam_px)
        try:
            target_x = float(self.stage.x.position) + float(dm[0])
            target_y = float(self.stage.y.position) + float(dm[1])
            self.stage.x.set(target_x)
            self.stage.y.set(target_y)
            self._proposed.value = (
                f"moving: x→{target_x:+.4f}, y→{target_y:+.4f}  "
                f"(press 'b' then a name + Enter to bookmark this spot)"
            )
        except Exception as exc:  # noqa: BLE001
            self._proposed.value = f"move failed: {exc}"
        self._clear_preview()

    def _clear_preview(self) -> None:
        self._pending = None
        self._preview_cds.data = {"x": [], "y": []}
        self._line_cds.data = {"x": [], "y": []}
        self._preview_renderer.visible = False
        self._line_renderer.visible = False

    # ---- bookmark internals --------------------------------------------------------

    def _on_name_enter(self, event) -> None:
        name = (event.new or "").strip()
        if not name:
            return
        if self._adding_pending:
            return  # in-flight; drop duplicate event for the same Enter press
        now = time.monotonic()
        if self._last_submission is not None:
            last_name, last_time = self._last_submission
            if last_name == name and (now - last_time) < 0.5:
                return  # same name within 0.5 s — Panel double-fire, drop it
        try:
            x = float(self.stage.x.position)
            y = float(self.stage.y.position)
            z = float(self.stage.z.position)
        except Exception:
            return
        self._last_submission = (name, now)
        existing = {b.name for b in self._bookmarks}
        base = name
        i = 2
        while name in existing:
            name = f"{base}_{i}"
            i += 1
        self._bookmarks.append(Bookmark(name=name, x=x, y=y, z=z))
        self._motor_at_bookmark.append((x, y, z))
        self._adding_pending = True
        # Bokeh model mutations (TextInput value, CDS data, Tabulator value, CodeEditor value)
        # need the document lock — Panel's event coroutine fires this watcher without it, so
        # defer the UI side to the next document tick.
        self._schedule_ui_refresh(clear_name_input=True)

    def _schedule_ui_refresh(self, clear_name_input: bool = False) -> None:
        def _apply() -> None:
            if clear_name_input:
                self._name_input.value = ""
            self.tick()
            self.tick_table()  # rebuild table now so user sees the new row immediately
            self._adding_pending = False
            self._notify_bookmarks_changed()

        doc = pn.state.curdoc
        if doc is not None:
            doc.add_next_tick_callback(_apply)
        else:  # outside a server session (e.g. unit tests) — safe to mutate directly
            _apply()

    def _on_remove(self, _event) -> None:
        sel = list(self._table.selection or [])
        if not sel:
            return
        # If we're removing any references, persist the new (smaller) reference list.
        removed_any_ref = any(self._bookmarks[i].is_reference for i in sel if i < len(self._bookmarks))
        keep = [i for i in range(len(self._bookmarks)) if i not in sel]
        self._bookmarks = [self._bookmarks[i] for i in keep]
        self._motor_at_bookmark = [self._motor_at_bookmark[i] for i in keep]
        if removed_any_ref:
            self._persist_references()

        def _apply() -> None:
            self._table.selection = []
            self.tick()
            self.tick_table()
            self._notify_bookmarks_changed()

        doc = pn.state.curdoc
        if doc is not None:
            doc.add_next_tick_callback(_apply)
        else:
            _apply()

    def _on_goto(self, _event) -> None:
        sel = list(self._table.selection or [])
        if not sel:
            return
        bm = self._bookmarks[sel[0]]
        try:
            self.stage.x.set(bm.x)
            self.stage.y.set(bm.y)
            self.stage.z.set(bm.z)
        except Exception:
            pass

    def script_text(self) -> str:
        """Bookmark list-scan plan for the persistent script panel. Drops references."""
        return bookmark_list_scan_snippet(self._bookmarks, scripts_cfg=self.cfg.scripts)

    def total_points(self) -> int:
        # Bookmark scans aren't time-estimated (operator drives them interactively).
        return 0

    # ---- reference bookmarks --------------------------------------------------------

    def _on_cell_edit(self, event) -> None:
        """Handle Tabulator cell edits — the ``scan`` and ``ref`` tickCross columns."""
        column = getattr(event, "column", None)
        idx = int(getattr(event, "row", -1))
        if not (0 <= idx < len(self._bookmarks)):
            return
        new_val = bool(getattr(event, "value", False))
        bm = self._bookmarks[idx]

        if column == "scan":
            # References are never scan targets; ignore (tick_table will reset the box).
            if bm.is_reference:
                if new_val:
                    self.tick_table()
                return
            if bm.in_scan == new_val:
                return  # no-op (table re-render)
            bm.in_scan = new_val
            # Scan target set changed — modes redraw overlays + scripts regenerate.
            self._notify_bookmarks_changed()
            return

        if column == "ref":
            if bm.is_reference == new_val:
                return  # no-op (could happen with table re-rendering)
            bm.is_reference = new_val
            self._persist_references()
            # Refresh — script set changes, marker layer membership changes.
            self.tick()
            self.tick_table()  # the row's "scan" box visibility depends on ref state
            self._notify_bookmarks_changed()
            return

    def _persist_references(self) -> None:
        """Write the current set of reference bookmarks back to the config file."""
        self.cfg.references = [
            ReferenceBookmark(name=b.name, x=b.x, y=b.y, z=b.z)
            for b in self._bookmarks
            if b.is_reference
        ]
        try:
            self.cfg.save()
        except Exception:
            # Saving might fail (read-only filesystem etc.) — keep the in-memory state.
            pass
