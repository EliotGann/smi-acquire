"""Calibrate mode: automated pixel↔motor affine fit.

Routine (5 frames total):

1. Capture a reference frame at the current motor position.
2. For each Δm in ``[(+step, 0), (-step, 0), (0, +step), (0, -step)]``:
   - move the stage to ``ref + Δm``
   - wait for the move to complete + a settle pad for the camera to deliver a fresh frame
   - capture the target frame
   - phase-cross-correlate against the reference → pixel shift ``Δp``
3. Return motors to the reference position.
4. Least-squares solve ``Δp = A · Δm`` for the 2×2 affine ``A``.
5. Show the proposed matrix and RMS residual; Accept writes it to the shared
   ``CalibrationModel`` (so Interactive, Area, Knife-edge all see the new value immediately)
   and persists to ``config/default.yaml``.

Notes
-----
``skimage.registration.phase_cross_correlation`` returns shift in (row, col) = (Δy, Δx)
order — we swap to our (Δx, Δy) convention before fitting.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import panel as pn
from skimage.registration import phase_cross_correlation


def _hann_window(img: np.ndarray) -> np.ndarray:
    """Apply a separable Hann window to suppress edge / periodic artifacts in the FFT."""
    h, w = img.shape
    win = np.hanning(h)[:, None] * np.hanning(w)[None, :]
    return img * win

from ..calibration import CalibrationModel
from ..camera_stream import CameraStream
from ..config import AppConfig
from ..devices import SampleStage
from ..overlays import BeamOverlay

log = logging.getLogger(__name__)


def _doc_safe(fn) -> None:
    """Schedule ``fn()`` on the doc thread (lock held). See modes/focus.py for details."""
    doc = pn.state.curdoc
    if doc is not None:
        doc.add_next_tick_callback(fn)
    else:
        fn()


async def _wait_for_status(status, timeout: float = 15.0) -> bool:
    """Await an ophyd Status object via an asyncio future. Returns True if completed."""
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    def _done(*args, **kwargs) -> None:
        if not future.done():
            loop.call_soon_threadsafe(future.set_result, True)

    try:
        if getattr(status, "done", False):
            return True
        status.add_callback(_done)
    except Exception:
        return False
    try:
        await asyncio.wait_for(future, timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


class CalibrateMode:
    name = "Calibrate"

    def __init__(
        self,
        fig,
        stage: SampleStage,
        beam_overlay: BeamOverlay,
        calibration: CalibrationModel,
        cfg: AppConfig,
        image_size_hint_provider,
        camera_stream: CameraStream,
        huber_calibration: "CalibrationModel | None" = None,
    ) -> None:
        self.fig = fig
        self.stage = stage
        self.beam = beam_overlay
        self.calibration = calibration              # piezo affine
        self.huber_calibration = huber_calibration  # Huber affine (optional)
        self.cfg = cfg
        self._dims = image_size_hint_provider
        self._stream = camera_stream
        self._active = False
        self._proposed: np.ndarray | None = None
        self._running = False
        # Which stack this calibration fits: "piezo" (default) or "huber".
        self._cal_stack = "piezo"

        units = cfg.ui.motor_units
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        # Sensible default calibration step: ~0.2 mm = 200 µm — large enough for phase
        # correlation to pick out a clear shift but small enough to stay in view.
        default_cal_step = 200.0 if is_um else 0.2
        step_increment = 10.0 if is_um else 0.05
        step_end = 5000.0 if is_um else 5.0
        self._step_mm = pn.widgets.FloatInput(
            name=f"step ({units})", value=default_cal_step,
            step=step_increment, start=step_increment / 10, end=step_end, width=130,
        )
        self._settle_s = pn.widgets.FloatInput(
            name="settle (s)", value=0.3, step=0.05, start=0.0, end=5.0, width=120,
        )
        self._status = pn.widgets.StaticText(
            value="Place a feature-rich target (text on paper, grid, etc.) in view, then click Start.",
            width=400,
        )
        self._progress = pn.indicators.Progress(
            name="progress", value=0, max=4, width=300, bar_color="info",
        )
        self._start_btn = pn.widgets.Button(name="Start calibration", button_type="primary", width=180)
        self._start_btn.on_click(self._on_start)
        self._accept_btn = pn.widgets.Button(name="Accept", button_type="success", width=100, disabled=True)
        self._accept_btn.on_click(self._on_accept)
        self._reject_btn = pn.widgets.Button(name="Reject", button_type="danger", width=100, disabled=True)
        self._reject_btn.on_click(self._on_reject)

        self._current_matrix_view = pn.pane.Markdown(self._fmt_matrix("current", self.calibration.matrix))
        self._proposed_matrix_view = pn.pane.Markdown("**proposed:** _run a calibration to populate_")
        self._residual_view = pn.pane.Markdown("")

        # Which stack to calibrate (piezo default; Huber only if the stage exposes it).
        self._stack_select = pn.widgets.RadioButtonGroup(
            name="Calibrate stack", options=["piezo", "Huber"], value="piezo",
            button_type="default", width=180)
        self._stack_select.param.watch(self._on_cal_stack_change, "value")
        _has_huber = (getattr(self.stage, "huber", None) is not None
                      and self.huber_calibration is not None)
        self._stack_row = pn.Row(pn.pane.Markdown("**fit:**", width=44), self._stack_select,
                                 visible=_has_huber)

        self.panel = pn.Column(
            pn.pane.Markdown("### Calibrate"),
            pn.pane.Markdown(
                "Automated pixel ↔ motor calibration. The routine captures a reference "
                "image, then moves the selected stack ±step in x and ±step in y, and uses "
                "image registration to fit the affine matrix."
            ),
            pn.pane.Markdown(
                "<span style='color:#888;font-size:12px'>Fit the <b>piezo</b> (µm) and the "
                "<b>Huber</b> (mm) stacks separately. Calibrate each at <b>χ = 0</b> — "
                "click-to-move then rotation-corrects for the live χ (the piezo rides on the "
                "Huber, so its x/y rotate with χ).</span>"
            ),
            self._stack_row,
            pn.Row(self._step_mm, self._settle_s),
            self._status,
            self._progress,
            pn.Row(self._start_btn, self._accept_btn, self._reject_btn),
            pn.layout.Divider(),
            self._current_matrix_view,
            self._proposed_matrix_view,
            self._residual_view,
        )

    # ---- Mode protocol -------------------------------------------------------------

    def activate(self) -> None:
        self._active = True
        self._current_matrix_view.object = self._fmt_matrix("current", self.calibration.matrix)

    def deactivate(self) -> None:
        self._active = False

    def on_tap(self, x: float, y: float) -> None:
        return

    def tick(self) -> None:
        return

    def tick_table(self) -> None:
        return

    # ---- internals -----------------------------------------------------------------

    @staticmethod
    def _fmt_matrix(label: str, A) -> str:
        A = np.asarray(A)
        return (
            f"**{label}:**  \n"
            f"```\n[[{A[0,0]:+8.3f}, {A[0,1]:+8.3f}],\n"
            f" [{A[1,0]:+8.3f}, {A[1,1]:+8.3f}]]\n```"
        )

    def _on_cal_stack_change(self, event) -> None:
        self._cal_stack = "huber" if str(event.new).lower() == "huber" else "piezo"
        # Reflect the selected stack's current matrix + drop any stale proposal.
        self._proposed = None
        self._proposed_matrix_view.object = "**proposed:** _run a calibration to populate_"
        self._residual_view.object = ""
        self._accept_btn.disabled = True
        self._reject_btn.disabled = True
        self._current_matrix_view.object = self._fmt_matrix(
            "current", self._active_calibration().matrix)
        self._status.value = "calibrating the {} stack (fit at χ = 0).".format(self._cal_stack)

    def _active_motors(self):
        """The (x, y) motors of the stack being calibrated (piezo primary, or Huber)."""
        if self._cal_stack == "huber":
            huber = getattr(self.stage, "huber", None)
            if huber is not None and hasattr(huber, "x") and hasattr(huber, "y"):
                return huber.x, huber.y
        return self.stage.x, self.stage.y

    def _active_calibration(self) -> CalibrationModel:
        if self._cal_stack == "huber" and self.huber_calibration is not None:
            return self.huber_calibration
        return self.calibration

    def _on_start(self, _event) -> None:
        # Panel async-button: schedule the coroutine on the doc's event loop.
        if self._running:
            return
        self._running = True
        self._accept_btn.disabled = True
        self._reject_btn.disabled = True
        self._start_btn.disabled = True
        doc = pn.state.curdoc
        if doc is not None:
            asyncio.ensure_future(self._run())
        else:
            # Synchronous fallback for tests.
            asyncio.run(self._run())

    async def _run(self) -> None:
        try:
            await self._do_calibration()
        except Exception as exc:  # noqa: BLE001
            log.exception("calibration failed")
            msg = f"calibration failed: {exc}"
            _doc_safe(lambda: setattr(self._status, "value", msg))
        finally:
            self._running = False
            _doc_safe(lambda: setattr(self._start_btn, "disabled", False))

    async def _do_calibration(self) -> None:
        step = float(self._step_mm.value or 0.0)
        units = self.cfg.ui.motor_units
        if step <= 0:
            _doc_safe(lambda: setattr(self._status, "value", f"step must be > 0 ({units})"))
            return
        settle = float(self._settle_s.value or 0.0)

        def _init():
            self._progress.value = 0
            self._status.value = "capturing reference frame…"
        _doc_safe(_init)
        await asyncio.sleep(max(0.1, settle))
        ref = self._stream.grab_gray()
        if ref is None:
            _doc_safe(lambda: setattr(self._status, "value", "no image available — is the camera connected?"))
            return

        cal_x, cal_y = self._active_motors()
        try:
            m_ref = (float(cal_x.position), float(cal_y.position))
        except Exception:
            _doc_safe(lambda: setattr(self._status, "value", "could not read motor positions"))
            return

        moves: list[tuple[float, float]] = [(step, 0.0), (-step, 0.0), (0.0, step), (0.0, -step)]
        dm_list: list[tuple[float, float]] = []
        dp_list: list[tuple[float, float]] = []

        for i, (dx, dy) in enumerate(moves):
            tx = m_ref[0] + dx
            ty = m_ref[1] + dy
            status_msg = f"step {i + 1}/4: moving to (Δx={dx:+.3f}, Δy={dy:+.3f}) {units}…"
            _doc_safe(lambda v=status_msg: setattr(self._status, "value", v))
            ok_x = await _wait_for_status(cal_x.set(tx))
            ok_y = await _wait_for_status(cal_y.set(ty))
            if not (ok_x and ok_y):
                fail_msg = f"step {i + 1}/4: motor move timed out, skipping"
                idx = i + 1
                _doc_safe(lambda v=fail_msg, n=idx: (setattr(self._status, "value", v),
                                                     setattr(self._progress, "value", n)))
                continue
            await asyncio.sleep(settle)

            target = self._stream.grab_gray()
            if target is None:
                fail_msg = f"step {i + 1}/4: failed to grab frame, skipping"
                idx = i + 1
                _doc_safe(lambda v=fail_msg, n=idx: (setattr(self._status, "value", v),
                                                     setattr(self._progress, "value", n)))
                continue

            shift, _, _ = phase_cross_correlation(
                _hann_window(ref), _hann_window(target),
                upsample_factor=10, disambiguate=True,
            )
            dp = (-float(shift[1]), -float(shift[0]))
            dm_list.append((dx, dy))
            dp_list.append(dp)
            done_idx = i + 1
            _doc_safe(lambda n=done_idx: setattr(self._progress, "value", n))

        _doc_safe(lambda: setattr(self._status, "value", "returning to reference position…"))
        await _wait_for_status(cal_x.set(m_ref[0]))
        await _wait_for_status(cal_y.set(m_ref[1]))

        if len(dm_list) < 2:
            _doc_safe(lambda: setattr(self._status, "value", "not enough successful steps to fit an affine"))
            return

        dms = np.asarray(dm_list, dtype=float)
        dps = np.asarray(dp_list, dtype=float)
        A_T, _, _, _ = np.linalg.lstsq(dms, dps, rcond=None)
        A_fit = A_T.T

        predicted = dms @ A_fit.T
        residuals = predicted - dps
        rms = float(np.sqrt(np.mean(residuals ** 2)))

        self._proposed = A_fit
        proposed_md = self._fmt_matrix("proposed", A_fit)
        residual_md = (
            f"**residual RMS:** {rms:.3f} px  \n"
            f"**N samples:** {len(dm_list)}"
        )

        def _publish():
            self._proposed_matrix_view.object = proposed_md
            self._residual_view.object = residual_md
            self._status.value = "review the proposed matrix, then Accept or Reject."
            self._accept_btn.disabled = False
            self._reject_btn.disabled = False
        _doc_safe(_publish)

    def _on_accept(self, _event) -> None:
        if self._proposed is None:
            return
        # Apply + persist to the matrix of the stack that was calibrated.
        self._active_calibration().update_matrix(self._proposed)
        if self._cal_stack == "huber":
            self.cfg.calibration.huber_matrix = self._proposed.tolist()
        else:
            self.cfg.calibration.matrix = self._proposed.tolist()
        try:
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"applied in-memory but save failed: {exc}"
        else:
            self._status.value = f"accepted and saved ({self._cal_stack})."
        self._current_matrix_view.object = self._fmt_matrix(
            "current", self._active_calibration().matrix)
        self._accept_btn.disabled = True
        self._reject_btn.disabled = True
        self._proposed = None

    def _on_reject(self, _event) -> None:
        self._proposed = None
        self._proposed_matrix_view.object = "**proposed:** _discarded — run again or keep the current matrix_"
        self._residual_view.object = ""
        self._status.value = "rejected. Current matrix is unchanged."
        self._accept_btn.disabled = True
        self._reject_btn.disabled = True
