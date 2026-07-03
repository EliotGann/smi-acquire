"""Read and render the latest published ``pf`` alignment result.

The profile/smi-plans side owns computing ``pf`` and publishing
``PeakResult.as_dict(arrays=True)``.  This module only reads that JSON payload from the
operational config Redis area via :class:`smi_acquire.config_store.AcquireConfigStore` and turns it
into Panel/Bokeh display objects.
"""

from __future__ import annotations

import math
import json
from typing import Any

import panel as pn
from bokeh.events import Tap
from bokeh.models import Span

from .config_store import AcquireConfigStore


PF_CONFIG_KEY = "alignment.pf.latest"


class LatestPFProvider:
    """Callable provider for the Alignment tab's persistent latest-``pf`` pane."""

    def __init__(
        self,
        config_store: AcquireConfigStore,
        *,
        key: str = PF_CONFIG_KEY,
        stage=None,
        executor=None,
    ) -> None:
        self.config_store = config_store
        self.key = key
        self.stage = stage
        self.executor = executor

    def __call__(self):
        payload = self.payload()
        return pf_payload_panel(
            payload,
            source=self.config_store.location,
            key=self.key,
            stage=self.stage,
            executor=self.executor,
        )

    def payload(self) -> dict[str, Any] | None:
        return self.config_store.get(self.key)

    def fingerprint(self) -> str | None:
        payload = self.payload()
        if payload is None:
            return None
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def pf_payload_panel(
    payload: dict[str, Any] | None,
    *,
    source: str = "",
    key: str = PF_CONFIG_KEY,
    stage=None,
    executor=None,
):
    """Build a Panel view for a published ``pf`` payload."""
    if not payload:
        return pn.pane.Markdown(
            f"No published `pf` result found at `{key}`. Run `pf(..., publish=True)` after "
            "smi-plans implements publishing."
        )

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    title = _title(payload, result)
    fig = _pf_figure(result, title=title)
    summary = _summary_markdown(payload, result, source=source, key=key)
    controls = _PFMoveControls(result, fig, stage=stage, executor=executor).view
    return pn.Column(fig, controls, summary, sizing_mode="stretch_width")


class _PFMoveControls:
    def __init__(self, result: dict[str, Any], fig, *, stage=None, executor=None) -> None:
        self.result = result
        self.stage = stage
        self.executor = executor
        self.motor_name = str(result.get("motor") or "")
        self.motor = _controlled_motor(stage, self.motor_name)
        self._selected: float | None = None
        self._selection_label: str | None = None
        self._marker = Span(location=0, dimension="height", line_color="#ff7f0e",
                            line_dash="solid", line_width=3, visible=False)
        self._current_marker = Span(location=0, dimension="height", line_color="#9467bd",
                                    line_dash="dotdash", line_width=3,
                                    visible=self.motor is not None)
        if self.motor is not None:
            pos = self._motor_position()
            if pos is not None:
                self._current_marker.location = pos
        fig.add_layout(self._marker)
        fig.add_layout(self._current_marker)
        fig.on_event(Tap, self._on_tap)

        self._status = pn.widgets.StaticText(value=self._initial_status(), sizing_mode="stretch_width")
        self._goto = pn.widgets.Button(name="Go to selected", button_type="primary", width=140,
                                      disabled=True)
        self._goto.on_click(self._move)
        buttons = [self._candidate_button(name) for name in ("cen", "com", "peak")]
        self.view = pn.Column(
            pn.Row(*buttons, self._goto),
            self._status,
            sizing_mode="stretch_width",
        )
        self.view._pf_move_controls = self

    def _initial_status(self) -> str:
        if self.motor is None:
            return f"`{self.motor_name or 'unknown'}` is not a controlled piezo/Huber motor."
        pos = self._motor_position()
        here = "unknown" if pos is None else f"{pos:.6g}"
        return (
            f"current `{self.motor_name}` = {here} (purple); "
            "click the plot or choose cen/com/peak to select target (orange)."
        )

    def _candidate_button(self, name: str):
        value = _float(self.result.get(name))
        label = f"{name}: {_fmt(value)}"
        btn = pn.widgets.Button(name=label, width=120, disabled=value is None)

        def _cb(_event) -> None:
            if value is not None:
                self._select(value, name)

        btn.on_click(_cb)
        return btn

    def _on_tap(self, event) -> None:
        self._select(float(event.x), "plot click")

    def _select(self, value: float, source: str) -> None:
        self._selected = float(value)
        self._selection_label = source
        self._marker.location = self._selected
        self._marker.visible = True
        self._goto.disabled = self.motor is None
        self._render_status()

    def _move(self, _event) -> None:
        if self.motor is None or self._selected is None:
            return
        try:
            if self.executor is not None:
                self.executor.move_abs(self.motor, self._selected)
            else:
                self.motor.set(self._selected)
            self._status.value = f"moving `{self.motor_name}` to {self._selected:.6g}"
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"move failed: {exc}"

    def update_current_position(self) -> None:
        pos = self._motor_position()
        self._current_marker.visible = pos is not None
        if pos is not None:
            self._current_marker.location = pos
        self._render_status()

    def _render_status(self) -> None:
        if self.motor is None:
            if self._selected is None:
                self._status.value = self._initial_status()
            else:
                self._status.value = (
                    f"selected {self._selection_label}: {self._selected:.6g} on "
                    f"`{self.motor_name}` (move unavailable)"
                )
            return
        pos = self._motor_position()
        here = "unknown" if pos is None else f"{pos:.6g}"
        if self._selected is None:
            self._status.value = (
                f"current `{self.motor_name}` = {here} (purple); "
                "click the plot or choose cen/com/peak to select target (orange)."
            )
        else:
            self._status.value = (
                f"current `{self.motor_name}` = {here} (purple); selected "
                f"{self._selection_label}: {self._selected:.6g} (orange)."
            )

    def _motor_position(self) -> float | None:
        if self.motor is None:
            return None
        try:
            return float(self.motor.position)
        except Exception:
            return None


def _title(payload: dict[str, Any], result: dict[str, Any]) -> str:
    scan_id = payload.get("scan_id", result.get("scan_id", "?"))
    motor = result.get("motor") or "x"
    det = result.get("detector") or "intensity"
    return f"pf scan {scan_id} · {motor} vs {det}"


def _summary_markdown(
    payload: dict[str, Any], result: dict[str, Any], *, source: str, key: str,
) -> pn.pane.Markdown:
    updated = payload.get("updated") or result.get("timestamp") or "unknown"
    scan_id = payload.get("scan_id", result.get("scan_id", "?"))
    uid = payload.get("uid", result.get("uid", ""))
    bits = [
        f"**source:** `{source}`",
        f"**key:** `{key}`",
        f"**updated:** `{updated}`",
        f"**scan:** `{scan_id}`",
    ]
    if uid:
        bits.append(f"**uid:** `{uid}`")
    bits.extend([
        f"**kind/model:** `{result.get('profile_kind', 'n/a')}` / `{result.get('model_name', 'n/a')}`",
        f"**cen:** `{_fmt(result.get('cen'))}`",
        f"**fwhm:** `{_fmt(result.get('fwhm'))}`",
        f"**peak:** `{_fmt(result.get('peak'))}`",
        f"**com:** `{_fmt(result.get('com'))}`",
        f"**cen_base:** `{_fmt(result.get('cen_base'))}`",
        f"**fw_base:** `{_fmt(result.get('fw_base'))}`",
        f"**left_base:** `{_fmt(result.get('left_base'))}`",
        f"**right_base:** `{_fmt(result.get('right_base'))}`",
        f"**baseline_noise:** `{_fmt(result.get('baseline_noise'))}`",
        f"**baseline_threshold:** `{_fmt(result.get('baseline_threshold'))}`",
        f"**R^2:** `{_fmt(result.get('r_squared'))}`",
    ])
    msg = result.get("message")
    if msg:
        bits.append(f"**message:** {msg}")
    return pn.pane.Markdown("  \n".join(bits), sizing_mode="stretch_width")


def _pf_figure(result: dict[str, Any], *, title: str):
    from bokeh.models import Band, ColumnDataSource, HoverTool
    from bokeh.plotting import figure

    x = _numbers(result.get("x"))
    y = _numbers(result.get("y"))
    yerr = _numbers(result.get("yerr"))
    if len(yerr) != len(y):
        yerr = [0.0] * len(y)
    lower = [v - e for v, e in zip(y, yerr)]
    upper = [v + e for v, e in zip(y, yerr)]

    p = figure(
        title=title,
        height=360,
        sizing_mode="stretch_width",
        x_axis_label=str(result.get("motor") or "x"),
        y_axis_label=str(result.get("detector") or "intensity"),
        tools="pan,box_zoom,wheel_zoom,reset,save",
        toolbar_location="above",
    )

    if x and y:
        src = ColumnDataSource({"x": x, "y": y, "lower": lower, "upper": upper})
        if any(e > 0 for e in yerr):
            p.add_layout(Band(base="x", lower="lower", upper="upper", source=src,
                              fill_color="#1f77b4", fill_alpha=0.12, line_width=0))
        points = p.scatter("x", "y", source=src, size=7, marker="circle", color="#1f77b4")
        p.line("x", "y", source=src, color="#1f77b4", alpha=0.35)
        p.add_tools(HoverTool(renderers=[points], tooltips=[("x", "@x"), ("y", "@y")]))

    fit_x = _numbers(result.get("fit_x"))
    fit_y = _numbers(result.get("fit_y"))
    if fit_x and fit_y:
        n = min(len(fit_x), len(fit_y))
        p.line(fit_x[:n], fit_y[:n], color="#d62728", line_width=2.5,
               legend_label=f"fit: {result.get('model_name', 'model')}")

    _vspan(p, result.get("cen"), "#d62728", "solid")
    _vspan(p, result.get("peak"), "#000000", "dashed")
    _vspan(p, result.get("com"), "#2ca02c", "dotted")
    if p.legend:
        p.legend.location = "top_right"
        p.legend.click_policy = "hide"
    return p


def _vspan(plot, value: Any, color: str, dash: str) -> None:
    value = _float(value)
    if value is None:
        return
    from bokeh.models import Span

    plot.add_layout(Span(location=value, dimension="height", line_color=color,
                         line_dash=dash, line_width=2))


def _controlled_motor(stage, motor_name: str):
    if stage is None or not motor_name:
        return None
    try:
        axes = stage.all_axes()
    except Exception:
        return None
    aliases = _motor_aliases(motor_name)
    for name in aliases:
        motor = axes.get(name)
        if motor is not None:
            return motor
    return None


def _motor_aliases(name: str) -> list[str]:
    raw = str(name or "").strip()
    if not raw:
        return []
    aliases = [raw]
    dotted = raw.replace(".", "_")
    aliases.append(dotted)
    if dotted.startswith("stage_"):
        aliases.append("stage_" + dotted[len("stage_"):])
    if dotted.startswith("huber_"):
        aliases.append("stage_" + dotted[len("huber_"):])
    if dotted.startswith("piezo_"):
        aliases.append(dotted)
    elif dotted in {"x", "y", "z", "th", "chi"}:
        aliases.append("piezo_" + dotted)
    return list(dict.fromkeys(aliases))


def _numbers(values: Any) -> list[float]:
    if values is None:
        return []
    out: list[float] = []
    for value in values:
        f = _float(value)
        if f is not None:
            out.append(f)
    return out


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except Exception:
        return None
    return f if math.isfinite(f) else None


def _fmt(value: Any) -> str:
    f = _float(value)
    return "n/a" if f is None else f"{f:.6g}"


__all__ = ["LatestPFProvider", "PF_CONFIG_KEY", "pf_payload_panel"]
