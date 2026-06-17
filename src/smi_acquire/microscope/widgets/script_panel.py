"""Quick scripts — color-coded, independently toggled scan snippets under the image.

Each scan *kind* (Bookmarks, Square, Polygon, Linear) gets its own tab with an **ON/OFF
toggle**. The toggle governs the kind independently of the others:

- **ON**  — the kind's on-image overlay is drawn (via ``provider.set_overlay_enabled(True)``)
  and its live quick-script is shown.
- **OFF** — the overlay is cleared and the script hidden.

So turning on the Linear line never lights up the Square grid: each kind is its own switch.
Tab labels are color-coded to match the on-image overlays (green bookmarks, orange grids,
magenta lines). Point-based scans show a wall-clock estimate at 3 s/point; the bookmark
list scan isn't estimated (the operator drives those interactively).
"""

from __future__ import annotations

import logging

import panel as pn

from ..scripts import estimate_label

log = logging.getLogger(__name__)


class _ScriptSection:
    """One color-coded scan kind: ON/OFF toggle + explanation + code + time estimate.

    The toggle drives ``provider.set_overlay_enabled(...)`` (if the provider exposes it) so the
    on-image overlay shows/hides in lockstep with the script body.
    """

    def __init__(self, provider, *, enabled: bool = False) -> None:
        self.provider = provider
        self.title = getattr(provider, "script_kind", getattr(provider, "name", "Scan"))
        self.color = getattr(provider, "script_color", "#888888")
        explanation = getattr(provider, "script_explanation", "")
        self._enabled = bool(enabled)

        self._toggle = pn.widgets.Toggle(value=self._enabled, width=150, margin=(4, 0, 6, 4))
        self._toggle.param.watch(self._on_toggle, "value")

        self._explanation = pn.pane.Markdown(
            explanation, sizing_mode="stretch_width", margin=(6, 0, 0, 4),
        )
        self._estimate = pn.pane.HTML("", sizing_mode="stretch_width", margin=(0, 0, 0, 4))
        self._code = pn.widgets.CodeEditor(
            value="", language="python", readonly=True,
            sizing_mode="stretch_width", height=170,
        )
        self._body = pn.Column(
            self._explanation, self._estimate, self._code, sizing_mode="stretch_width",
        )

        self.view = pn.Column(
            self._toggle, self._body, sizing_mode="stretch_width", margin=(0, 0, 6, 0),
        )
        # Sync the initial state down to the provider + style the toggle.
        self._apply_enabled()
        self.refresh()

    # ---- enable / disable -----------------------------------------------------------

    def _on_toggle(self, _event) -> None:
        self._enabled = bool(self._toggle.value)
        self._apply_enabled()
        self.refresh()

    def _apply_enabled(self) -> None:
        on = self._enabled
        self._toggle.name = f"{self.title} \u00b7 {'ON' if on else 'OFF'}"
        self._toggle.button_type = "success" if on else "default"
        self._toggle.button_style = "solid" if on else "outline"
        self._toggle.icon = "check" if on else "circle"
        self._body.visible = on
        setter = getattr(self.provider, "set_overlay_enabled", None)
        if callable(setter):
            try:
                setter(on)
            except Exception:  # noqa: BLE001
                log.debug("set_overlay_enabled failed for %s", self.title, exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- live content ---------------------------------------------------------------

    def refresh(self) -> None:
        if not self._enabled:
            return
        try:
            text = self.provider.script_text()
        except Exception as exc:  # noqa: BLE001
            text = f"# error generating script: {exc!r}"
        if text != self._code.value:
            self._code.value = text

        try:
            pts = int(self.provider.total_points())
        except Exception:
            pts = 0
        label = estimate_label(pts)
        html = (
            f"<span style='color:{self.color};font-weight:600;'>\u23f1 {label}</span>"
            if label else ""
        )
        if html != self._estimate.object:
            self._estimate.object = html


def _tab_color_stylesheet(colors: list[str]) -> str:
    """Color-code each tab label by position to match the on-image overlays."""
    rules = []
    for i, color in enumerate(colors, start=1):
        rules.append(
            f".bk-header .bk-tab:nth-child({i}) {{color:{color};font-weight:600;}}"
        )
        rules.append(
            f".bk-header .bk-tab:nth-child({i}).bk-active {{"
            f"color:{color};border-color:{color};}}"
        )
    return "\n".join(rules)


# Kinds enabled on first load — only the bookmark list, so the image starts uncluttered and
# each scan overlay is opt-in.
_DEFAULT_ON = {"Bookmarks"}


class ScriptPanel:
    """Color-coded tabs — one :class:`_ScriptSection` (with its own ON/OFF toggle) per provider.

    ``providers`` are mode objects exposing ``script_kind``, ``script_color``,
    ``script_text()``, ``total_points()`` and (optionally) ``set_overlay_enabled(bool)`` —
    e.g. InteractiveMode, SquareScanMode, AreaMode, LinearScanMode.
    """

    def __init__(self, providers: list) -> None:
        self._sections = [
            _ScriptSection(p, enabled=(getattr(p, "script_kind", "") in _DEFAULT_ON))
            for p in providers
        ]
        self._tabs = pn.Tabs(
            *[(s.title, s.view) for s in self._sections],
            dynamic=False,
            sizing_mode="stretch_width",
            stylesheets=[_tab_color_stylesheet([s.color for s in self._sections])],
        )
        self.view = pn.Column(
            pn.pane.Markdown(
                "### Quick scripts &nbsp; "
                "<span style='font-weight:400;font-size:12px;color:#888'>"
                "toggle each kind on/off independently · color-matched to the overlays · "
                "time est. at 3 s/point</span>"
            ),
            self._tabs,
            sizing_mode="stretch_width",
        )
        self.refresh()

    def refresh(self) -> None:
        for s in self._sections:
            s.refresh()
