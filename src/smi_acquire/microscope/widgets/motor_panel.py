"""Motor jog and absolute-move panel.

Rows of controls (one per axis) with:
- jog ± buttons (using a per-axis step input)
- live readback display
- absolute-move input
- a "moving" indicator that lights while a motion is in flight

The full stacked stage is shown in two sections — the piezo fine stage (µm) and the Huber
coarse stage (mm) — each with its own units. Moves route through the executor (interlock-gated).
"""

from __future__ import annotations

import panel as pn
from ophyd import EpicsMotor

from ..config import AppConfig
from ..devices import SampleStage


# Per-stack display: axis order, the section's units, and a sensible default jog step.
_PIEZO_AXES = (("x", "X"), ("y", "Y"), ("z", "Z (focus)"), ("th", "θ"), ("chi", "χ"))
_HUBER_AXES = (("x", "X"), ("y", "Y"), ("z", "Z"),
               ("theta", "θ"), ("chi", "χ"), ("phi", "φ"))


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

        # (motor, readback_widget) pairs to refresh each tick, across all sections.
        self._readbacks: list = []
        # Back-compat handles for the primary x/y/z step inputs (used by steps_mm()).
        self._step_x = self._step_y = self._step_z = None

        sections = []
        piezo_units = cfg.ui.motor_units            # the on-axis primary units (µm at SMI)
        sections.append(self._build_section(
            "Piezo (fine)", getattr(stage, "piezo", None), _PIEZO_AXES, piezo_units,
            fallback={"x": stage.x, "y": stage.y, "z": stage.z}))
        # The Huber coarse stage is mm regardless of the piezo's units.
        huber_units = "mm"
        sections.append(self._build_section(
            "Huber (coarse)", getattr(stage, "huber", None), _HUBER_AXES, huber_units))

        self.view = pn.Column(pn.pane.Markdown("### Motors"),
                              *[s for s in sections if s is not None],
                              sizing_mode="stretch_width")

    def _build_section(self, title, group, axes, units, fallback=None):
        """A titled block of axis rows for one stack; returns None if no axes are present."""
        fallback = fallback or {}
        is_um = units.lower() in ("um", "µm", "micron", "microns")
        default_step = self.cfg.ui.default_step if is_um else 0.01
        increment = 1.0 if is_um else 0.001

        rows = []
        for axis, label in axes:
            motor = getattr(group, axis, None) if group is not None else fallback.get(axis)
            if motor is None:
                continue
            row, step, rb = _axis_row(motor, label, default_step, units, increment, self.executor)
            rows.append(row)
            self._readbacks.append((motor, rb))
            # Keep primary x/y/z step handles for the legacy steps_mm() accessor.
            if title.startswith("Piezo") and axis in ("x", "y", "z"):
                setattr(self, f"_step_{axis}", step)
        if not rows:
            return None
        return pn.Column(
            pn.pane.Markdown(f"**{title}** · _{units}_"), *rows,
            styles={"background": "#f6f8fa", "padding": "4px 8px", "border-radius": "6px"},
            margin=(0, 0, 8, 0))

    def refresh_readbacks(self) -> None:
        for motor, rb in self._readbacks:
            try:
                rb.value = f"{motor.position:+.4f}"
            except Exception:
                rb.value = "—"

    def steps_mm(self) -> tuple[float, float, float]:
        def _v(w):
            return float(w.value) if w is not None else 0.0
        return _v(self._step_x), _v(self._step_y), _v(self._step_z)
