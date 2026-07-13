"""Alignment mode: 1D alignment scan setup plus optional ``pf`` display."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import panel as pn

from .linear import LinearScanMode
from ..scripts import alignment_line_scans_at_bookmarks


class AlignmentMode(LinearScanMode):
    """Top-level Alignment tab for SMI-specific 1D alignment scans.

    The scan setup stays in smi-acquire because it is microscope geometry and plan generation.
    Reading/displaying processed results is injected by the host app via ``pf_provider`` so this
    package does not grow direct Tiled access.
    """

    name = "Alignment"

    def __init__(
        self,
        *args,
        pf_provider: Callable[[], Any] | None = None,
        live_pf_provider: Callable[[], Any] | None = None,
        **kwargs,
    ) -> None:
        self._pf_provider = pf_provider
        self._live_pf_provider = live_pf_provider
        self._pf_fingerprint = None
        super().__init__(*args, **kwargs)

        self._script = pn.widgets.CodeEditor(
            value="", language="python", readonly=True,
            sizing_mode="stretch_width", height=230,
        )
        self._refresh_pf_btn = pn.widgets.Button(
            name="Refresh result", button_type="primary", width=130,
            disabled=self._pf_provider is None and self._live_pf_provider is None,
        )
        self._refresh_pf_btn.on_click(lambda _e: self.refresh_pf())
        self._pf_result = pn.Column(sizing_mode="stretch_width")
        self._pf_display = pn.widgets.Select(
            name="Result display", options=["pf", "livePF"], value="pf", width=150,
        )
        self._pf_display.param.watch(lambda _e: self._refresh_script(), "value")
        self._viewer_mode = pn.widgets.Select(
            name="Viewer", options=["latest", "live"], value="latest", width=120,
        )
        self._viewer_mode.param.watch(self._on_viewer_mode, "value")
        self.pf_panel = pn.Column(
            pn.Row(
                pn.pane.Markdown("#### Alignment result", margin=(8, 0, 0, 0)),
                self._viewer_mode,
                self._refresh_pf_btn,
            ),
            self._pf_result,
            sizing_mode="stretch_width",
        )

        self.panel = pn.Column(
            pn.pane.Markdown("### Alignment"),
            pn.pane.Markdown(
                "Set up a 1D alignment scan from the same scan-target samples used elsewhere. "
                "The generated plan switches to alignment mode before scanning, returns to "
                "measurement position afterward, and displays the selected result view at the end."
            ),
            pn.Row(self._distance, self._step, self._direction, self._pf_display),
            pn.Row(self._add_toggle, self._clear_placed_btn),
            self._count,
            pn.layout.Divider(),
            pn.pane.Markdown("#### Plan"),
            self._script,
            pn.layout.Divider(),
            self.pf_panel,
            sizing_mode="stretch_width",
        )

        self.set_on_change(self._refresh_script)
        self._refresh_script()
        self.refresh_pf(force=True)

    def activate(self) -> None:
        super().activate()
        self.set_overlay_enabled(True)

    def deactivate(self) -> None:
        super().deactivate()
        self.set_overlay_enabled(False)

    def script_text(self) -> str:
        params = self._params()
        if params is None:
            return "# set a positive distance and step to populate this script\n"
        dist, num, axis = params
        return alignment_line_scans_at_bookmarks(
            self._scan_targets(),
            motor_axis=axis.lower(),
            distance=dist,
            num=num,
            scripts_cfg=self.cfg.scripts,
            pf_display=self._pf_display.value,
        )

    def tick_table(self) -> None:
        self._refresh_script()

    def on_bookmarks_changed(self) -> None:
        super().on_bookmarks_changed()
        self._refresh_script()

    def _refresh_script(self) -> None:
        try:
            text = self.script_text()
        except Exception as exc:  # noqa: BLE001
            text = f"# error generating alignment script: {exc!r}"
        if text != self._script.value:
            self._script.value = text

    def _on_viewer_mode(self, _event) -> None:
        self._pf_fingerprint = None
        self._refresh_pf_button_state()
        self.refresh_pf(force=True)

    def _refresh_pf_button_state(self) -> None:
        self._refresh_pf_btn.disabled = self._active_pf_provider() is None

    def poll_pf(self) -> None:
        fingerprint = self._latest_pf_fingerprint()
        if fingerprint != self._pf_fingerprint:
            self.refresh_pf(force=True, fingerprint=fingerprint)
        else:
            self._update_pf_current_position()

    def refresh_pf(self, *, force: bool = True, fingerprint=None) -> None:
        if not force:
            self.poll_pf()
            return
        self._pf_result.clear()
        provider = self._active_pf_provider()
        if provider is None:
            self._pf_fingerprint = None
            self._pf_result.append(pn.pane.Markdown(
                f"No `{self._viewer_mode.value}` alignment result provider is attached."
            ))
            return
        try:
            if fingerprint is None:
                fingerprint = self._latest_pf_fingerprint()
            result = provider()
        except Exception as exc:  # noqa: BLE001
            self._pf_result.append(pn.pane.Markdown(f"alignment result refresh failed: `{exc!r}`"))
            return
        self._pf_fingerprint = fingerprint
        self._pf_result.append(pn.panel(result, sizing_mode="stretch_width"))

    def _update_pf_current_position(self) -> None:
        for obj in list(self._pf_result):
            updater = getattr(obj, "_pf_move_controls", None)
            if updater is not None:
                updater.update_current_position()
                return
            try:
                children = list(obj)
            except Exception:
                children = []
            for child in children:
                updater = getattr(child, "_pf_move_controls", None)
                if updater is not None:
                    updater.update_current_position()
                    return

    def _latest_pf_fingerprint(self):
        getter = getattr(self._active_pf_provider(), "fingerprint", None)
        if not callable(getter):
            return object()
        return getter()

    def _active_pf_provider(self):
        if self._viewer_mode.value == "live":
            return self._live_pf_provider
        return self._pf_provider
