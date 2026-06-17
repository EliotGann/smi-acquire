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


class _AxisGroup:
    """A bare namespace of EpicsMotors keyed by short axis name (built from a PV dict)."""

    def __init__(self, motors: dict):
        self._motors = motors
        for axis, m in motors.items():
            setattr(self, axis, m)

    def __iter__(self):
        return iter(self._motors.items())

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        for m in self._motors.values():
            m.wait_for_connection(timeout=timeout)


class SampleStage(Device):
    """The stacked SMI sample stage.

    Exposes the **primary click-to-move axes** as ``.x/.y/.z`` (built from ``epics.motors`` — by
    the SMI stack these are normally the piezo fine axes), and, when configured, the **full
    stack** so a captured position records every axis:

    * ``.piezo`` — the SmarAct fine stage (``x/y/z/th/chi``), from ``epics.piezo_motors``.
    * ``.huber`` — the Huber coarse stage (``x/y/z/theta/chi/phi``), from ``epics.stage_motors``.

    :meth:`all_axes` returns ``{position_field: motor}`` over every connected axis using the
    ``smi_plans.Position`` field names (``piezo_x`` … ``stage_phi``), which is what the app reads
    to capture a complete position.
    """

    @classmethod
    def from_config(cls, epics_cfg: EpicsConfig, name: str = "stage") -> "SampleStage":
        # Primary x/y/z (the click-to-move axes every microscope mode uses).
        primary = {
            axis: EpicsMotor(pv, name=f"{name}_{axis}")
            for axis, pv in epics_cfg.motors.items()
        }
        obj = cls.__new__(cls)
        obj.name = name
        obj.x = primary["x"]
        obj.y = primary["y"]
        obj.z = primary["z"]

        # Full stacked axes (optional). Reuse the primary EpicsMotor objects where the PV string
        # matches, so we don't open two CA channels to the same record.
        by_pv = {pv: m for (axis, pv), m in zip(epics_cfg.motors.items(), primary.values())}

        def _build(axes_cfg: dict, prefix: str):
            out = {}
            for axis, pv in axes_cfg.items():
                out[axis] = by_pv.get(pv) or EpicsMotor(pv, name=f"{name}_{prefix}_{axis}")
            return out

        piezo_motors = _build(epics_cfg.piezo_motors, "piezo")
        huber_motors = _build(epics_cfg.stage_motors, "huber")
        obj.piezo = _AxisGroup(piezo_motors) if piezo_motors else None
        obj.huber = _AxisGroup(huber_motors) if huber_motors else None
        obj._piezo_motors = piezo_motors
        obj._huber_motors = huber_motors
        return obj

    def all_axes(self) -> dict:
        """``{Position-field: EpicsMotor}`` over every configured axis (piezo_* + stage_*).

        Falls back to the primary x/y/z (mapped to ``piezo_*``) when no full stack is configured.
        """
        out = {}
        for axis, m in getattr(self, "_piezo_motors", {}).items():
            out[f"piezo_{axis}"] = m
        for axis, m in getattr(self, "_huber_motors", {}).items():
            out[f"stage_{axis}"] = m
        if not out:                       # minimal config: only primary x/y/z
            out = {"piezo_x": self.x, "piezo_y": self.y, "piezo_z": self.z}
        return out

    def read_all_axes(self) -> dict:
        """``{Position-field: position}`` for every configured axis that reads back."""
        readings = {}
        for field, m in self.all_axes().items():
            try:
                readings[field] = round(float(m.position), 4)
            except Exception:
                pass
        return readings

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        for m in (self.x, self.y, self.z):
            m.wait_for_connection(timeout=timeout)
