"""Connection / health status footer."""

from __future__ import annotations

import panel as pn

from ..devices import Camera, SampleStage


class StatusBar:
    def __init__(self, camera: Camera, stage: SampleStage) -> None:
        self.camera = camera
        self.stage = stage
        self._cam = pn.widgets.StaticText(name="camera", value="…", width=200)
        self._mx = pn.widgets.StaticText(name="X", value="…", width=140)
        self._my = pn.widgets.StaticText(name="Y", value="…", width=140)
        self._mz = pn.widgets.StaticText(name="Z", value="…", width=140)
        self.view = pn.Row(self._cam, self._mx, self._my, self._mz, sizing_mode="stretch_width")

    def refresh(self) -> None:
        try:
            connected = self.camera.image.array_data.connected and self.camera.cam.color_mode.connected
            self._cam.value = "connected" if connected else "disconnected"
        except Exception:
            self._cam.value = "error"

        for motor, label, slot in (
            (self.stage.x, "X", self._mx),
            (self.stage.y, "Y", self._my),
            (self.stage.z, "Z", self._mz),
        ):
            try:
                slot.value = f"{label}: {motor.position:+.4f}"
            except Exception:
                slot.value = f"{label}: —"
