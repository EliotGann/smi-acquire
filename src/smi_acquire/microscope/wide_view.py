"""Wide/top camera view: grayscale x/z navigation with a beam-path overlay."""

from __future__ import annotations

import numpy as np
import panel as pn
from bokeh.models import ColumnDataSource, LabelSet
from bokeh.plotting import figure

from .camera_stream import CameraStream, placeholder_image
from .calibration import CalibrationModel
from .config import AppConfig
from .devices import Camera, SampleStage

_COMMIT_TOLERANCE_PX = 20.0
_PLANE_EPS = 1e-3


def _alpha_for_distance(distance: float, scale: float) -> float:
    if scale <= 0:
        return 0.8
    return max(0.12, min(0.85, 0.85 * np.exp(-abs(distance) / scale)))


class WideCameraView:
    """Top/wide camera where horizontal image motion maps to z and vertical maps to x."""

    def __init__(self, cfg: AppConfig, stage: SampleStage, *, executor=None):
        self.cfg = cfg
        self.stage = stage
        self.executor = executor
        wc = cfg.wide_camera
        self.camera = Camera.from_config(wc.camera, name="wide_camera")
        w, h = wc.image_size_hint
        self.fig = figure(
            title="Wide/top camera (x/z)", x_range=(0, w), y_range=(h, 0),
            width=380, height=int(380 * h / w), tools="pan,wheel_zoom,reset,tap",
            active_scroll="wheel_zoom",
        )
        self.fig.grid.visible = False
        self.fig.axis.visible = False
        self.fig.toolbar.logo = None
        self.img_cds = ColumnDataSource(
            data={"image": [placeholder_image(w, h)], "x": [0], "y": [0], "dw": [w], "dh": [h]})
        self.stream = CameraStream(self.camera, self.img_cds, fallback_size=wc.image_size_hint,
                                   epics_cfg=wc.camera, display_max_dim=cfg.ui.display_max_dim)
        self.fig.image_rgba(image="image", x="x", y="y", dw="dw", dh="dh", source=self.img_cds)

        cx, cy = wc.center_px
        self.fig.rect(x=[cx], y=[cy], width=[wc.path_width_px], height=[wc.path_height_px],
                      fill_color="orange", fill_alpha=0.25, line_color="orange", line_width=2)
        self.fig.scatter(x=[cx], y=[cy], marker="cross", size=18, color="orange", line_width=3)

        self.calibration = CalibrationModel(wc.calibration)
        self._pending: tuple[float, float] | None = None
        self._bookmarks = []
        self._preview_cds = ColumnDataSource(data={"x": [], "y": []})
        self._line_cds = ColumnDataSource(data={"x": [], "y": []})
        self.fig.scatter("x", "y", source=self._preview_cds, marker="cross", size=22,
                         color="cyan", line_width=3)
        self.fig.line("x", "y", source=self._line_cds, color="cyan", line_dash="dashed", line_width=2)
        self._markers_cds = ColumnDataSource(data={"x": [], "y": [], "name": [], "alpha": []})
        self._oop_cds = ColumnDataSource(data={"x": [], "y": [], "marker": [], "alpha": [], "color": []})
        self.fig.scatter("x", "y", source=self._markers_cds, marker="circle", size=12,
                         fill_color="lime", fill_alpha="alpha", line_color="lime",
                         line_alpha="alpha", line_width=2)
        self.fig.scatter("x", "y", source=self._oop_cds, marker="marker", size=11,
                         fill_color="color", fill_alpha="alpha", line_color="color",
                         line_alpha="alpha")
        labels = LabelSet(x="x", y="y", text="name", source=self._markers_cds,
                          text_color="lime", text_alpha="alpha", text_font_size="9pt",
                          x_offset=8, y_offset=-5)
        self.fig.add_layout(labels)

        self.status = pn.widgets.StaticText(value="click a feature, then click again to move x/z")
        clear = pn.widgets.Button(name="cancel preview", width=130)
        clear.on_click(lambda _e: self._clear_preview())
        self.panel = pn.Column(
            pn.pane.Markdown("### Wide/top camera"),
            pn.pane.Markdown("Orange band = beam path; orange cross = target point. Click twice to move x/z."),
            pn.pane.Bokeh(self.fig),
            self.status,
            clear,
        )
        self.fig.on_event("tap", lambda event: self.on_tap(float(event.x), float(event.y)))

    def set_samples(self, entries) -> None:
        self._bookmarks = list(entries or [])
        self.tick_markers()

    def tick(self) -> None:
        try:
            self.stream.tick()
        except Exception:
            pass
        self.tick_markers()

    def tick_markers(self) -> None:
        try:
            x_now = float(self.stage.x.position)
            z_now = float(self.stage.z.position)
            y_now = float(self.stage.y.position)
        except Exception:
            x_now = z_now = y_now = 0.0
        cx, cy = self.cfg.wide_camera.center_px
        xs, ys, names, alphas = [], [], [], []
        oop_x, oop_y, oop_marker, oop_alpha, oop_color = [], [], [], [], []
        for bm in self._bookmarks:
            dp = self.calibration.motor_to_pixel_delta((z_now - bm.z, x_now - bm.x))
            px = cx + float(dp[0])
            py = cy + float(dp[1])
            xs.append(px)
            ys.append(py)
            names.append(bm.name)
            dy = bm.y - y_now
            alpha = _alpha_for_distance(y_now - bm.y, self.cfg.wide_camera.out_of_plane_fade)
            alphas.append(alpha)
            if abs(dy) > _PLANE_EPS:
                oop_x.append(px + 12)
                oop_y.append(py - 9 if dy > 0 else py + 9)
                oop_marker.append("triangle" if dy > 0 else "inverted_triangle")
                oop_alpha.append(max(alpha, 0.35))
                oop_color.append("orange" if dy > 0 else "deepskyblue")
        self._markers_cds.data = {"x": xs, "y": ys, "name": names, "alpha": alphas}
        self._oop_cds.data = {
            "x": oop_x, "y": oop_y, "marker": oop_marker,
            "alpha": oop_alpha, "color": oop_color,
        }

    def on_tap(self, x: float, y: float) -> None:
        if self._pending is None:
            self._show_preview(x, y)
            return
        px, py = self._pending
        if (x - px) ** 2 + (y - py) ** 2 <= _COMMIT_TOLERANCE_PX ** 2:
            self._commit(px, py)
        else:
            self._show_preview(x, y)

    def _show_preview(self, x: float, y: float) -> None:
        self._pending = (x, y)
        cx, cy = self.cfg.wide_camera.center_px
        dm = self.calibration.click_to_motor_delta((x, y), (cx, cy))
        self._preview_cds.data = {"x": [x], "y": [y]}
        self._line_cds.data = {"x": [cx, x], "y": [cy, y]}
        self.status.value = "preview Δz={:+.4f}, Δx={:+.4f}; click again to move".format(dm[0], dm[1])

    def _commit(self, x: float, y: float) -> None:
        cx, cy = self.cfg.wide_camera.center_px
        dz, dx = self.calibration.click_to_motor_delta((x, y), (cx, cy))
        try:
            target_x = float(self.stage.x.position) + float(dx)
            target_z = float(self.stage.z.position) + float(dz)
            self._move_axis(self.stage.x, target_x)
            self._move_axis(self.stage.z, target_z)
            self.status.value = "moving x→{:+.4f}, z→{:+.4f}".format(target_x, target_z)
        except Exception as exc:
            self.status.value = "move failed: {}".format(exc)
        self._clear_preview()

    def _move_axis(self, motor, target: float):
        if self.executor is not None:
            return self.executor.move_abs(motor, target)
        return motor.set(target)

    def _clear_preview(self) -> None:
        self._pending = None
        self._preview_cds.data = {"x": [], "y": []}
        self._line_cds.data = {"x": [], "y": []}


__all__ = ["WideCameraView", "_alpha_for_distance"]
