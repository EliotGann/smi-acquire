from __future__ import annotations

import numpy as np

from smi_acquire.alignment_pf import LatestPFProvider, PF_CONFIG_KEY, pf_payload_panel
from smi_acquire.config_store import AcquireConfigStore
from smi_acquire.microscope.calibration import CalibrationModel
from smi_acquire.microscope.config import AppConfig, EpicsConfig
from smi_acquire.microscope.modes.calibrate import CalibrateMode
from smi_acquire.microscope.overlays import BeamOverlay
from smi_acquire.microscope.widgets.beam_panel import BeamPanel


def _cfg() -> AppConfig:
    return AppConfig(epics=EpicsConfig(camera_prefix="SIM:", motors={"x": "x", "y": "y", "z": "z"}))


def test_config_store_defaults_to_operational_db3():
    store = AcquireConfigStore.connect(offline=True)
    assert not store.live
    assert store.location == "offline (in-memory)"
    store.put("microscope.beam", {"width_px": 42.0})
    assert store.get("microscope.beam") == {"width_px": 42.0}


def test_offline_env_does_not_touch_config_store_redis(monkeypatch):
    monkeypatch.setenv("SMI_ACQUIRE_OFFLINE", "1")
    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "redis":
            raise AssertionError("redis touched")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    store = AcquireConfigStore.connect()
    assert not store.live


def test_beam_panel_loads_and_saves_store_values():
    cfg = _cfg()
    overlay = BeamOverlay(center_px=cfg.beam.center_px, width_px=cfg.beam.width_px,
                          height_px=cfg.beam.height_px)
    store = AcquireConfigStore.connect(offline=True)
    panel = BeamPanel(cfg, overlay, config_store=store)

    panel._cx.value = 111.0
    panel._cy.value = 222.0
    panel._w.value = 33.0
    panel._h.value = 44.0
    panel._on_save_store(None)
    assert store.get("microscope.beam") == {
        "center_px": [111.0, 222.0], "width_px": 33.0, "height_px": 44.0,
    }

    store.put("microscope.beam", {"center_px": [10.0, 20.0], "width_px": 5.0, "height_px": 6.0})
    panel._on_load_store(None)
    assert cfg.beam.center_px == (10.0, 20.0)
    assert cfg.beam.width_px == 5.0
    assert cfg.beam.height_px == 6.0


def test_calibrate_mode_loads_and_saves_store_matrix():
    cfg = _cfg()
    store = AcquireConfigStore.connect(offline=True)
    cal = CalibrationModel([[1.0, 0.0], [0.0, 1.0]])
    mode = CalibrateMode(
        None, None, None, cal, cfg, lambda: (640, 480), None, config_store=store,
    )

    cal.update_matrix([[2.0, 0.0], [0.0, 3.0]])
    mode._on_save_store(None)
    assert store.get("microscope.calibration.piezo") == {"matrix": [[2.0, 0.0], [0.0, 3.0]]}

    store.put("microscope.calibration.piezo", {"matrix": [[4.0, 1.0], [2.0, 5.0]]})
    mode._on_load_store(None)
    assert np.allclose(cal.matrix, [[4.0, 1.0], [2.0, 5.0]])
    assert cfg.calibration.matrix == [[4.0, 1.0], [2.0, 5.0]]


def test_latest_pf_provider_reads_config_store_key():
    store = AcquireConfigStore.connect(offline=True)
    store.put(PF_CONFIG_KEY, {
        "version": 1,
        "updated": "2026-07-03T00:00:00Z",
        "result": {
            "scan_id": 5,
            "motor": "piezo_x",
            "detector": "pil2M",
            "x": [0, 1, 2],
            "y": [1, 3, 2],
            "yerr": [0, 0, 0],
            "fit_x": [0, 1, 2],
            "fit_y": [1, 2.8, 2],
            "cen": 1.0,
            "fwhm": 0.5,
            "peak": 1.0,
            "com": 1.1,
            "cen_base": 1.2,
            "fw_base": 0.6,
            "baseline_noise": 0.01,
            "baseline_threshold": 0.03,
            "r_squared": 0.99,
        },
    })

    view = LatestPFProvider(store)()

    assert len(view) == 3
    assert "scan" in view[2].object
    assert "cen_base" in view[2].object
    assert "baseline_threshold" in view[2].object


def test_latest_pf_provider_fingerprint_changes_with_payload():
    store = AcquireConfigStore.connect(offline=True)
    provider = LatestPFProvider(store)

    assert provider.fingerprint() is None

    store.put(PF_CONFIG_KEY, {"result": {"scan_id": 1, "x": [0], "y": [1]}})
    first = provider.fingerprint()
    store.put(PF_CONFIG_KEY, {"result": {"scan_id": 2, "x": [0], "y": [2]}})

    assert provider.fingerprint() != first


def test_pf_payload_panel_handles_missing_payload():
    pane = pf_payload_panel(None)

    assert "No published `pf` result" in pane.object


def test_pf_payload_panel_offers_move_for_controlled_motor():
    class Motor:
        position = 0.0

        def set(self, value):
            self.position = float(value)

    class Stage:
        def __init__(self):
            self.motor = Motor()

        def all_axes(self):
            return {"piezo_x": self.motor}

    view = pf_payload_panel({
        "result": {
            "motor": "piezo_x",
            "x": [0, 1, 2],
            "y": [1, 3, 2],
            "cen": 1.0,
            "com": 1.1,
            "peak": 1.0,
        }
    }, stage=Stage())

    controls = view[1]

    assert "current `piezo_x`" in controls[1].value
    assert "purple" in controls[1].value


def test_pf_current_position_marker_updates():
    class Motor:
        position = 0.0

    class Stage:
        def __init__(self):
            self.motor = Motor()

        def all_axes(self):
            return {"piezo_x": self.motor}

    stage = Stage()
    view = pf_payload_panel({
        "result": {
            "motor": "piezo_x",
            "x": [0, 1, 2],
            "y": [1, 3, 2],
        }
    }, stage=stage)
    controls = view[1]._pf_move_controls

    stage.motor.position = 1.25
    controls.update_current_position()

    assert "1.25" in view[1][1].value
