"""Pull frames from the EPICS camera and push RGBA-packed arrays into a Bokeh CDS.

Supports two image protocols:

- **CA** (``image_protocol="ca"``): reads ``ArrayData`` over Channel Access from an
  ``NDPluginStdArrays`` plugin (typically ``image1:ArrayData``). Reshape uses the cam's
  ``ColorMode_RBV`` plus the image plugin's ``ArraySize{0,1,2}_RBV``.

- **PVA** (``image_protocol="pva"``): reads an ``NTNDArray`` over PVAccess via ``p4p`` from an
  ``NDPluginPva`` plugin (typically ``Pva1:Image``). The NTNDArray carries dimensions and the
  color-mode attribute inline, so no separate size PVs are required.

Both paths return ``(H, W, 3)`` uint8 RGB. Downstream we pack to uint32 RGBA and optionally
subsample for the display so the WebSocket payload stays small.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from dataclasses import dataclass

import numpy as np
from bokeh.models import ColumnDataSource

from .config import EpicsConfig
from .devices import Camera

log = logging.getLogger(__name__)


# Last-resort diagnostic sink. Writes appendant lines to /tmp/swaxs_camera.log so we can see
# what's happening without relying on Bokeh's logger config. ``tail -f /tmp/swaxs_camera.log``
# to watch live.
_DIAG_PATH = "/tmp/swaxs_camera.log"


def _diag(msg: str) -> None:
    try:
        with open(_DIAG_PATH, "a") as f:
            f.write(msg.rstrip("\n") + "\n")
    except Exception:
        pass


_diag("=== camera_stream module imported ===")


# AD ColorMode_RBV string values → reshape (color_axis_position)
# RGB1 → pixel interleaved (R G B R G B …)        — flat reshapes to (H, W, 3)
# RGB2 → row    interleaved (RRR…GGG…BBB… per row)— flat reshapes to (H, 3, W) → (H, W, 3)
# RGB3 → plane  interleaved (all R, all G, all B) — flat reshapes to (3, H, W) → (H, W, 3)
# Mono → (H, W) grayscale


def pack_rgba(rgb: np.ndarray) -> np.ndarray:
    """Pack an (H, W, 3) uint8 array into a contiguous (H, W) uint32 RGBA array."""
    h, w = rgb.shape[:2]
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = 255
    return rgba.view(dtype=np.uint32).reshape(h, w)


def placeholder_image(w: int, h: int) -> np.ndarray:
    """Solid dark image with a small 'no signal' band — used when the PV is disconnected."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[..., 0] = 40
    img[h // 2 - 2 : h // 2 + 2, :, :] = (180, 30, 30)
    return pack_rgba(img)


def reshape_frame(
    flat: np.ndarray, depth: int, width: int, height: int, color_mode: str
) -> np.ndarray:
    """Reshape a flat AD ArrayData buffer into (H, W, 3) uint8 according to ColorMode."""
    flat = np.asarray(flat, dtype=np.uint8)
    expected = max(depth, 1) * width * height
    if flat.size < expected:
        raise ValueError(f"frame too small: got {flat.size}, expected {expected}")
    flat = flat[:expected]

    if color_mode == "Mono" or depth <= 1:
        mono = flat.reshape(height, width)
        return np.stack([mono] * 3, axis=-1)
    if color_mode == "RGB1":
        # Pixel-interleaved: bytes go R, G, B, R, G, B, … in row-major order.
        return flat.reshape(height, width, 3)
    if color_mode == "RGB2":
        # Row-interleaved: per row, all R then all G then all B.
        return np.moveaxis(flat.reshape(height, 3, width), 1, -1)
    # RGB3 (or default): plane-interleaved: all R, then all G, then all B.
    return np.moveaxis(flat.reshape(3, height, width), 0, -1)


def stride_for_max_dim(width: int, height: int, max_dim: int) -> int:
    """Integer stride such that subsampling by it keeps both axes ≤ ``max_dim``."""
    if max_dim <= 0:
        return 1
    s = max(1, int(np.ceil(max(width, height) / float(max_dim))))
    return s


@dataclass
class FrameDims:
    width: int
    height: int


class CameraStream:
    """Bind a Camera (and optionally a PVA image PV) to a Bokeh ColumnDataSource.

    The downstream CDS is fed packed-RGBA frames at ``poll_hz`` via :meth:`tick`.
    """

    def __init__(
        self,
        camera: Camera,
        cds: ColumnDataSource,
        fallback_size: tuple[int, int] = (640, 480),
        epics_cfg: EpicsConfig | None = None,
        display_max_dim: int = 1280,
    ) -> None:
        self.camera = camera
        self.cds = cds
        self.fallback_size = fallback_size
        self.display_max_dim = display_max_dim
        self._last_dims: FrameDims | None = None
        self._consecutive_errors = 0

        # Choose CA or PVA path.
        self._protocol = (epics_cfg.image_protocol if epics_cfg else "ca").lower()
        self._pva_ctx = None
        self._pva_pv: str | None = None
        _diag(f"CameraStream.__init__ protocol={self._protocol} display_max_dim={display_max_dim}")
        if self._protocol == "pva":
            from p4p.client.thread import Context  # local import — p4p is heavy
            self._pva_ctx = Context("pva")
            assert epics_cfg is not None
            self._pva_pv = epics_cfg.camera_prefix + epics_cfg.image_pv
            _diag(
                f"PVA mode: pv={self._pva_pv!r}  "
                f"EPICS_PVA_ADDR_LIST={os.environ.get('EPICS_PVA_ADDR_LIST')!r} "
                f"EPICS_PVA_AUTO_ADDR_LIST={os.environ.get('EPICS_PVA_AUTO_ADDR_LIST')!r} "
                f"EPICS_PVA_NAME_SERVERS={os.environ.get('EPICS_PVA_NAME_SERVERS')!r}"
            )

    def current_dims(self) -> FrameDims:
        return self._last_dims or FrameDims(*self.fallback_size)

    def grab_rgb(self) -> np.ndarray | None:
        """Read the current frame and return (H, W, 3) uint8."""
        try:
            if self._protocol == "pva":
                return self._grab_pva()
            return self._grab_ca()
        except Exception as exc:  # noqa: BLE001
            # Surface the first couple of failures at WARNING so they're visible without
            # bumping the log level. Subsequent ones drop to DEBUG to avoid spam.
            level = logging.WARNING if self._consecutive_errors < 3 else logging.DEBUG
            log.log(level, "grab_rgb failed (protocol=%s): %r", self._protocol, exc, exc_info=True)
            return None

    def grab_gray(self) -> np.ndarray | None:
        """Float32 luminance frame matched to the **displayed** resolution.

        We apply the same subsample stride that ``tick()`` uses, so phase-cross-correlation
        produces shifts in display-pixel units — the same coordinate space the user clicks
        in. Without this, the calibration matrix would be in native px / motor unit while
        click-to-move uses display px, and the move would be off by exactly the stride
        factor.
        """
        rgb = self.grab_rgb()
        if rgb is None:
            return None
        stride = stride_for_max_dim(rgb.shape[1], rgb.shape[0], self.display_max_dim)
        if stride > 1:
            rgb = rgb[::stride, ::stride, :]
        rgb = rgb.astype(np.float32)
        return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]

    # ---- protocol-specific readers --------------------------------------------------

    def _grab_ca(self) -> np.ndarray | None:
        # Use the image plugin's ArraySize{0,1,2} (NDPluginBase output dimensions), not the
        # cam plugin's X/Y/Z — the image plugin's output is what we actually receive.
        try:
            depth = int(self.camera.image.array_size0.get(use_monitor=False))
            width = int(self.camera.image.array_size1.get(use_monitor=False))
            height = int(self.camera.image.array_size2.get(use_monitor=False))
            color_mode = self.camera.cam.color_mode.get(use_monitor=False)
            flat = self.camera.image.array_data.get(use_monitor=False)
            return reshape_frame(flat, depth, width, height, color_mode)
        except Exception:
            return None

    def _grab_pva(self) -> np.ndarray | None:
        assert self._pva_ctx is not None and self._pva_pv is not None
        try:
            # p4p's NTNDArray handler returns a ``p4p.nt.ndarray.ntndarray`` — a numpy.ndarray
            # subclass that has already been reshaped per the underlying dim/color_mode. It
            # also carries ``.attrib`` (dict of NDAttributes) and ``.timestamp``.
            arr = self._pva_ctx.get(self._pva_pv, timeout=5.0)
        except Exception as exc:
            if self._consecutive_errors < 5:
                _diag(f"PVA get for {self._pva_pv!r} raised: {exc!r}")
                _diag(traceback.format_exc())
            raise

        if arr is None:
            raise RuntimeError("p4p Context.get returned None")

        # Make sure we got an actual numpy array.
        if not isinstance(arr, np.ndarray):
            raise TypeError(f"unexpected p4p return type: {type(arr).__name__}")

        if self._consecutive_errors >= 3:
            _diag(f"PVA recovered: shape={arr.shape}, dtype={arr.dtype}")
        elif self._consecutive_errors == 0:
            # First success — useful baseline log.
            if not getattr(self, "_logged_first_pva", False):
                _diag(f"PVA first frame: shape={arr.shape}, dtype={arr.dtype}, "
                      f"attrib={dict(getattr(arr, 'attrib', {}))}")
                self._logged_first_pva = True

        # Determine layout from the actual array shape (p4p has already reshaped per dims).
        if arr.ndim == 2:
            # Mono — replicate to RGB for display.
            mono = np.ascontiguousarray(arr, dtype=np.uint8)
            return np.stack([mono] * 3, axis=-1)

        if arr.ndim == 3:
            # Either (H, W, 3) or (3, H, W) depending on the AD ColorMode.
            if arr.shape[-1] == 3:
                return np.ascontiguousarray(arr, dtype=np.uint8)
            if arr.shape[0] == 3:
                return np.ascontiguousarray(np.moveaxis(arr, 0, -1), dtype=np.uint8)
            # Some IOCs emit (H, 3, W) (RGB2 row interleave) — handle as a last resort.
            if arr.shape[1] == 3:
                return np.ascontiguousarray(np.moveaxis(arr, 1, -1), dtype=np.uint8)

        raise RuntimeError(f"unexpected ntndarray shape: {arr.shape}")

    # ---- display tick ---------------------------------------------------------------

    def tick(self) -> None:
        try:
            rgb = self.grab_rgb()
            if rgb is None:
                raise RuntimeError("no frame")
            # Subsample for the display so the WS doesn't push tens of MB.
            stride = stride_for_max_dim(rgb.shape[1], rgb.shape[0], self.display_max_dim)
            disp = rgb[::stride, ::stride, :] if stride > 1 else rgb
            packed = pack_rgba(disp)
            height, width = disp.shape[:2]
            self._last_dims = FrameDims(width, height)
            self._consecutive_errors = 0
        except Exception as exc:  # disconnected PV, malformed buffer, etc.
            self._consecutive_errors += 1
            if self._consecutive_errors <= 5:
                _diag(f"tick #{self._consecutive_errors}: {type(exc).__name__}: {exc}")
                _diag(traceback.format_exc())
            if self._consecutive_errors <= 3 or self._consecutive_errors % 50 == 0:
                log.warning("camera read failed (%d in a row): %s", self._consecutive_errors, exc)
            w, h = (self._last_dims.width, self._last_dims.height) if self._last_dims else self.fallback_size
            packed = placeholder_image(w, h)
            width, height = w, h

        self.cds.data = {
            "image": [packed],
            "x": [0],
            "y": [0],
            "dw": [width],
            "dh": [height],
        }
