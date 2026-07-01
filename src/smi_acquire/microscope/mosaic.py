"""Cached map-background layer for the microscope view.

The live camera remains in the usual pixel coordinate frame (0..W, 0..H). Cached frames are
drawn behind it, shifted by motor deltas through the current calibration. That lets pan/zoom show
previously visited positions around the current frame without changing click-to-move semantics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import panel as pn
from bokeh.models import ColumnDataSource
from bokeh.plotting import figure as Figure  # noqa: N812

from .calibration import CalibrationModel
from .camera_stream import CameraStream
from .devices import SampleStage
from .overlays import BeamOverlay


def _gray(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB image to uint8 luminance."""
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]).astype(np.uint8)


def _gray_rgba(gray: np.ndarray, alpha: int = 105) -> np.ndarray:
    """Convert grayscale image to packed RGBA with fixed alpha."""
    rgba = np.empty((*gray.shape, 4), dtype=np.uint8)
    rgba[..., 0] = gray
    rgba[..., 1] = gray
    rgba[..., 2] = gray
    rgba[..., 3] = alpha
    return rgba.view(dtype=np.uint32).reshape(gray.shape)


@dataclass
class _Tile:
    gray: np.ndarray
    x: float
    y: float
    z: float
    w: int
    h: int
    timestamp: float


class MosaicBackground:
    """Faded cached camera frames plus controls for passive/active mapping."""

    def __init__(
        self,
        fig: Figure,
        stream: CameraStream,
        stage: SampleStage,
        beam: BeamOverlay,
        calibration: CalibrationModel,
        executor=None,
    ) -> None:
        self.stream = stream
        self.stage = stage
        self.beam = beam
        self.calibration = calibration
        self.executor = executor
        self._tiles: list[_Tile] = []
        self._last_pos: tuple[float, float, float] | None = None
        self._last_motion_t = time.monotonic()
        self._mapping = False
        self._capture_alpha = 115

        self.cds = ColumnDataSource(data={"image": [], "x": [], "y": [], "dw": [], "dh": []})
        self.renderer = fig.image_rgba(
            image="image", x="x", y="y", dw="dw", dh="dh", source=self.cds,
            level="image", global_alpha=1.0,
        )
        self.frame_cds = ColumnDataSource(data={"x": [0], "y": [0], "w": [0], "h": [0]})
        fig.rect(
            x="x", y="y", width="w", height="h", source=self.frame_cds,
            fill_alpha=0.0, line_color="#00bcd4", line_width=3, line_dash="dashed",
        )

        self.enabled = pn.widgets.Checkbox(name="show map background", value=True, width=190)
        self.passive = pn.widgets.Checkbox(name="passively add settled views", value=True, width=220)
        self.capture_btn = pn.widgets.Button(name="Add current view", button_type="primary", width=150)
        self.reset_btn = pn.widgets.Button(name="Reset map background", button_type="danger", width=200)
        self.nx = pn.widgets.IntInput(name="frames wide", value=5, start=1, end=50, width=120)
        self.ny = pn.widgets.IntInput(name="frames tall", value=3, start=1, end=20, width=120)
        self.overlap = pn.widgets.FloatInput(name="overlap", value=0.25, start=0.0, end=0.8,
                                             step=0.05, width=100)
        self.map_btn = pn.widgets.Button(name="Map background", button_type="success", width=160)
        self.status = pn.pane.Markdown("_No cached views yet._")

        self.enabled.param.watch(lambda _e: self._redraw(), "value")
        self.capture_btn.on_click(lambda _e: self.capture_current(force=True))
        self.reset_btn.on_click(lambda _e: self.clear())
        self.map_btn.on_click(lambda _e: self.map_grid())
        self.panel = pn.Column(
            pn.pane.Markdown("### Map Background"),
            pn.pane.Markdown(
                "Faded grayscale views are cached behind the live camera. Pan/zoom to see "
                "nearby mapped regions; the cyan dashed rectangle is the current live frame."
            ),
            pn.Row(self.enabled, self.passive),
            pn.Row(self.capture_btn, self.reset_btn),
            pn.layout.Divider(),
            pn.pane.Markdown("**Active map**"),
            pn.Row(self.nx, self.ny),
            pn.Row(self.overlap, self.map_btn),
            self.status,
        )

    def tick(self) -> None:
        self._update_frame_outline()
        if not self.passive.value or self._mapping:
            return
        pos = self._position()
        if pos is None:
            return
        now = time.monotonic()
        if self._last_pos is None or self._distance(pos, self._last_pos) > 1e-4:
            self._last_pos = pos
            self._last_motion_t = now
            return
        if now - self._last_motion_t >= 1.0:
            self.capture_current(force=False)
            self._last_motion_t = now + 999.0  # do not repeatedly capture the same settled view

    def capture_current(self, *, force: bool = False) -> bool:
        pos = self._position()
        if pos is None:
            self.status.object = "_No motor position available._"
            return False
        if not force and not self._far_enough(pos):
            return False
        rgb = self.stream.grab_rgb()
        if rgb is None:
            self.status.object = "_No camera frame to cache._"
            return False
        self._tiles.append(_Tile(
            gray=_gray(rgb),
            x=pos[0], y=pos[1], z=pos[2], w=int(rgb.shape[1]), h=int(rgb.shape[0]),
            timestamp=time.time(),
        ))
        self._tiles = self._tiles[-400:]
        self._redraw()
        return True

    def clear(self) -> None:
        self._tiles.clear()
        self._redraw()

    @property
    def active(self) -> bool:
        """True when cached map imagery is visible and can provide off-frame context."""
        return bool(self.enabled.value and self._tiles)

    def map_grid(self) -> None:
        """Synchronous small-grid mapper. Keeps UI simple; intended for dev/local use."""
        if self._mapping:
            return
        pos = self._position()
        if pos is None:
            self.status.object = "_No motor position available._"
            return
        self._mapping = True
        self.map_btn.disabled = True
        try:
            nx = max(1, int(self.nx.value))
            ny = max(1, int(self.ny.value))
            step_x, step_y = self._motor_frame_step()
            step_x *= max(0.05, 1.0 - float(self.overlap.value))
            step_y *= max(0.05, 1.0 - float(self.overlap.value))
            x0 = pos[0] - (nx - 1) * step_x / 2.0
            y0 = pos[1] - (ny - 1) * step_y / 2.0
            count = 0
            for j in range(ny):
                cols = range(nx) if j % 2 == 0 else range(nx - 1, -1, -1)
                for i in cols:
                    self._move_to(x0 + i * step_x, y0 + j * step_y)
                    time.sleep(1.0)
                    if self.capture_current(force=True):
                        count += 1
                    self.status.object = f"_Mapped {count}/{nx * ny} views._"
        finally:
            self._move_to(pos[0], pos[1])
            self._mapping = False
            self.map_btn.disabled = False
            self._redraw()

    def _position(self) -> tuple[float, float, float] | None:
        try:
            return (float(self.stage.x.position), float(self.stage.y.position), float(self.stage.z.position))
        except Exception:
            return None

    def _move_to(self, x: float, y: float) -> None:
        if self.executor is not None:
            sx = self.executor.move_abs(self.stage.x, x)
            sy = self.executor.move_abs(self.stage.y, y)
        else:
            sx = self.stage.x.set(x)
            sy = self.stage.y.set(y)
        for status in (sx, sy):
            try:
                status.wait(timeout=30)
            except AttributeError:
                pass

    def _motor_frame_step(self) -> tuple[float, float]:
        A_inv = np.linalg.inv(self.calibration.matrix)
        dx = A_inv @ np.array([self.stream.current_dims().width, 0.0], dtype=float)
        dy = A_inv @ np.array([0.0, self.stream.current_dims().height], dtype=float)
        return abs(float(dx[0])) or 1.0, abs(float(dy[1])) or 1.0

    def _far_enough(self, pos: tuple[float, float, float]) -> bool:
        if not self._tiles:
            return True
        step_x, step_y = self._motor_frame_step()
        threshold = 0.35 * max(min(step_x, step_y), 1e-9)
        return min(self._distance(pos, (t.x, t.y, t.z)) for t in self._tiles) > threshold

    @staticmethod
    def _distance(a, b) -> float:
        return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5)

    def _update_frame_outline(self) -> None:
        w, h = self.stream.current_dims().width, self.stream.current_dims().height
        self.frame_cds.data = {"x": [w / 2.0], "y": [h / 2.0], "w": [w], "h": [h]}

    def _redraw(self) -> None:
        if not self.enabled.value or not self._tiles:
            self.cds.data = {"image": [], "x": [], "y": [], "dw": [], "dh": []}
            self.status.object = "_No cached views yet._" if not self._tiles else "_Map hidden._"
            return
        pos = self._position() or (0.0, 0.0, 0.0)
        beam_px = self.beam.center
        placed = []
        for tile in self._tiles:
            dp = self.calibration.motor_to_pixel_delta((pos[0] - tile.x, pos[1] - tile.y))
            cx = beam_px[0] + float(dp[0])
            cy = beam_px[1] + float(dp[1])
            placed.append((tile, int(round(cx - tile.w / 2.0)), int(round(cy - tile.h / 2.0))))
        x_min = min(x for _tile, x, _y in placed)
        y_min = min(y for _tile, _x, y in placed)
        x_max = max(x + tile.w for tile, x, _y in placed)
        y_max = max(y + tile.h for tile, _x, y in placed)
        width = int(x_max - x_min)
        height = int(y_max - y_min)
        acc = np.zeros((height, width), dtype=np.float32)
        count = np.zeros((height, width), dtype=np.float32)
        for tile, x, y in placed:
            dx = x - x_min
            dy = y - y_min
            acc[dy:dy + tile.h, dx:dx + tile.w] += tile.gray.astype(np.float32)
            count[dy:dy + tile.h, dx:dx + tile.w] += 1.0
        mask = count > 0
        avg = np.zeros((height, width), dtype=np.uint8)
        avg[mask] = np.clip(acc[mask] / count[mask], 0, 255).astype(np.uint8)
        image = _gray_rgba(avg, self._capture_alpha)
        self.cds.data = {"image": [image], "x": [x_min], "y": [y_min], "dw": [width], "dh": [height]}
        self.status.object = f"**{len(self._tiles)}** cached map view(s)."


__all__ = ["MosaicBackground"]
