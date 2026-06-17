"""UI for editing the beam-position overlay."""

from __future__ import annotations

import panel as pn

from ..config import AppConfig
from ..overlays import BeamOverlay


class BeamPanel:
    def __init__(self, cfg: AppConfig, overlay: BeamOverlay) -> None:
        self.cfg = cfg
        self.overlay = overlay

        cx, cy = cfg.beam.center_px
        self._cx = pn.widgets.FloatInput(name="center x (px)", value=cx, step=1, width=120)
        self._cy = pn.widgets.FloatInput(name="center y (px)", value=cy, step=1, width=120)
        self._w = pn.widgets.FloatInput(name="width (px)", value=cfg.beam.width_px, step=1, width=110)
        self._h = pn.widgets.FloatInput(name="height (px)", value=cfg.beam.height_px, step=1, width=110)

        preset_names = ["—"] + list(cfg.beam.presets.keys())
        self._preset = pn.widgets.Select(name="preset", options=preset_names, value="—", width=120)
        self._save = pn.widgets.Button(name="save to config", button_type="primary", width=130)
        self._status = pn.widgets.StaticText(name="", value="", width=300)

        for widget in (self._cx, self._cy, self._w, self._h):
            widget.param.watch(self._apply, "value")
        self._preset.param.watch(self._on_preset, "value")
        self._save.on_click(self._on_save)

        self.view = pn.Column(
            pn.pane.Markdown("### Beam"),
            pn.Row(self._cx, self._cy),
            pn.Row(self._w, self._h),
            pn.Row(self._preset, self._save),
            self._status,
            sizing_mode="stretch_width",
        )

    def _apply(self, _event=None) -> None:
        self.overlay.update(
            center_px=(float(self._cx.value), float(self._cy.value)),
            width_px=float(self._w.value),
            height_px=float(self._h.value),
        )

    def _on_preset(self, event) -> None:
        if event.new == "—":
            return
        preset = self.cfg.beam.presets.get(event.new)
        if preset is None:
            return
        self._w.value = preset.width_px
        self._h.value = preset.height_px

    def _on_save(self, _event) -> None:
        self.cfg.beam.center_px = (float(self._cx.value), float(self._cy.value))
        self.cfg.beam.width_px = float(self._w.value)
        self.cfg.beam.height_px = float(self._h.value)
        try:
            self.cfg.save()
            self._status.value = "saved."
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"save failed: {exc}"
