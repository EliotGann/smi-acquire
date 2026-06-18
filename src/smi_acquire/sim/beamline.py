"""
smi_acquire.sim.beamline
=========================

A complete set of **simulated SMI beamline devices** (``ophyd.sim`` stand-ins) plus the global
identifiers the ``smi_plans`` plan files expect at runtime (``piezo``, ``waxs``, ``energy``,
``pil2M``, ``bps`` …).

This is vendored from ``smi-plans/tests/conftest.py::SimBeamline`` so the GUI's **dry-run
validator** can exercise a generated script without any hardware or RunEngine: build the sim,
inject its globals into the ``smi_plans`` modules, exhaust the plan, and assert the message
stream is one balanced run.

It is also the spiritual sibling of the microscope's fake caproto IOC (``smi_acquire.sim
.fake_ioc``): the IOC drives the *interactive* sample-builder over EPICS; this in-process sim
drives the *plan validation*. Neither touches real hardware.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

try:
    import bluesky.plan_stubs as bps
    import bluesky.preprocessors as bpp
    import bluesky.plans as bp
    from ophyd import Signal, Device, Component as Cpt
    from ophyd.sim import SynAxis, SynSignal, motor, Syn2DGauss
    _HAVE_BLUESKY = True
except Exception:  # pragma: no cover - off-beamline without bluesky/ophyd
    _HAVE_BLUESKY = False


if _HAVE_BLUESKY:

    class _Stack(Device):
        """SmarAct piezo fine stage: .x/.y/.z/.th."""
        x = Cpt(SynAxis, name="x")
        y = Cpt(SynAxis, name="y")
        z = Cpt(SynAxis, name="z")
        th = Cpt(SynAxis, name="th")

    class _HuberStage(Device):
        """The Huber coarse ``stage`` (STG_pseudo) as on the live beamline: lab-frame x/y/z +
        rotations theta/chi/phi, with the back-compat ``.th``/``.ph``/``.ch`` aliases the real
        device provides. ``phi`` is the rotation axis the removed ``prs`` was repointed to."""
        x = Cpt(SynAxis, name="x")
        y = Cpt(SynAxis, name="y")
        z = Cpt(SynAxis, name="z")
        theta = Cpt(SynAxis, name="theta")
        chi = Cpt(SynAxis, name="chi")
        phi = Cpt(SynAxis, name="phi")

        @property
        def th(self):
            return self.theta

        @property
        def ph(self):
            return self.phi

        @property
        def ch(self):
            return self.chi

    class _WaxsMotors(Device):
        """The WAXS detector's motion sub-device (the real ``pil900KW.motors``).

        On the beamline ``waxs = pil900KW.motors`` and ``pil900KW.motors.kind = 'normal'`` -- so
        the detector ``pil900KW`` records ``.motors``' keys (``waxs_arc``/``waxs_bsx``/
        ``waxs_bsy``), and ``waxs`` IS this same sub-device.  Reading BOTH ``pil900KW`` and
        ``waxs`` in one event would duplicate those keys (the collision ``dedup_readables``
        fixes).  The arc is moved via ``waxs.arc`` (``bps.mv(waxs.arc, angle)``), as on the
        beamline -- NOT ``bps.mv(waxs, ...)``.
        """
        arc = Cpt(SynAxis, name="waxs_arc")
        bs_x = Cpt(SynAxis, name="waxs_bsx")
        bs_y = Cpt(SynAxis, name="waxs_bsy")

    class _WaxsDetector(Device):
        """Stand-in for the SMI ``pil900KW`` WAXS area detector.

        Has a readable image stat (``stats``) AND a ``motors`` sub-device it records (kind
        normal), so it reports ``waxs_arc`` etc. -- the parent/child key overlap with ``waxs``.
        """
        stats = Cpt(SynSignal, func=lambda: 1.0, name="stats")
        motors = Cpt(_WaxsMotors, name="motors")

    class _XBPM(Device):
        sumX = Cpt(SynSignal, func=lambda: 1000.0, name="sumX")
        sumY = Cpt(SynSignal, func=lambda: 1000.0, name="sumY")

    class _PinDiode(Device):
        current2 = Cpt(SynSignal, func=lambda: 0.5, name="current2")
        averaging_time = Cpt(Signal, value=1.0, name="averaging_time")

    class _SDDpos(Device):
        z = Cpt(SynAxis, name="z")

    class _Cam(Device):
        num_images = Cpt(Signal, value=1, name="num_images")
        acquire = Cpt(Signal, value=0, name="acquire")
        acquire_time = Cpt(Signal, value=1.0, name="acquire_time")

    class _AreaDet(Device):
        cam = Cpt(_Cam, name="cam")
        stats = Cpt(SynSignal, func=lambda: 1.0, name="stats")

    class _DetMotor(Device):
        x = Cpt(SynAxis, name="x")
        y = Cpt(SynAxis, name="y")
        z = Cpt(SynAxis, name="z")

    class _Lakeshore(Device):
        input_A = Cpt(SynSignal, func=lambda: 300.0, name="input_A")
        input_A_celsius = Cpt(SynSignal, func=lambda: 27.0, name="input_A_celsius")
        ch1_read = Cpt(SynSignal, func=lambda: 27.0, name="ch1_read")
        ch1_sp = Cpt(Signal, value=300.0, name="ch1_sp")

        class _Out:
            def mv_temp(self, T):
                yield from bps.null()
        output1 = _Out()

    class _Linkam(Device):
        temperature_current = Cpt(SynSignal, func=lambda: 27.0, name="temperature_current")

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._sp = 27.0

        def setTemperature(self, T):
            self._sp = float(T)

        def on(self):
            pass

        def temperature(self):
            return self._sp

    class _Att:
        def __init__(self, name):
            self.close_cmd = SynSignal(func=lambda: 0, name=name + "_close")
            self.open_cmd = SynSignal(func=lambda: 0, name=name + "_open")


class SimBeamline:
    """Container of simulated devices + the globals to inject into ``smi_plans``."""

    def __init__(self):
        if not _HAVE_BLUESKY:
            raise RuntimeError("bluesky/ophyd not available; dry-run needs the beamline env")
        self.np = np
        self.bps = bps
        self.bpp = bpp
        self.bp = bp
        self.Signal = Signal

        self.piezo = _Stack(name="piezo")
        self.stage = _HuberStage(name="stage")
        self.energy = SynAxis(name="energy")
        self.xbpm2 = _XBPM(name="xbpm2")
        self.xbpm3 = _XBPM(name="xbpm3")
        self.pin_diode = _PinDiode(name="pin_diode")
        self.pil2M = _AreaDet(name="pil2M")
        self.pil2M.motor = _DetMotor(name="pil2M_motor")
        # WAXS detector + its motion sub-device; ``waxs`` IS ``pil900KW.motors`` (beamline wiring
        # ``waxs = pil900KW.motors``).  ``motors`` is recorded by the detector (kind normal), so
        # reading both pil900KW and waxs in one event collides on waxs_arc/_bsx/_bsy unless
        # de-duplicated -- the case smi_plans dedup_readables handles.  The arc is moved via
        # ``waxs.arc`` (NOT ``bps.mv(waxs, ...)``).
        self.pil900KW = _WaxsDetector(name="pil900KW")
        self.pil900KW.motors.kind = "normal"
        self.waxs = self.pil900KW.motors
        self.pil300KW = Syn2DGauss("pil300KW", motor, "motor", motor, "motor", center=0, Imax=1)
        self.amptek = Syn2DGauss("amptek", motor, "motor", motor, "motor", center=0, Imax=1)
        self.rayonix = Syn2DGauss("rayonix", motor, "motor", motor, "motor", center=0, Imax=1)
        self.pil2M_pos = _SDDpos(name="pil2M_pos")
        self.ls = _Lakeshore(name="ls")
        self.LThermal = _Linkam(name="LThermal")
        self.syringe_pu = SynAxis(name="syringe_pu")
        self.att2_9 = _Att("att2_9")
        self.att2_10 = _Att("att2_10")
        self.att2_11 = _Att("att2_11")
        self.att2_12 = _Att("att2_12")

        # Keep the WAXS arc up so saxs_waxs_dets() keeps pil2M (SAXS) in the list.
        # The arc is moved via ``waxs.arc`` (= pil900KW.motors.arc), as on the beamline.
        self.waxs.arc.set(20).wait()

    # -- callable globals smi_plans expects ---------------------------------
    def det_exposure_time(self, a, b=None):
        yield from bps.null()

    def alignement_gisaxs_hex(self, angle=0.1):
        yield from bps.mv(self.piezo.th, angle)

    # All the profile's top-level GISAXS alignment routines share the same call shape
    # (``align(angle)``); for dry-run validation they are the same simple stand-in so a
    # generated setup() that calls any of them resolves (see registry.ALIGNMENT_ROUTINES).
    alignement_gisaxs_doblestack = alignement_gisaxs_hex
    alignement_gisaxs_hex_short = alignement_gisaxs_hex
    alignement_gisaxs_hex_roughsample = alignement_gisaxs_hex
    alignment_gisaxs = alignement_gisaxs_hex
    alignement_gisaxs_short = alignement_gisaxs_hex
    alignement_gisaxs_rough = alignement_gisaxs_hex
    alignement_gisaxs_multisample = alignement_gisaxs_hex
    quickalign_gisaxs = alignement_gisaxs_hex
    fast_align = alignement_gisaxs_hex

    def setDryFlow(self, v):
        yield from bps.null()

    def setWetFlow(self, v):
        yield from bps.null()

    def set_humidity(self, v):
        yield from bps.null()

    def readHumidity(self):
        return 45.0

    # rig-specific axis callables a generated script may reference
    def set_potential(self, v):
        yield from bps.null()

    def set_rh(self, v):
        yield from bps.null()

    def globals_dict(self):
        return {
            "np": self.np, "bps": self.bps, "bpp": self.bpp, "bp": self.bp,
            "Signal": self.Signal,
            "piezo": self.piezo, "stage": self.stage, "waxs": self.waxs,
            "energy": self.energy, "xbpm2": self.xbpm2, "xbpm3": self.xbpm3,
            "pin_diode": self.pin_diode, "pil2M": self.pil2M, "pil900KW": self.pil900KW,
            "pil300KW": self.pil300KW, "amptek": self.amptek, "rayonix": self.rayonix,
            "pil2M_pos": self.pil2M_pos, "ls": self.ls, "LThermal": self.LThermal,
            "syringe_pu": self.syringe_pu,
            "att2_9": self.att2_9, "att2_10": self.att2_10,
            "att2_11": self.att2_11, "att2_12": self.att2_12,
            "det_exposure_time": self.det_exposure_time,
            "alignement_gisaxs_hex": self.alignement_gisaxs_hex,
            "alignement_gisaxs_doblestack": self.alignement_gisaxs_doblestack,
            "alignement_gisaxs_hex_short": self.alignement_gisaxs_hex_short,
            "alignement_gisaxs_hex_roughsample": self.alignement_gisaxs_hex_roughsample,
            "alignment_gisaxs": self.alignment_gisaxs,
            "alignement_gisaxs_short": self.alignement_gisaxs_short,
            "alignement_gisaxs_rough": self.alignement_gisaxs_rough,
            "alignement_gisaxs_multisample": self.alignement_gisaxs_multisample,
            "quickalign_gisaxs": self.quickalign_gisaxs,
            "fast_align": self.fast_align,
            "setDryFlow": self.setDryFlow, "setWetFlow": self.setWetFlow,
            "set_humidity": self.set_humidity, "readHumidity": self.readHumidity,
            "set_potential": self.set_potential, "set_rh": self.set_rh,
        }

    # -- message-stream assertions ------------------------------------------
    @staticmethod
    def run_count(msgs):
        cmds = [m.command for m in msgs]
        return cmds.count("open_run"), cmds.count("close_run")

    @staticmethod
    def events_by_stream(msgs):
        return dict(Counter(m.kwargs.get("name", "primary")
                            for m in msgs if m.command == "create"))

    @classmethod
    def primary_events(cls, msgs):
        return cls.events_by_stream(msgs).get("primary", 0)


__all__ = ["SimBeamline"]
