"""Pull frames from the EPICS camera and push RGBA-packed arrays into a Bokeh CDS.

Supports two image protocols:

- **CA** (``image_protocol="ca"``): reads ``ArrayData`` over Channel Access from an
  ``NDPluginStdArrays`` plugin (typically ``image1:ArrayData``). Reshape uses the cam's
  ``ColorMode_RBV`` plus the image plugin's ``ArraySize{0,1,2}_RBV``.

- **PVA** (``image_protocol="pva"``): subscribes to an ``NTNDArray`` over PVAccess via ``p4p``
  from an ``NDPluginPva`` plugin (typically ``Pva1:Image``). The NTNDArray carries dimensions and
  the color-mode attribute inline, so no separate size PVs are required.

Both paths return ``(H, W, 3)`` uint8 RGB. Downstream we pack to uint32 RGBA and optionally
subsample for the display so the WebSocket payload stays small.

**Why PVA uses a background monitor, not a per-tick get.** The Bokeh periodic callbacks
(:meth:`tick`) run on the server's single Tornado IOLoop thread — the *same* thread that
serialises frames and pushes them down the WebSocket to the browser. A synchronous
``Context.get(timeout=5)`` for a multi-megabyte ``NTNDArray`` (the beamline camera is
3296×2472×3 ≈ 24 MB) blocks that thread for the whole round-trip, starving the WebSocket and
causing the "missed frames" the operator sees. Instead we open one :meth:`p4p.client.thread.Context.monitor`
subscription per stream: p4p's worker thread receives each new frame, reshapes it to RGB, and
stashes it in :attr:`_latest_rgb`. :meth:`tick` then only reads that cached reference (cheap,
non-blocking) and pushes it out. The heavy reshape/copy also happens off the IOLoop.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
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


def to_uint8(arr: np.ndarray, bit_depth: int | None = None) -> np.ndarray:
    """Scale raw image samples down to uint8 for display.

    Cameras deliver 8-, 12-, or 16-bit data (the latter two stored in ``uint16``). The browser
    only shows 8-bit RGBA, so wider samples must be right-shifted. ``bit_depth`` gives the number
    of significant bits (e.g. 12 for a 12-bit sensor in a uint16 buffer → ``>> 4``); when omitted
    we infer it from the dtype width (uint16 → 16, uint8 → 8), which is correct for sensors that
    fill the full dtype but renders 12-bit-in-uint16 data too dark — so set ``bit_depth``
    explicitly for those.
    """
    if arr.dtype == np.uint8:
        return arr
    if bit_depth is None:
        bit_depth = arr.dtype.itemsize * 8
    shift = max(0, int(bit_depth) - 8)
    if shift == 0:
        return np.asarray(arr, dtype=np.uint8)
    # Right-shift keeps the top 8 significant bits; works for any unsigned integer dtype.
    return np.right_shift(arr, shift).astype(np.uint8)


def ntndarray_to_rgb(arr: np.ndarray, bit_depth: int | None = None) -> np.ndarray:
    """Convert a p4p ``ntndarray`` (already reshaped per its NTNDArray dims) to (H, W, 3) uint8.

    p4p hands us a ``numpy.ndarray`` subclass whose shape reflects the areaDetector ColorMode:
    ``(H, W)`` Mono, ``(H, W, 3)`` RGB1, ``(3, H, W)`` RGB3, or ``(H, 3, W)`` RGB2. Anything else
    (notably a zero-length ``(0,)`` array, which the IOC transiently emits between acquisitions)
    is rejected so the caller can skip it rather than treat it as a real frame. ``bit_depth``
    (optional) scales >8-bit samples down to 8-bit — see :func:`to_uint8`.
    """
    arr = to_uint8(arr, bit_depth)
    if arr.ndim == 2:
        # Mono — replicate to RGB for display.
        mono = np.ascontiguousarray(arr)
        return np.stack([mono] * 3, axis=-1)
    if arr.ndim == 3:
        # Either (H, W, 3) or (3, H, W) depending on the AD ColorMode.
        if arr.shape[-1] == 3:
            return np.ascontiguousarray(arr)
        if arr.shape[0] == 3:
            return np.ascontiguousarray(np.moveaxis(arr, 0, -1))
        # Some IOCs emit (H, 3, W) (RGB2 row interleave) — handle as a last resort.
        if arr.shape[1] == 3:
            return np.ascontiguousarray(np.moveaxis(arr, 1, -1))
    raise ValueError(f"unexpected ntndarray shape: {arr.shape}")


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
        self._bit_depth = getattr(epics_cfg, "bit_depth", None) if epics_cfg else None
        self._last_dims: FrameDims | None = None
        self._consecutive_errors = 0

        # Choose CA or PVA path.
        self._protocol = (epics_cfg.image_protocol if epics_cfg else "ca").lower()
        self._pva_ctx = None
        self._pva_pv: str | None = None
        self._pva_sub = None
        # Latest frame delivered by the PVA monitor, plus a monotonic timestamp so callers
        # (autofocus) can tell a fresh post-move frame from a stale one. Guarded by a lock
        # because it is written from p4p's worker thread and read from the Bokeh IOLoop thread.
        self._latest_rgb: np.ndarray | None = None
        self._latest_stamp: float = 0.0
        self._pva_last_error: BaseException | None = None
        self._frame_lock = threading.Lock()
        self._logged_first_pva = False
        # Display backpressure: we never enqueue a new image patch while the previous one is
        # still waiting to be flushed to the browser. This coalesces bursts so the Bokeh IOLoop
        # (which serialises + writes the multi-MB RGBA buffer over the WebSocket) is never
        # starved by a backlog of image pushes — that backlog is what makes buttons feel laggy.
        self._push_pending = False
        self._push_scheduled_at = 0.0
        self._last_pushed_stamp = -1.0
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
            # Subscribe once; frames arrive asynchronously on p4p's worker thread. We keep only
            # the most recent one — the display never needs a backlog.
            self._pva_sub = self._pva_ctx.monitor(
                self._pva_pv, self._on_pva_update, notify_disconnect=True
            )

    def current_dims(self) -> FrameDims:
        return self._last_dims or FrameDims(*self.fallback_size)

    def frame_stamp(self) -> float:
        """Monotonic timestamp of the last frame the PVA monitor delivered (0 if none/CA)."""
        with self._frame_lock:
            return self._latest_stamp

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

    @staticmethod
    def _to_gray_display(rgb: np.ndarray, display_max_dim: int) -> np.ndarray:
        stride = stride_for_max_dim(rgb.shape[1], rgb.shape[0], display_max_dim)
        if stride > 1:
            rgb = rgb[::stride, ::stride, :]
        rgb = rgb.astype(np.float32)
        return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]

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
        return self._to_gray_display(rgb, self.display_max_dim)

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

    def _on_pva_update(self, value) -> None:
        """p4p monitor callback — runs on p4p's worker thread, **not** the Bokeh IOLoop.

        Reshapes each incoming NTNDArray to RGB here (off the IOLoop) and caches only the most
        recent frame. Zero-length arrays (emitted transiently between acquisitions) and
        disconnect notifications are recorded but never overwrite a good frame.
        """
        # notify_disconnect=True means we may be handed Disconnected/RemoteError/Cancelled.
        if isinstance(value, Exception):
            with self._frame_lock:
                self._pva_last_error = value
            _diag(f"PVA monitor for {self._pva_pv!r} event: {value!r}")
            return
        try:
            if not isinstance(value, np.ndarray):
                raise TypeError(f"unexpected p4p type: {type(value).__name__}")
            if value.size == 0:
                # Placeholder update between frames — ignore, keep the last good frame.
                return
            rgb = ntndarray_to_rgb(value, self._bit_depth)
        except Exception as exc:  # noqa: BLE001 — reshape/type errors
            with self._frame_lock:
                self._pva_last_error = exc
            if not self._logged_first_pva:
                _diag(f"PVA reshape failed: {exc!r}")
                _diag(traceback.format_exc())
            return

        if not self._logged_first_pva:
            _diag(f"PVA first frame: shape={value.shape}, dtype={value.dtype}, "
                  f"attrib={dict(getattr(value, 'attrib', {}))}")
            self._logged_first_pva = True

        with self._frame_lock:
            self._latest_rgb = rgb
            self._latest_stamp = time.monotonic()
            self._pva_last_error = None

    def _grab_pva(self) -> np.ndarray | None:
        """Return the most recent frame delivered by the background monitor (non-blocking).

        This never does I/O: it just hands back the cached RGB the monitor thread produced, so it
        cannot stall the Bokeh IOLoop / WebSocket. Returns ``None`` if no frame has arrived yet
        (or the PV is disconnected), which the caller renders as the "no signal" placeholder.
        """
        with self._frame_lock:
            rgb = self._latest_rgb
            err = self._pva_last_error
        if rgb is None:
            if err is not None:
                raise RuntimeError(f"no PVA frame yet ({err!r})")
            return None
        return rgb

    # ---- display tick ---------------------------------------------------------------

    def tick(self) -> None:
        # Backpressure: if the previous frame's patch hasn't been flushed yet, drop this one
        # rather than pile another multi-MB write onto a busy IOLoop. A stuck flag (doc torn
        # down after we scheduled, callback never ran) is force-cleared after a short grace so
        # the stream can't freeze permanently.
        if self._push_pending:
            if (time.monotonic() - self._push_scheduled_at) < 2.0:
                return
            self._push_pending = False

        # For the PVA path, skip repacking when no new frame has arrived since the last push —
        # there's nothing to show and packing/pushing an identical frame just wastes the loop.
        if self._protocol == "pva":
            with self._frame_lock:
                stamp = self._latest_stamp
                have_frame = self._latest_rgb is not None
            if have_frame and stamp == self._last_pushed_stamp:
                return
        else:
            stamp = 0.0

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

        data = {"image": [packed], "x": [0], "y": [0], "dw": [width], "dh": [height]}
        self._push_frame(data, stamp)

    def _push_frame(self, data: dict, stamp: float) -> None:
        """Assign the CDS, coalescing via a next-tick callback so pushes can't stack up.

        We mark ``_push_pending`` before scheduling and clear it once the patch has actually been
        applied on the doc thread. While pending, :meth:`tick` short-circuits — so if the IOLoop
        is busy draining a large write, intermediate frames are dropped instead of queued.
        """
        def _apply() -> None:
            try:
                self.cds.data = data
                self._last_pushed_stamp = stamp
            finally:
                self._push_pending = False

        doc = self._doc()
        if doc is None:
            _apply()  # headless / tests — no doc, apply directly
            return
        self._push_pending = True
        self._push_scheduled_at = time.monotonic()
        try:
            doc.add_next_tick_callback(_apply)
        except Exception:  # noqa: BLE001 — doc torn down mid-flight
            self._push_pending = False

    @staticmethod
    def _doc():
        try:
            import panel as pn
            return pn.state.curdoc
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        """Tear down the background PVA monitor (idempotent)."""
        sub = self._pva_sub
        self._pva_sub = None
        if sub is not None:
            try:
                sub.close()
            except Exception:  # noqa: BLE001
                pass
