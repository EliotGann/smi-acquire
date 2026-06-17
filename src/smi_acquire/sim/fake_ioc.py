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
  - legacy aliases SWAXS:SIM:mtrX / mtrY / mtrZ kept pointing at the piezo x/y/z so older
    configs (motors: {x: mtrX, ...}) still resolve.

The image pattern depends on the (piezo) x/y/z motor positions so click-to-move and bookmark
projection have something meaningful to track.  (The rotation axes do NOT yet rotate the
synthetic image — the rotation-aware click math is deferred to real-hardware work; here they
exist so the full axis state can be captured and moved.)
"""

from __future__ import annotations

import numpy as np
from caproto import ChannelType
from caproto.ioc_examples.fake_motor_record import FakeMotor
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

WIDTH = 640
HEIGHT = 480
DEPTH = 3
FRAME_RATE_HZ = 10
PX_PER_MM = 100.0


def _render_frame(t: float, mx: float, my: float, mz: float) -> np.ndarray:
    """Synthetic RGB frame: drifting checkerboard whose alignment depends on motor pos."""
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.float32)
    # Tie pixel offset to motor position so moving the stage looks like moving a sample.
    offset_x = -mx * PX_PER_MM
    offset_y = -my * PX_PER_MM
    cell = 40
    u = ((xx - offset_x) // cell).astype(int)
    v = ((yy - offset_y) // cell).astype(int)
    base = ((u + v) % 2).astype(np.uint8) * 80 + 60  # 60 / 140 squares

    # Add a focus halo so z does something visible.
    cx, cy = WIDTH / 2, HEIGHT / 2
    rr = np.hypot(xx - cx, yy - cy)
    focus_sigma = 40.0 + 200.0 * abs(mz)
    halo = np.exp(-(rr**2) / (2 * focus_sigma**2)) * 60.0

    # A bright feature ~ at the origin (in motor coords), to make calibration easy. We
    # additionally render a few off-axis weaker spots so phase-cross-correlation has unique
    # peaks (the checkerboard alone is too periodic).
    feature_offsets = [
        (0.0, 0.0, 220.0, 8.0),
        (2.0, -1.5, 150.0, 5.0),   # in pixel units from the bright feature's image position
        (-3.0, 2.5, 130.0, 5.0),
    ]
    feat = np.zeros_like(base, dtype=np.float32)
    for ox_cells, oy_cells, amp, sigma in feature_offsets:
        feat_px_x = cx - mx * PX_PER_MM + ox_cells * 30
        feat_px_y = cy - my * PX_PER_MM + oy_cells * 30
        feat += np.exp(
            -(((xx - feat_px_x) ** 2 + (yy - feat_px_y) ** 2) / (2 * sigma ** 2))
        ) * amp

    r = np.clip(base + halo + feat, 0, 255).astype(np.uint8)
    g = np.clip(base + 0.5 * halo + 0.6 * feat, 0, 255).astype(np.uint8)
    b = np.clip(base * 0.7 + 0.3 * feat, 0, 255).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)  # (H, W, 3) — RGB3 layout
    return rgb


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
    # --- legacy aliases: the old configs used mtrX/Y/Z; point them at the piezo x/y/z ---
    mtrX = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="mtrX")
    mtrY = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="mtrY")
    mtrZ = SubGroup(FakeMotor, velocity=2.0, precision=4, user_limits=(-50.0, 50.0), prefix="mtrZ")


def main() -> None:
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="SWAXS:SIM:",
        desc="Fake on-axis microscope IOC for swaxs-beam-image dev",
    )
    ioc = SwaxsSimIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
