"""Motor jog and absolute-move panel.

Three rows of controls (X, Y, Z) with:
- jog ± buttons (using a per-axis step input)
- live readback display
- absolute-move input
- a "moving" indicator that lights while a motion is in flight
"""

from __future__ import annotations

import panel as pn
from ophyd import EpicsMotor

from ..config import AppConfig
from ..devices import SampleStage


def _axis_row(
    motor: EpicsMotor,
    label: str,
    default_step: float,
    units: str,
    increment: float,
    executor=None,
) -> tuple[pn.Row, pn.widgets.FloatInput, pn.widgets.StaticText]:
    step = pn.widgets.FloatInput(name=f"step ({units})", value=default_step, step=increment, width=120)
    readback = pn.widgets.StaticText(name=f"{label} pos ({units})", value="—", width=160)
    moving = pn.indicators.LoadingSpinner(value=False, width=20, height=20)
    abs_input = pn.widgets.FloatInput(name=f"→ abs ({units})", value=0.0, step=increment, width=120)
    btn_minus = pn.widgets.Button(name=f"− {label}", button_type="primary", width=70)
    btn_plus = pn.widgets.Button(name=f"+ {label}", button_type="primary", width=70)
    btn_abs = pn.widgets.Button(name="move", button_type="default", width=60)
    btn_stop = pn.widgets.Button(name="stop", button_type="danger", width=60)

    def _track(status) -> None:
        moving.value = True

        def _done(*_a, **_kw) -> None:
            moving.value = False

        try:
            status.add_callback(_done)
        except AttributeError:
            moving.value = False

    def _move_rel(delta_sign: int):
        def _cb(_event) -> None:
            try:
                delta = delta_sign * float(step.value)
                # Route through the executor (interlock-gated) when the host injected one;
                # otherwise fall back to a direct ophyd move (standalone microscope).
                if executor is not None:
                    _track(executor.jog(motor, delta))
                else:
                    _track(motor.set(motor.position + delta))
            except Exception as exc:  # noqa: BLE001
                readback.value = f"err: {exc}"
        return _cb

    def _move_abs(_event) -> None:
        try:
            if executor is not None:
                _track(executor.move_abs(motor, float(abs_input.value)))
            else:
                _track(motor.set(float(abs_input.value)))
        except Exception as exc:  # noqa: BLE001
            readback.value = f"err: {exc}"

    def _stop(_event) -> None:
        try:
            if executor is not None:
                executor.stop(motor)
            else:
                motor.stop()
        except Exception:
            pass

    btn_minus.on_click(_move_rel(-1))
    btn_plus.on_click(_move_rel(+1))
    btn_abs.on_click(_move_abs)
    btn_stop.on_click(_stop)

    row = pn.Row(
        pn.pane.Markdown(f"**{label}**", width=20),
        btn_minus,
        btn_plus,
        step,
        readback,
        moving,
        abs_input,
        btn_abs,
        btn_stop,
    )
    return row, step, readback


class MotorPanel:
    def __init__(self, stage: SampleStage, cfg: AppConfig, executor=None) -> None:
        self.stage = stage
        self.cfg = cfg
        self.executor = executor

        default_step = cfg.ui.default_step
        units = cfg.ui.motor_units
        # Pick a sensible up/down-arrow increment based on units. Users can still type any
        # value; this just sets the +/- step in the numeric input.
        increment = 1.0 if units.lower() in ("um", "µm", "micron", "microns") else 0.001
        self._row_x, self._step_x, self._rb_x = _axis_row(
            stage.x, "X", default_step, units, increment, executor)
        self._row_y, self._step_y, self._rb_y = _axis_row(
            stage.y, "Y", default_step, units, increment, executor)
        self._row_z, self._step_z, self._rb_z = _axis_row(
            stage.z, "Z (focus)", default_step, units, increment, executor)

        self.view = pn.Column(
            pn.pane.Markdown("### Motors"),
            self._row_x,
            self._row_y,
            self._row_z,
            sizing_mode="stretch_width",
        )

    def refresh_readbacks(self) -> None:
        for motor, rb in (
            (self.stage.x, self._rb_x),
            (self.stage.y, self._rb_y),
            (self.stage.z, self._rb_z),
        ):
            try:
                rb.value = f"{motor.position:+.4f}"
            except Exception:
                rb.value = "—"

    def steps_mm(self) -> tuple[float, float, float]:
        return float(self._step_x.value), float(self._step_y.value), float(self._step_z.value)
