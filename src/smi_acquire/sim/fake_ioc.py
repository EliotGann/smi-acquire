"""Hardware-free caproto IOC for developing the on-axis microscope app.

Publishes:
  - SWAXS:SIM:cam1:ArraySize0_RBV  (uint, 3)
  - SWAXS:SIM:cam1:ArraySize1_RBV  (uint, width)
  - SWAXS:SIM:cam1:ArraySize2_RBV  (uint, height)
  - SWAXS:SIM:cam1:ColorMode_RBV   (string, "RGB3")
  - SWAXS:SIM:image1:ArrayData     (uint8 buffer, drifting pattern)
  - the stacked sample stage as fake motor records (SMI geometry):
      piezo (fine, top):  SWAXS:SIM:pzX / pzY / pzZ / pzTH / pzCHI
      Huber (coarse):     SWAXS:SIM:hX / hY / hZ / hTHETA / hCHI / hPHI

The image is a camera-sized viewport into a deterministic virtual sample field roughly 100 camera
frames wide by 10 frames high.  The field has tile/grid landmarks, slow global contrast changes,
and many local speckle-like features, so motor motion looks like panning across a real mounted
field instead of sliding over a small repeating checkerboard.  The field is generated as cached
640x480 tiles; each frame composes the few tiles visible in the viewport, keeping the CA server
responsive without allocating the full virtual field.  (The rotation axes do NOT yet rotate the
synthetic image — they exist so the full axis state can be captured and moved.)

Startup note: each ``FakeMotor`` initializes its record fields (``.VAL``/``.RBV``/…) on an async
startup task, so with this many motors the IOC takes ~15–25 s before *all* fields answer CA
searches.  The app tolerates this (it retries the connection on tab-open / periodic callback);
a one-shot client should wait accordingly.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from caproto import ChannelType
from caproto.ioc_examples.fake_motor_record import FakeMotor
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

WIDTH = 640
HEIGHT = 480
DEPTH = 3
FRAME_RATE_HZ = 10
FIELD_TILES_X = 100
FIELD_TILES_Y = 10
FIELD_WIDTH = WIDTH * FIELD_TILES_X
FIELD_HEIGHT = HEIGHT * FIELD_TILES_Y
# Map the fake motor limits (-50..50) across the virtual field.  This makes the whole travel range
# cover the full synthetic sample while keeping the displayed viewport camera-sized.
FIELD_X_PX_PER_MM = (FIELD_WIDTH - WIDTH) / 100.0
FIELD_Y_PX_PER_MM = (FIELD_HEIGHT - HEIGHT) / 100.0


def _hash01(i: int | np.ndarray, j: int | np.ndarray, salt: int = 0) -> float | np.ndarray:
    """Deterministic 0..1 pseudo-random value for integer lattice coordinates."""
    mask = np.uint64(0xFFFFFFFF)
    n = (np.asarray(i, dtype=np.uint64) * np.uint64(374761393)) & mask
    n ^= (np.asarray(j, dtype=np.uint64) * np.uint64(668265263)) & mask
    n ^= (np.uint64(salt) * np.uint64(2246822519)) & mask
    n = ((n ^ (n >> np.uint64(13))) * np.uint64(1274126177)) & mask
    n = (n ^ (n >> np.uint64(16))) & mask
    out = n.astype(np.float32) / np.float32(0xFFFFFFFF)
    if out.shape == ():
        return float(out)
    return out


@lru_cache(maxsize=64)
def _render_tile(tile_x_i: int, tile_y_i: int) -> np.ndarray:
    """Render one deterministic 640x480 tile of the virtual sample field."""
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.float32)
    gx = xx + tile_x_i * WIDTH
    gy = yy + tile_y_i * HEIGHT
    tile_hash = _hash01(tile_x_i, tile_y_i, 1)

    r = 72 + 35 * np.sin(gx / 5200.0) + 22 * np.cos((gx + gy) / 2400.0)
    g = 78 + 32 * np.sin(gy / 1500.0 + 0.8) + 18 * np.cos(gx / 3400.0)
    b = 82 + 30 * np.cos((gx - 2 * gy) / 3100.0) + 16 * np.sin(gy / 800.0)
    r += 55 * (tile_hash - 0.5)
    g += 45 * (_hash01(tile_x_i, tile_y_i, 2) - 0.5)
    b += 40 * (_hash01(tile_x_i, tile_y_i, 3) - 0.5)

    edge_dist = np.minimum.reduce([xx, WIDTH - xx, yy, HEIGHT - yy])
    minor_x = np.minimum(xx % 160, 160 - (xx % 160))
    minor_y = np.minimum(yy % 120, 120 - (yy % 120))
    major = edge_dist < 4
    minor = (minor_x < 1.5) | (minor_y < 1.5)
    r = np.where(major, 225, r)
    g = np.where(major, 210, g)
    b = np.where(major, 80, b)
    r = np.where(minor & ~major, r * 0.62, r)
    g = np.where(minor & ~major, g * 0.62, g)
    b = np.where(minor & ~major, b * 0.62, b)

    fid = ((xx - 42) ** 2 + (yy - 42) ** 2) < (10 + 10 * tile_hash) ** 2
    stripe = (xx < 18 + 30 * _hash01(tile_x_i, tile_y_i, 4)) & (yy < 75)
    r = np.where(fid | stripe, 245, r)
    g = np.where(fid, 40 + 180 * _hash01(tile_x_i, tile_y_i, 5), np.where(stripe, 120, g))
    b = np.where(fid | stripe, 35, b)

    feat = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    cell = 96.0
    ix0 = int(np.floor(tile_x_i * WIDTH / cell)) - 1
    ix1 = int(np.ceil((tile_x_i + 1) * WIDTH / cell)) + 1
    iy0 = int(np.floor(tile_y_i * HEIGHT / cell)) - 1
    iy1 = int(np.ceil((tile_y_i + 1) * HEIGHT / cell)) + 1
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            cx = (ix + 0.15 + 0.7 * _hash01(ix, iy, 11)) * cell - tile_x_i * WIDTH
            cy = (iy + 0.15 + 0.7 * _hash01(ix, iy, 12)) * cell - tile_y_i * HEIGHT
            amp = 65 + 160 * _hash01(ix, iy, 13)
            sigma = 3.0 + 13.0 * _hash01(ix, iy, 14)
            radius = max(16, int(4 * sigma))
            if cx + radius < 0 or cx - radius >= WIDTH or cy + radius < 0 or cy - radius >= HEIGHT:
                continue
            x0 = max(0, int(cx - radius))
            x1 = min(WIDTH, int(cx + radius + 1))
            y0 = max(0, int(cy - radius))
            y1 = min(HEIGHT, int(cy + radius + 1))
            spot = np.exp(-(((xx[y0:y1, x0:x1] - cx) ** 2
                             + (yy[y0:y1, x0:x1] - cy) ** 2) / (2 * sigma ** 2))) * amp
            feat[y0:y1, x0:x1] += spot.astype(np.float32)

    r = np.clip(r + feat, 0, 255).astype(np.uint8)
    g = np.clip(g + 0.65 * feat, 0, 255).astype(np.uint8)
    b = np.clip(b + 0.35 * feat, 0, 255).astype(np.uint8)
    return np.stack([r, g, b], axis=-1)


def _slice_virtual_field(x0: int, y0: int) -> np.ndarray:
    """Compose a camera viewport from cached virtual-field tiles."""
    out = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    x_end = x0 + WIDTH
    y_end = y0 + HEIGHT
    for ty in range(y0 // HEIGHT, (y_end - 1) // HEIGHT + 1):
        for tx in range(x0 // WIDTH, (x_end - 1) // WIDTH + 1):
            tile = _render_tile(tx, ty)
            src_x0 = max(0, x0 - tx * WIDTH)
            src_y0 = max(0, y0 - ty * HEIGHT)
            src_x1 = min(WIDTH, x_end - tx * WIDTH)
            src_y1 = min(HEIGHT, y_end - ty * HEIGHT)
            dst_x0 = tx * WIDTH + src_x0 - x0
            dst_y0 = ty * HEIGHT + src_y0 - y0
            out[dst_y0:dst_y0 + (src_y1 - src_y0), dst_x0:dst_x0 + (src_x1 - src_x0)] = (
                tile[src_y0:src_y1, src_x0:src_x1]
            )
    return out


def _render_frame(t: float, mx: float, my: float, mz: float) -> np.ndarray:
    """Synthetic RGB frame: viewport into a large deterministic virtual sample field."""
    # Motor position controls which portion of the large field is visible. Positive motor motion
    # pans the sample under the fixed camera.
    origin_x = (FIELD_WIDTH - WIDTH) / 2.0 - mx * FIELD_X_PX_PER_MM
    origin_y = (FIELD_HEIGHT - HEIGHT) / 2.0 - my * FIELD_Y_PX_PER_MM
    origin_x = np.clip(origin_x, 0, FIELD_WIDTH - WIDTH)
    origin_y = np.clip(origin_y, 0, FIELD_HEIGHT - HEIGHT)
    x0 = int(round(origin_x))
    y0 = int(round(origin_y))
    rgb = _slice_virtual_field(x0, y0)

    # Add a z-dependent defocus wash so the focus tab has a visibly changing metric.
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.float32)
    cx_view, cy_view = WIDTH / 2, HEIGHT / 2
    rr = np.hypot(xx - cx_view, yy - cy_view)
    focus_sigma = 35.0 + 180.0 * abs(mz)
    halo = np.exp(-(rr**2) / (2 * focus_sigma**2))[..., None] * np.array([45.0, 22.0, 11.0])
    return np.clip(rgb.astype(np.float32) + halo, 0, 255).astype(np.uint8)


class CamGroup(PVGroup):
    color_mode = pvproperty(
        value="RGB1",   # data layout we publish is pixel-interleaved (R,G,B per pixel)
        name="ColorMode_RBV",
        dtype=ChannelType.STRING,
        read_only=True,
    )
    # Camera-native sizes; preserved for clients that read them off the cam plugin.
    array_size_x = pvproperty(value=WIDTH, name="ArraySizeX_RBV", dtype=ChannelType.LONG, read_only=True)
    array_size_y = pvproperty(value=HEIGHT, name="ArraySizeY_RBV", dtype=ChannelType.LONG, read_only=True)
    array_size_z = pvproperty(value=DEPTH, name="ArraySizeZ_RBV", dtype=ChannelType.LONG, read_only=True)
    # Exposure (setpoint + RBV). AreaDetector standard.
    acquire_time = pvproperty(value=0.05, name="AcquireTime", dtype=ChannelType.DOUBLE)
    acquire_time_rbv = pvproperty(value=0.05, name="AcquireTime_RBV", dtype=ChannelType.DOUBLE, read_only=True)

    @acquire_time.putter
    async def acquire_time(self, instance, value):
        # Echo the setpoint into the RBV so clients reading AcquireTime_RBV see the change.
        await self.acquire_time_rbv.write(float(value))


class ImageGroup(PVGroup):
    # NDPluginBase output dimensions — index 0 is the innermost data axis (fastest in memory).
    array_size0 = pvproperty(value=DEPTH, name="ArraySize0_RBV", dtype=ChannelType.LONG, read_only=True)
    array_size1 = pvproperty(value=WIDTH, name="ArraySize1_RBV", dtype=ChannelType.LONG, read_only=True)
    array_size2 = pvproperty(value=HEIGHT, name="ArraySize2_RBV", dtype=ChannelType.LONG, read_only=True)
    array_data = pvproperty(
        value=[0] * (WIDTH * HEIGHT * DEPTH),
        name="ArrayData",
        dtype=ChannelType.CHAR,
        max_length=WIDTH * HEIGHT * DEPTH,
        read_only=True,
    )

    @array_data.startup
    async def array_data(self, instance, async_lib):
        # ``parent`` is the SwaxsSimIOC instance. caproto's FakeMotor stores the motor record
        # readback under ``.motor.field_inst.user_readback_value`` (the .RBV field). The image
        # tracks the PIEZO x/y/z (the fine top stage the click-to-move drives).
        parent = self.parent
        t0 = 0.0
        while True:
            try:
                mx = float(parent.pzX.motor.field_inst.user_readback_value.value)
                my = float(parent.pzY.motor.field_inst.user_readback_value.value)
                mz = float(parent.pzZ.motor.field_inst.user_readback_value.value)
            except Exception:
                mx = my = mz = 0.0
            frame = _render_frame(t0, mx, my, mz)
            await instance.write(frame.ravel().astype(np.uint8))
            t0 += 1.0 / FRAME_RATE_HZ
            await async_lib.sleep(1.0 / FRAME_RATE_HZ)


class SwaxsSimIOC(PVGroup):
    cam = SubGroup(CamGroup, prefix="cam1:")
    image = SubGroup(ImageGroup, prefix="image1:")
    # --- piezo fine stage (top of the stack): x/y/z/th/chi ---
    pzX = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="pzX")
    pzY = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="pzY")
    pzZ = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="pzZ")
    pzTH = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-5.0, 5.0), prefix="pzTH")
    pzCHI = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-5.0, 5.0), prefix="pzCHI")
    # --- Huber coarse stage (bottom): x/y/z + rotations theta/chi/phi ---
    hX = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-100.0, 100.0), prefix="hX")
    hY = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-100.0, 100.0), prefix="hY")
    hZ = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-100.0, 100.0), prefix="hZ")
    hTHETA = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-5.0, 5.0), prefix="hTHETA")
    hCHI = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-5.0, 5.0), prefix="hCHI")
    hPHI = SubGroup(FakeMotor, velocity=5.0, precision=4, user_limits=(-90.0, 90.0), prefix="hPHI")


def main() -> None:
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="SWAXS:SIM:",
        desc="Fake on-axis microscope IOC for swaxs-beam-image dev",
    )
    ioc = SwaxsSimIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
