"""Ophyd devices for the microscope camera and the X/Y/Z sample stage.

The Camera follows the AreaDetector PV naming convention but is hand-built as a lightweight
``Device`` so it works against a partial simulator IOC (no need for every AD cam PV to exist).

For the **CA image path**, the cam plugin exposes ``ColorMode_RBV`` and the image plugin
exposes ``ArrayData`` + ``ArraySize{0,1,2}_RBV`` (NDPluginBase's output dimensions).

For the **PVA image path**, we still build the cam plugin for ColorMode, but the image
plugin is skipped — the NTNDArray over PVA carries dimensions + color mode inline.
"""

from __future__ import annotations

from ophyd import Component as Cpt
from ophyd import Device, EpicsMotor, EpicsSignal, EpicsSignalRO

from .config import EpicsConfig


class _CamPlugin(Device):
    color_mode = Cpt(EpicsSignalRO, "ColorMode_RBV", string=True)
    # Exposure controls (AreaDetector standard). AcquireTime is the setpoint; the IOC echoes
    # back AcquireTime_RBV after applying the change.
    acquire_time = Cpt(EpicsSignal, "AcquireTime")
    acquire_time_rbv = Cpt(EpicsSignalRO, "AcquireTime_RBV")


class _ImagePlugin(Device):
    array_data = Cpt(EpicsSignalRO, "ArrayData")
    array_size0 = Cpt(EpicsSignalRO, "ArraySize0_RBV")
    array_size1 = Cpt(EpicsSignalRO, "ArraySize1_RBV")
    array_size2 = Cpt(EpicsSignalRO, "ArraySize2_RBV")


class Camera(Device):
    """Lightweight AreaDetector-style camera composed at runtime from the config."""

    @classmethod
    def from_config(cls, epics_cfg: EpicsConfig, name: str = "camera") -> "Camera":
        prefix = epics_cfg.camera_prefix
        cam_suffix = epics_cfg.cam_suffix

        cam = _CamPlugin(prefix + cam_suffix, name=f"{name}_cam")

        # The CA image plugin is only constructed when we're using the CA protocol — under
        # PVA the structured NTNDArray carries dims + color mode inline, so the separate
        # CA size/data PVs don't need to exist on the IOC.
        image: _ImagePlugin | None = None
        if epics_cfg.image_protocol.lower() == "ca":
            # ``image_pv`` here is like "image1:ArrayData"; the plugin's PV prefix is
            # everything up to and including "image1:". Strip the trailing "ArrayData".
            ip = epics_cfg.image_pv
            if ip.endswith("ArrayData"):
                plugin_prefix = ip[: -len("ArrayData")]
            else:
                plugin_prefix = ip.rstrip("ArrayData").rstrip(":") + ":"
            image = _ImagePlugin(prefix + plugin_prefix, name=f"{name}_image")

        obj = cls.__new__(cls)
        obj.name = name
        obj.cam = cam
        obj.image = image
        return obj

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        self.cam.wait_for_connection(timeout=timeout)
        if self.image is not None:
            self.image.wait_for_connection(timeout=timeout)


class SampleStage(Device):
    """Container of three EpicsMotors. Built at runtime from configured PV strings."""

    @classmethod
    def from_config(cls, epics_cfg: EpicsConfig, name: str = "stage") -> "SampleStage":
        motors = {
            axis: EpicsMotor(pv, name=f"{name}_{axis}")
            for axis, pv in epics_cfg.motors.items()
        }
        obj = cls.__new__(cls)
        obj.name = name
        obj.x = motors["x"]
        obj.y = motors["y"]
        obj.z = motors["z"]
        return obj

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        for m in (self.x, self.y, self.z):
            m.wait_for_connection(timeout=timeout)
