"""Friendly exposure-time control: a log-spaced dial + an exact input box.

Sits at the top of the on-axis microscope view. The camera stays passive otherwise; this is the
one exposure write the alignment UI makes (set ``AcquireTime``), and it is gated by the
RunEngine-busy interlock -- when a scan is running on the beamline RE, the control is disabled
(you're "logged out") so it cannot fight the worker.

The slider is **log-spaced** (0.0001 s .. 2 s) so a user can sweep across decades quickly; the
input box takes an exact value. Both write the camera setpoint and reflect the live readback.
"""

from __future__ import annotations

import math

import panel as pn

_MIN_S = 1e-4          # 0.0001 s
_MAX_S = 2.0           # 2 s
_LOG_MIN = math.log10(_MIN_S)
_LOG_MAX = math.log10(_MAX_S)


def _clamp(v: float) -> float:
    return max(_MIN_S, min(_MAX_S, float(v)))


class ExposureControl:
    """A log slider + exact input bound to the camera's ``AcquireTime`` (interlock-gated)."""

    def __init__(self, camera, interlock=None):
        self.camera = camera
        self.interlock = interlock
        self._suppress = False        # guard against feedback loops between slider/input

        # Log-spaced slider: the widget value is log10(seconds); labels show the real time.
        self._slider = pn.widgets.FloatSlider(
            name="", start=_LOG_MIN, end=_LOG_MAX, step=0.01, value=_LOG_MIN,
            show_value=False, width=300, format="0.0000",
        )
        self._input = pn.widgets.FloatInput(
            name="exposure (s)", value=_MIN_S, step=0.001, start=_MIN_S, end=_MAX_S, width=120,
        )
        self._readout = pn.pane.HTML("", width=210)
        self._slider.param.watch(self._on_slider, "value")
        self._input.param.watch(self._on_input, "value")

        self.view = pn.Row(
            pn.pane.HTML("<b>⏱ Exposure</b>", width=92),
            self._slider,
            self._input,
            self._readout,
            sizing_mode="stretch_width",
        )
        # Seed from the live camera readback.
        self.refresh(initial=True)

    # ------------------------------------------------------------------
    def _locked(self) -> bool:
        il = self.interlock
        return bool(il is not None and il.is_busy())

    def _set_exposure(self, seconds: float) -> None:
        """Write the camera setpoint (no-op + status when interlocked or no camera)."""
        seconds = _clamp(seconds)
        if self._locked():
            self._readout.object = ("<span style='color:#b26a00'>🔒 locked (RE busy)</span>")
            return
        cam = self.camera
        if cam is None or not hasattr(cam, "cam") or not hasattr(cam.cam, "acquire_time"):
            self._readout.object = "<span style='color:#999'>no camera</span>"
            return
        try:
            cam.cam.acquire_time.put(seconds)
            self._readout.object = "set → <b>{:g} s</b>".format(seconds)
        except Exception as exc:  # noqa: BLE001
            self._readout.object = "<span style='color:#c0392b'>err: {}</span>".format(exc)

    # -- widget event handlers (suppress cross-updates) ----------------
    def _on_slider(self, event) -> None:
        if self._suppress:
            return
        seconds = _clamp(10 ** float(event.new))
        self._suppress = True
        try:
            self._input.value = round(seconds, 6)
        finally:
            self._suppress = False
        self._set_exposure(seconds)

    def _on_input(self, event) -> None:
        if self._suppress:
            return
        seconds = _clamp(event.new)
        self._suppress = True
        try:
            self._slider.value = math.log10(seconds)
        finally:
            self._suppress = False
        self._set_exposure(seconds)

    # -- periodic / initial refresh from the live readback -------------
    def refresh(self, initial: bool = False) -> None:
        """Reflect the camera's live ``AcquireTime_RBV`` + show the lock state."""
        cam = self.camera
        rbv = None
        if cam is not None and hasattr(cam, "cam") and hasattr(cam.cam, "acquire_time_rbv"):
            try:
                rbv = float(cam.cam.acquire_time_rbv.get())
            except Exception:
                rbv = None
        locked = self._locked()
        # Disable the controls when a scan is running (you're "logged out").
        self._slider.disabled = locked
        self._input.disabled = locked
        if rbv is not None:
            if initial:
                # On first load, sync the widgets to the actual camera value.
                s = _clamp(rbv)
                self._suppress = True
                try:
                    self._input.value = round(s, 6)
                    self._slider.value = math.log10(s)
                finally:
                    self._suppress = False
            if locked:
                self._readout.object = ("RBV {:g} s &nbsp; "
                                        "<span style='color:#b26a00'>🔒 RE busy</span>".format(rbv))
            else:
                self._readout.object = "RBV <b>{:g} s</b>".format(rbv)
        elif locked:
            self._readout.object = "<span style='color:#b26a00'>🔒 RE busy</span>"


__all__ = ["ExposureControl"]
