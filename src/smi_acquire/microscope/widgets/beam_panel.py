"""UI for editing the beam-position overlay."""

from __future__ import annotations

import panel as pn

from ..config import AppConfig
from ..overlays import BeamOverlay


class BeamPanel:
    def __init__(self, cfg: AppConfig, overlay: BeamOverlay, *, config_store=None) -> None:
        self.cfg = cfg
        self.overlay = overlay
        self.config_store = config_store

        cx, cy = cfg.beam.center_px
        self._cx = pn.widgets.FloatInput(name="center x (px)", value=cx, step=1, width=120)
        self._cy = pn.widgets.FloatInput(name="center y (px)", value=cy, step=1, width=120)
        self._w = pn.widgets.FloatInput(name="width (px)", value=cfg.beam.width_px, step=1, width=110)
        self._h = pn.widgets.FloatInput(name="height (px)", value=cfg.beam.height_px, step=1, width=110)

        preset_names = ["—"] + list(cfg.beam.presets.keys())
        self._preset = pn.widgets.Select(name="preset", options=preset_names, value="—", width=120)
        self._save = pn.widgets.Button(name="save to config", button_type="primary", width=130)
        self._load_store = pn.widgets.Button(name="load from store", width=130)
        self._save_store = pn.widgets.Button(name="save to store", width=130)
        self._status = pn.widgets.StaticText(name="", value="", width=300)

        for widget in (self._cx, self._cy, self._w, self._h):
            widget.param.watch(self._apply, "value")
        self._preset.param.watch(self._on_preset, "value")
        self._save.on_click(self._on_save)
        self._load_store.on_click(self._on_load_store)
        self._save_store.on_click(self._on_save_store)

        self.view = pn.Column(
            pn.pane.Markdown("### Beam"),
            pn.Row(self._cx, self._cy),
            pn.Row(self._w, self._h),
            pn.Row(self._preset, self._save),
            pn.Row(self._load_store, self._save_store),
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
        self._update_config_from_widgets()
        try:
            self.cfg.save()
            self._status.value = "saved to config file."
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"save failed: {exc}"

    def _update_config_from_widgets(self) -> None:
        self.cfg.beam.center_px = (float(self._cx.value), float(self._cy.value))
        self.cfg.beam.width_px = float(self._w.value)
        self.cfg.beam.height_px = float(self._h.value)

    def _payload(self) -> dict:
        return {
            "center_px": [float(self._cx.value), float(self._cy.value)],
            "width_px": float(self._w.value),
            "height_px": float(self._h.value),
        }

    def _on_save_store(self, _event) -> None:
        if self.config_store is None:
            self._status.value = "config store unavailable."
            return
        try:
            self._update_config_from_widgets()
            self.config_store.put("microscope.beam", self._payload())
            self._status.value = f"saved to config store ({self.config_store.location})."
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"store save failed: {exc}"

    def _on_load_store(self, _event) -> None:
        if self.config_store is None:
            self._status.value = "config store unavailable."
            return
        try:
            data = self.config_store.get("microscope.beam")
            if not data:
                self._status.value = "no beam config in store."
                return
            center = data.get("center_px", self.cfg.beam.center_px)
            self._cx.value = float(center[0])
            self._cy.value = float(center[1])
            self._w.value = float(data.get("width_px", self.cfg.beam.width_px))
            self._h.value = float(data.get("height_px", self.cfg.beam.height_px))
            self._update_config_from_widgets()
            self._apply()
            self._status.value = "loaded from config store; use save to config to update YAML."
        except Exception as exc:  # noqa: BLE001
            self._status.value = f"store load failed: {exc}"
