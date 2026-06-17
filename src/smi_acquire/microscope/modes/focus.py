"""Focus mode: auto-focus a user-defined ROI by sweeping the z motor.

Workflow
--------
1. Use the **Box Edit** tool in the figure toolbar to draw a rectangle around the feature
   you want to bring into focus. (If you don't draw anything, the full frame is used.)
2. Set the z range (centred on the current z), the coarse step, the per-step settle time,
   and a minimum refinement step (defines the precision the search will converge to).
3. Press **Start**. Two phases run:
   - **Coarse**: even sampling over ``[z₀ − half, z₀ + half]`` at ``coarse_step``.
   - **Refine**: binary refinement around the coarse peak — step is halved each pass until
     it drops below ``min_step``.
   At every step a focus metric is computed on the ROI and plotted live.
4. The motor lands at the best-z. ``Move to current best`` re-issues the move at any time.

Focus metric
------------
Variance of the discrete Laplacian inside the ROI. This is the standard "is the image sharp"
measure: when the image is in focus, edges are crisp and the second derivative has high
variance; out of focus, edges blur and the variance collapses.
"""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np
import panel as pn
from bokeh.models import (
    BoxEditTool,
    ColumnDataSource,
)
from bokeh.plotting import figure as Figure  # noqa: N812 — type alias

from ..calibration import CalibrationModel
from ..camera_stream import CameraStream
from ..config import AppConfig
from ..devices import SampleStage
from ..overlays import BeamOverlay
from .calibrate import _wait_for_status

log = logging.getLogger(__name__)


def _doc_safe(fn) -> None:
    """Schedule ``fn()`` on the Bokeh document thread, where the doc lock is held.

    Mutating Bokeh server-side models (CDS data, widget value, progress) from inside an async
    coroutine throws ``RuntimeError: _pending_writes should be non-None …`` because the lock
    isn't held during awaited continuations. Marshaling via ``add_next_tick_callback`` puts
    the mutation on the doc-thread tick where the lock is reacquired.
    """
    doc = pn.state.curdoc
    if doc is not None:
        doc.add_next_tick_callback(fn)
    else:
        fn()  # headless / tests — no doc, so direct mutation is fine


def _laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the discrete 5-point Laplacian — the focus metric."""
    if gray.size < 9:
        return 0.0
    g = gray.astype(np.float32, copy=False)
    lap = (
        -4.0 * g
        + np.roll(g, 1, axis=0) + np.roll(g, -1, axis=0)
        + np.roll(g, 1, axis=1) + np.roll(g, -1, axis=1)
    )
    # Trim 1-pixel rim where np.roll wrap-around introduces artifacts.
    inner = lap[1:-1, 1:-1]
    return float(inner.var())


def _crop_roi(
    gray: np.ndarray,
    cx: float, cy: float, w: float, h: float,
) -> np.ndarray:
    H, W = gray.shape
    left = max(0, int(math.floor(cx - w / 2)))
    right = min(W, int(math.ceil(cx + w / 2)))
    top = max(0, int(math.floor(cy - h / 2)))
    bottom = min(H, int(math.ceil(cy + h / 2)))
    if right <= left or bottom <= top:
        return gray  # fall back to full frame
    return gray[top:bottom, left:right]


class FocusMode:
    name = "Focus"

    def __init__(
        self,
        fig: Figure,
        stage: SampleStage,
        beam_overlay: BeamOverlay,
        calibration: CalibrationModel,
        cfg: AppConfig,
        image_size_hint_provider,
        camera_stream: CameraStream,
        interlock=None,
    ) -> None:
        self.fig = fig
        self.stage = stage
        self.beam = beam_overlay
        self.calibration = calibration
        self.cfg = cfg
        self._dims = image_size_hint_provider
        self._stream = camera_stream
        self.interlock = interlock
        self._active = False
        self._running = False
        self._cancel_requested = False
        self._best_z: float | None = None

        # ROI rectangle. BoxEditTool needs a Rect glyph (center + width + height).
        self._roi_cds = ColumnDataSource(data={"x": [], "y": [], "width": [], "height": []})
        self._roi_renderer = fig.rect(
            x="x", y="y", width="width", height="height",
            source=self._roi_cds,
            fill_color="violet", fill_alpha=0.1,
            line_color="violet", line_width=2,
        )
        self._roi_renderer.visible = False
        self._box_edit_tool = BoxEditTool(renderers=[self._roi_renderer], num_objects=1)
        self.tools = (self._box_edit_tool,)

        # Live focus-vs-z plot.
        self._plot_cds = ColumnDataSource(data={"z": [], "metric": []})
        self._best_cds = ColumnDataSource(data={"z": [], "metric": []})
        units = cfg.ui.motor_units
        self._plot = Figure(
            title="focus metric vs z",
            width=440, height=240,
            x_axis_label=f"z ({units})", y_axis_label="variance(Laplacian)",
            tools="pan,wheel_zoom,reset",
        )
        self._plot.line("z", "metric", source=self._plot_cds, color="navy", line_width=1)
        self._plot.scatter("z", "metric", source=self._plot_cds, size=6, color="navy")
        self._plot.scatter("z", "metric", source=self._best_cds, size=14, color="red",
                           marker="star", line_color="black")

        # ---- widgets ----
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        default_half = 100.0 if is_um else 0.1
        default_coarse = 20.0 if is_um else 0.02
        default_min = 1.0 if is_um else 0.001
        step_inc = 5.0 if is_um else 0.005

        half_max = 5000.0 if is_um else 5.0     # µm: 5 mm-equivalent reach
        coarse_max = 1000.0 if is_um else 1.0   # µm: lets you do an initial wide sweep
        self._half_range = pn.widgets.FloatInput(
            name=f"± range around current z ({units})",
            value=default_half, step=step_inc, start=step_inc, end=half_max, width=180,
        )
        self._coarse_step = pn.widgets.FloatInput(
            name=f"coarse step ({units})",
            value=default_coarse, step=step_inc, start=step_inc / 5, end=coarse_max, width=160,
        )
        self._min_step = pn.widgets.FloatInput(
            name=f"refinement min step ({units})",
            value=default_min, step=step_inc / 10, start=step_inc / 100, end=default_coarse, width=200,
        )
        self._settle_s = pn.widgets.FloatInput(
            name="settle (s)", value=0.4, step=0.05, start=0.0, end=30.0, width=120,
        )
        self._exposure_btn = pn.widgets.Button(
            name="Optimize exposure", button_type="success", width=180,
        )
        self._exposure_btn.on_click(self._on_optimize_exposure)
        self._exposure_status = pn.widgets.StaticText(value="", width=440)
        self._status = pn.widgets.StaticText(
            value="draw an ROI with the Box Edit tool, then click Start.", width=440,
        )
        self._progress = pn.indicators.Progress(
            name="progress", value=0, max=100, width=300, bar_color="info",
        )
        self._start_btn = pn.widgets.Button(name="Start focus", button_type="primary", width=140)
        self._start_btn.on_click(self._on_start)
        self._cancel_btn = pn.widgets.Button(name="Cancel", button_type="danger", width=100, disabled=True)
        self._cancel_btn.on_click(self._on_cancel)
        self._goto_best_btn = pn.widgets.Button(
            name="Move to current best z", button_type="default", width=200, disabled=True,
        )
        self._goto_best_btn.on_click(self._on_goto_best)
        self._best_view = pn.pane.Markdown(f"**best z**: _none yet_")

        self.panel = pn.Column(
            pn.pane.Markdown("### Focus"),
            pn.pane.Markdown(
                "Use the **Box Edit** tool in the toolbar to outline a feature; the focus "
                "metric is computed inside that box. If no box is drawn, the full frame is "
                "used."
            ),
            pn.layout.Divider(),
            pn.pane.Markdown("**Exposure**"),
            pn.Row(self._exposure_btn, self._settle_s),
            self._exposure_status,
            pn.layout.Divider(),
            pn.pane.Markdown("**Search**"),
            self._half_range,
            pn.Row(self._coarse_step, self._min_step),
            pn.Row(self._start_btn, self._cancel_btn),
            self._status,
            self._progress,
            pn.layout.Divider(),
            self._best_view,
            self._goto_best_btn,
            pn.pane.Bokeh(self._plot),
        )

    # ---- Mode protocol -------------------------------------------------------------

    def activate(self) -> None:
        self._active = True
        self._roi_renderer.visible = True

    def deactivate(self) -> None:
        self._active = False
        self._roi_renderer.visible = False

    def on_tap(self, x: float, y: float) -> None:  # ROI is drawn via BoxEditTool
        return

    def tick(self) -> None:
        return

    def tick_table(self) -> None:
        return

    # ---- handlers ------------------------------------------------------------------

    def _locked(self) -> str:
        """Return the interlock banner if an external RunEngine is busy, else ''.

        Focus moves z AND writes the camera exposure, so it is locked out while the beamline
        RunEngine is running (camera stays passive: display only).
        """
        il = self.interlock
        if il is not None and il.is_busy():
            return il.banner()
        return ""

    def _on_start(self, _event) -> None:
        if self._running:
            return
        locked = self._locked()
        if locked:
            self._status.value = "🔒 " + locked
            return
        self._running = True
        self._cancel_requested = False
        # These three mutations are under the doc lock (we're inside the button click
        # callback), so direct assignment is fine here.
        self._start_btn.disabled = True
        self._cancel_btn.disabled = False
        self._goto_best_btn.disabled = True
        doc = pn.state.curdoc
        if doc is not None:
            asyncio.ensure_future(self._run())
        else:  # tests / headless
            asyncio.run(self._run())

    def _on_cancel(self, _event) -> None:
        self._cancel_requested = True
        # Click callback — doc lock is held, direct mutation is safe.
        self._status.value = "cancel requested — waiting for current step to finish…"

    def _on_goto_best(self, _event) -> None:
        if self._best_z is None:
            return
        locked = self._locked()
        if locked:
            self._status.value = "🔒 " + locked
            return
        try:
            self.stage.z.set(self._best_z)
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"move failed: {exc!r}"

    def _on_optimize_exposure(self, _event) -> None:
        """One-shot linear-scaling auto-expose against the current ROI.

        Target the 99th percentile of ROI luminance at ~220 (just below 8-bit clipping):
        if the camera response is linear (true in normal operating range for most CMOS),
        ``new_exp = cur_exp · (target / p99)`` puts the highlights right where we want them.
        Sets the focus-search settle time to 1.5× the new exposure so we always wait for a
        fresh frame after each motor move.
        """
        locked = self._locked()
        if locked:
            self._exposure_status.value = "🔒 " + locked
            return
        gray = self._stream.grab_gray()
        if gray is None:
            self._exposure_status.value = "no image — can't optimize exposure"
            return
        cx, cy, w, h = self._roi_or_full(gray.shape)
        roi = _crop_roi(gray, cx, cy, w, h)
        if roi.size < 9:
            self._exposure_status.value = "ROI too small"
            return

        cam = self._stream.camera
        if cam is None or not hasattr(cam, "cam") or not hasattr(cam.cam, "acquire_time"):
            self._exposure_status.value = "camera doesn't expose AcquireTime — check PV config"
            return

        try:
            cur_exp = float(cam.cam.acquire_time_rbv.get(use_monitor=False))
        except Exception as exc:  # noqa: BLE001
            self._exposure_status.value = f"could not read AcquireTime_RBV: {exc!r}"
            return

        p99 = float(np.percentile(roi, 99))
        target = 220.0
        if p99 < 1.0:
            new_exp = cur_exp * 5.0  # nearly black — boost aggressively
        else:
            new_exp = cur_exp * (target / p99)
        # Clamp to a sane range; bumping the top end avoids cutting off slow integration cams.
        new_exp = float(max(1e-4, min(30.0, new_exp)))

        try:
            cam.cam.acquire_time.put(new_exp)
        except Exception as exc:  # noqa: BLE001
            self._exposure_status.value = f"could not write AcquireTime: {exc!r}"
            return

        new_settle = 1.5 * new_exp
        self._settle_s.value = float(new_settle)
        self._exposure_status.value = (
            f"AcquireTime: {cur_exp:.4g}s → {new_exp:.4g}s  "
            f"(ROI p99={p99:.0f} → target {int(target)}); "
            f"settle time set to {new_settle:.3g}s"
        )

    async def _run(self) -> None:
        try:
            await self._do_focus()
        except Exception as exc:  # noqa: BLE001
            log.exception("focus failed")
            msg = f"focus failed: {exc!r}"
            _doc_safe(lambda: setattr(self._status, "value", msg))
        finally:
            self._running = False
            best_z_local = self._best_z

            def _finish():
                self._start_btn.disabled = False
                self._cancel_btn.disabled = True
                if best_z_local is not None:
                    self._goto_best_btn.disabled = False
            _doc_safe(_finish)

    async def _do_focus(self) -> None:
        units = self.cfg.ui.motor_units
        half = float(self._half_range.value or 0.0)
        coarse = float(self._coarse_step.value or 0.0)
        min_step = float(self._min_step.value or 0.0)
        settle = float(self._settle_s.value or 0.0)
        if half <= 0 or coarse <= 0 or min_step <= 0:
            self._status.value = "range / steps must all be > 0"
            return
        if min_step > coarse:
            self._status.value = "refinement min step must be ≤ coarse step"
            return

        try:
            z0 = float(self.stage.z.position)
        except Exception:
            _doc_safe(lambda: setattr(self._status, "value", "could not read z position"))
            return

        # Reset the live plot + best display.
        def _reset_plot():
            self._plot_cds.data = {"z": [], "metric": []}
            self._best_cds.data = {"z": [], "metric": []}
            self._best_view.object = "**best z**: _searching…_"
        _doc_safe(_reset_plot)
        self._best_z = None

        # Build the coarse grid.
        n_coarse = max(3, int(math.ceil(2 * half / coarse)) + 1)
        zs = np.linspace(z0 - half, z0 + half, n_coarse)
        refine_passes = max(0, int(math.ceil(math.log2(max(coarse / min_step, 1.0)))))
        prog_max = len(zs) + refine_passes * 3

        def _init_progress():
            self._progress.max = prog_max
            self._progress.value = 0
            self._status.value = f"coarse pass: {n_coarse} points over ±{half:g} {units}…"
        _doc_safe(_init_progress)

        results: list[tuple[float, float]] = []
        for i, z in enumerate(zs):
            if self._cancel_requested:
                _doc_safe(lambda: setattr(self._status, "value", "cancelled."))
                await _wait_for_status(self.stage.z.set(z0))
                return
            await self._move_and_score(float(z), settle, results)
            progress_now = i + 1
            _doc_safe(lambda v=progress_now: setattr(self._progress, "value", v))

        # Coarse peak.
        zs_arr = np.array([r[0] for r in results])
        ms_arr = np.array([r[1] for r in results])
        peak_idx = int(np.argmax(ms_arr))
        best_z = float(zs_arr[peak_idx])
        best_m = float(ms_arr[peak_idx])
        self._publish_best(best_z, best_m)

        # Refinement.
        step = coarse / 2.0
        progress_done = len(zs)
        while step >= min_step and not self._cancel_requested:
            for sgn in (-1.0, +1.0):
                if self._cancel_requested:
                    break
                z = float(best_z + sgn * step)
                if any(abs(z - r[0]) < 1e-9 for r in results):
                    continue
                m = await self._move_and_score(z, settle, results)
                if m > best_m:
                    best_m = m
                    best_z = z
                    self._publish_best(best_z, best_m)
                progress_done += 1
                _doc_safe(lambda v=progress_done: setattr(self._progress, "value", v))
            step /= 2.0

        # Final move to best z.
        msg_move = f"moving to best z = {best_z:.4g} {units}…"
        _doc_safe(lambda: setattr(self._status, "value", msg_move))
        await _wait_for_status(self.stage.z.set(best_z))
        cancelled = self._cancel_requested
        msg_final = (
            f"cancelled — at best so far z = {best_z:.4g} {units}"
            if cancelled
            else f"done — landed at z = {best_z:.4g} {units}"
        )

        def _finalise():
            self._progress.value = prog_max
            self._status.value = msg_final
        _doc_safe(_finalise)

    async def _move_and_score(
        self,
        z: float,
        settle: float,
        results: list[tuple[float, float]],
    ) -> float:
        await _wait_for_status(self.stage.z.set(z))
        await asyncio.sleep(settle)
        gray = self._stream.grab_gray()
        if gray is None:
            return 0.0
        cx, cy, w, h = self._roi_or_full(gray.shape)
        roi = _crop_roi(gray, cx, cy, w, h)
        m = _laplacian_variance(roi)
        results.append((z, m))
        # Keep the plot CDS sorted by z so the connecting line doesn't zig-zag.
        results.sort(key=lambda r: r[0])
        zs_snapshot = [r[0] for r in results]
        ms_snapshot = [r[1] for r in results]
        _doc_safe(lambda: setattr(self._plot_cds, "data", {"z": zs_snapshot, "metric": ms_snapshot}))
        return m

    def _roi_or_full(self, gray_shape) -> tuple[float, float, float, float]:
        H, W = gray_shape
        d = self._roi_cds.data
        if d["x"] and d["width"] and d["height"]:
            return float(d["x"][0]), float(d["y"][0]), float(d["width"][0]), float(d["height"][0])
        return W / 2, H / 2, float(W), float(H)

    def _publish_best(self, z: float, m: float) -> None:
        self._best_z = z
        units = self.cfg.ui.motor_units

        def _apply():
            self._best_cds.data = {"z": [z], "metric": [m]}
            self._best_view.object = f"**best z**: `{z:.4g} {units}`  (metric = {m:.3g})"
            self._goto_best_btn.disabled = False
        _doc_safe(_apply)
