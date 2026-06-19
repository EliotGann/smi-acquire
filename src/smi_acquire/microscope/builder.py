"""
smi_acquire.microscope.builder
==============================

Assemble the vendored on-axis microscope as an **embeddable component** (rather than a
top-level servable app). The guided-interview app embeds the returned ``layout`` as its
"Samples" tab and reads ``interactive`` (the bookmark store) to harvest sample positions into
the :class:`~smi_acquire.spec.ExperimentSpec`.

This is the swaxs-beam-image ``app.build_app`` logic, lightly adapted to:
  * return the live objects (layout, the InteractiveMode bookmark store, stage, camera, modes)
    instead of calling ``.servable()``, and
  * register the per-session periodic callbacks via :func:`attach_periodic_callbacks`, which the
    host app calls inside its own ``pn.state`` context.

No real hardware: it talks to whatever ``config/microscope.yaml`` (``$BEAM_IMAGE_CONFIG``)
points at — by default the bundled fake caproto IOC (``smi_acquire.sim.fake_ioc``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import panel as pn
from bokeh.events import Tap
from bokeh.models import ColumnDataSource, CustomJS, Div
from bokeh.plotting import figure

from .calibration import CalibrationModel
from .camera_stream import CameraStream, placeholder_image
from .config import AppConfig, load_config
from .devices import Camera, SampleStage
from .modes.area import AreaMode
from .modes.calibrate import CalibrateMode
from .modes.focus import FocusMode
from .modes.interactive import InteractiveMode
from .modes.linear import LinearScanMode
from .modes.square import SquareScanMode
from .overlays import BeamOverlay
from .widgets.beam_panel import BeamPanel
from .widgets.exposure import ExposureControl
from .widgets.motor_panel import MotorPanel
from .widgets.script_panel import ScriptPanel
from .widgets.status_bar import StatusBar
log = logging.getLogger(__name__)


_KEYHOOK_AND_FOCUS_JS = """
function focusBookmark() {
  var inputs = document.querySelectorAll('.bookmark-name input');
  if (!inputs.length) {
    inputs = document.querySelectorAll('input[placeholder*="bookmark"], input[placeholder*="Enter"]');
  }
  if (inputs.length) {
    var el = inputs[inputs.length - 1];
    el.focus();
    try { el.select(); } catch (e) {}
  }
}
if (!window.__swaxsKeyHookInstalled) {
  window.__swaxsKeyHookInstalled = true;
  window.addEventListener('keydown', function (e) {
    var ae = document.activeElement || {};
    var tag = ae.tagName || '';
    var isTyping = (tag === 'INPUT' || tag === 'TEXTAREA');
    if (e.key === 'b' && !isTyping && !e.ctrlKey && !e.metaKey && !e.altKey) {
      focusBookmark(); e.preventDefault();
    } else if (e.key === 'Escape' && isTyping) {
      try { ae.blur(); } catch (err) {}
    }
  }, true);
  window.__swaxsFocusBookmark = focusBookmark;
}
if (window.__swaxsFocusBookmark) { window.__swaxsFocusBookmark(); }
"""

_POLY_TOOL_JS = """
if (typeof fig === 'undefined' || fig.toolbar == null) { }
else {
  var want = (cb_obj.text || '').endsWith(':on');
  var found = null;
  var tools = fig.toolbar.tools || [];
  for (var i = 0; i < tools.length; i++) {
    var t = tools[i];
    if (t && t.name === 'swaxs_poly_draw') { found = t; break; }
    if (t && t.tools) {
      for (var j = 0; j < t.tools.length; j++) {
        if (t.tools[j] && t.tools[j].name === 'swaxs_poly_draw') { found = t.tools[j]; break; }
      }
      if (found) break;
    }
  }
  if (found) { found.active = want; }
}
"""


@dataclass
class MicroscopeUI:
    """The live microscope pieces the host app needs."""
    layout: pn.viewable.Viewable
    interactive: InteractiveMode          # the bookmark store (sample positions live here)
    stage: SampleStage
    camera: Camera
    cfg: AppConfig
    capture_slot: pn.Column               # host fills this with capture-position controls
    _periodic: List[tuple]                # (callback, period_ms)

    def attach_periodic_callbacks(self) -> None:
        """Register this microscope's periodic callbacks on the current Panel session."""
        for cb, period in self._periodic:
            pn.state.add_periodic_callback(cb, period=period)

    def bookmark_rows(self, *, motor_object: str = "piezo",
                      include_references: bool = False) -> List[dict]:
        """Harvest the current bookmarks as ExperimentSpec sample rows.

        Maps each bookmark's (x, y, z) motor position onto ``{motor_object}_x/y/z`` columns so
        ``SampleList.from_columns`` / the code generator can consume them directly. References
        (fixed landmarks) are excluded unless requested.
        """
        rows = []
        for bm in self.interactive._bookmarks:  # noqa: SLF001 (intentional vendored access)
            if bm.is_reference and not include_references:
                continue
            rows.append({
                "name": bm.name,
                "{}_x".format(motor_object): round(float(bm.x), 4),
                "{}_y".format(motor_object): round(float(bm.y), 4),
                "{}_z".format(motor_object): round(float(bm.z), 4),
            })
        return rows


def _build_figure(cfg: AppConfig):
    w, h = cfg.ui.image_size_hint
    fig = figure(
        title="On-axis microscope",
        x_range=(0, w), y_range=(h, 0),
        width=760, height=int(760 * h / w),
        tools="pan,wheel_zoom,reset,tap", active_scroll="wheel_zoom",
    )
    fig.grid.visible = False
    fig.axis.visible = False
    fig.toolbar.logo = None
    img_cds = ColumnDataSource(
        data={"image": [placeholder_image(w, h)], "x": [0], "y": [0], "dw": [w], "dh": [h]})
    fig.image_rgba(image="image", x="x", y="y", dw="dw", dh="dh", source=img_cds)
    return fig, img_cds


class _NullMode:
    name = "—"
    def activate(self): ...
    def deactivate(self): ...
    def on_tap(self, x, y): ...
    def tick(self): ...
    def tick_table(self): ...


def build_microscope(cfg: AppConfig | None = None, *, executor=None, interlock=None) -> MicroscopeUI:
    """Construct the interactive microscope and return its embeddable pieces.

    ``executor`` is an optional :class:`smi_acquire.execute.Executor`; when given, the motor jog
    panel + click-to-move route moves through it (so they are interlock-gated against an external
    RunEngine).  ``interlock`` (an :class:`smi_acquire.interlock.Interlock`) additionally gates the
    camera: focus/autofocus + exposure writes are locked out while a scan is running (the camera
    stays passive: display only).  Without them the microscope falls back to direct ophyd moves
    and unguarded camera writes (standalone use).
    """
    cfg = cfg or load_config()
    camera = Camera.from_config(cfg.epics, name="microscope")
    stage = SampleStage.from_config(cfg.epics, name="stage")
    for dev, label in ((camera, "camera"), (stage, "stage")):
        try:
            dev.wait_for_connection(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s did not connect within 2s: %s", label, exc)

    fig, img_cds = _build_figure(cfg)
    stream = CameraStream(camera, img_cds, fallback_size=cfg.ui.image_size_hint,
                          epics_cfg=cfg.epics, display_max_dim=cfg.ui.display_max_dim)

    beam_overlay = BeamOverlay(center_px=cfg.beam.center_px, width_px=cfg.beam.width_px,
                               height_px=cfg.beam.height_px)
    beam_overlay.add_to(fig)
    calibration = CalibrationModel.from_config(cfg.calibration)
    # Separate Huber affine for the coarse-stage click-to-move toggle (µm piezo vs mm Huber).
    huber_calibration = CalibrationModel(cfg.calibration.huber_matrix)

    motor_panel = MotorPanel(stage, cfg, executor=executor)
    beam_panel = BeamPanel(cfg, beam_overlay)
    status_bar = StatusBar(camera, stage)
    exposure = ExposureControl(camera, interlock=interlock)

    def dims():
        return (stream.current_dims().width, stream.current_dims().height)
    interactive = InteractiveMode(fig, stage, beam_overlay, calibration, cfg, dims,
                                  executor=executor, huber_calibration=huber_calibration)
    polygon = AreaMode(fig, stage, beam_overlay, calibration, cfg, dims, bookmark_store=interactive)
    square = SquareScanMode(fig, stage, beam_overlay, calibration, cfg, dims, bookmark_store=interactive)
    linear = LinearScanMode(fig, stage, beam_overlay, calibration, cfg, dims, bookmark_store=interactive)
    focus = FocusMode(fig, stage, beam_overlay, calibration, cfg, dims, camera_stream=stream,
                      interlock=interlock)
    calibrate = CalibrateMode(fig, stage, beam_overlay, calibration, cfg, dims,
                              camera_stream=stream, huber_calibration=huber_calibration)
    modes = (interactive, polygon, square, linear, focus, calibrate)

    script_panel = ScriptPanel([interactive, square, polygon, linear])
    for scan_mode in (polygon, square, linear):
        scan_mode.set_on_change(script_panel.refresh)
    interactive.add_bookmark_listener(script_panel.refresh)

    _poly_tool_trigger = Div(text="0:off", visible=False)
    _poly_tool_trigger.js_on_change("text", CustomJS(args={"fig": fig}, code=_POLY_TOOL_JS))
    _poly_seq = {"n": 0}

    def _sync_poly_tool(active: bool) -> None:
        _poly_seq["n"] += 1
        _poly_tool_trigger.text = "{}:{}".format(_poly_seq["n"], "on" if active else "off")

    polygon.set_draw_tool_sync(_sync_poly_tool)

    for mode in modes:
        tools = getattr(mode, "tools", ())
        if tools:
            fig.add_tools(*tools)

    state = {"active": _NullMode()}

    def _activate(new_mode) -> None:
        if new_mode is state["active"]:
            return
        try:
            state["active"].deactivate()
        except Exception:
            pass
        state["active"] = new_mode
        try:
            new_mode.activate()
        except Exception:
            pass

    setup_tabs = pn.Tabs(("Calibrate", calibrate.panel), ("Motors", motor_panel.view),
                         ("Beam", beam_panel.view), dynamic=False)
    scan_tabs = pn.Tabs((square.name, square.panel), (polygon.name, polygon.panel),
                        (linear.name, linear.panel), dynamic=False)

    # The **Move** tab folds three things that were redundant/scattered into one place:
    #   1. ``capture_slot`` — filled by the host app with its capture-position controls
    #      (live position, name field, "new sample here" / "assign" / "reference").
    #   2. the single shared bookmark list (the only bookmark display).
    #   3. the click-to-move preview controls.
    capture_slot = pn.Column(sizing_mode="stretch_width")
    move_tab = pn.Column(
        capture_slot,
        pn.layout.Divider(),
        interactive.bookmark_panel,
        pn.layout.Divider(),
        interactive.move_panel,
        sizing_mode="stretch_width",
    )
    main_tabs = pn.Tabs((interactive.name, move_tab), ("Scan", scan_tabs),
                        (focus.name, focus.panel), ("Setup", setup_tabs), dynamic=False)

    def _resolve():
        top = main_tabs.active
        if top == 0:
            return interactive
        if top == 1:
            return (square, polygon, linear)[scan_tabs.active]
        if top == 2:
            return focus
        return calibrate if setup_tabs.active == 0 else state["active"]

    def _on_tab(_e):
        _activate(_resolve())

    main_tabs.param.watch(_on_tab, "active")
    setup_tabs.param.watch(_on_tab, "active")
    scan_tabs.param.watch(_on_tab, "active")
    _activate(_resolve())

    fig.on_event(Tap, lambda event: state["active"].on_tap(float(event.x), float(event.y)))
    fig.js_on_event(Tap, CustomJS(code=_KEYHOOK_AND_FOCUS_JS))

    _js_trigger = Div(text="0", visible=False)
    _js_trigger.js_on_change("text", CustomJS(code=_KEYHOOK_AND_FOCUS_JS))

    def _tick_all() -> None:
        for m in modes:
            try:
                m.tick()
            except Exception:
                pass

    interval_ms = max(1, int(1000.0 / cfg.ui.poll_hz))
    periodic = [
        (stream.tick, interval_ms),
        (motor_panel.refresh_readbacks, 500),
        (status_bar.refresh, 1000),
        (exposure.refresh, 1000),
        (_tick_all, interval_ms),
        (lambda: state["active"].tick_table(), 1000),
        (script_panel.refresh, 1500),
        (lambda: setattr(_js_trigger, "text", "1"), 1500),
    ]

    side = pn.Column(main_tabs, width=440, height=fig.height, scroll=True)
    layout = pn.Column(
        pn.pane.Bokeh(_js_trigger, height=0, width=0, margin=0),
        pn.pane.Bokeh(_poly_tool_trigger, height=0, width=0, margin=0),
        exposure.view,
        pn.Row(pn.pane.Bokeh(fig, sizing_mode="stretch_width"), side),
        pn.layout.Divider(),
        script_panel.view,
        pn.layout.Divider(),
        status_bar.view,
    )
    return MicroscopeUI(layout=layout, interactive=interactive, stage=stage,
                        camera=camera, cfg=cfg, capture_slot=capture_slot, _periodic=periodic)


__all__ = ["MicroscopeUI", "build_microscope"]
