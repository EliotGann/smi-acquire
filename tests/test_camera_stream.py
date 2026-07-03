"""Tests for the EPICS camera → Bokeh CDS bridge (:mod:`smi_acquire.microscope.camera_stream`).

Focus areas:
  * ``ntndarray_to_rgb`` correctly maps every areaDetector ColorMode layout to (H, W, 3) and
    rejects degenerate shapes (notably the zero-length array the IOC emits between frames).
  * The PVA path is monitor-driven: frames arrive via a background callback and ``tick()`` only
    reads the cached frame — it must **never** perform a blocking ``Context.get()``.
  * Empty / error monitor events don't clobber the last good frame.
"""

from __future__ import annotations

import numpy as np
import pytest
from bokeh.models import ColumnDataSource

from smi_acquire.microscope import camera_stream as cs
from smi_acquire.microscope.camera_stream import CameraStream, ntndarray_to_rgb, to_uint8
from smi_acquire.microscope.config import EpicsConfig


# --------------------------------------------------------------------------------------------
# to_uint8 / bit-depth scaling
# --------------------------------------------------------------------------------------------


def test_to_uint8_passthrough_for_uint8():
    a = np.array([[0, 127, 255]], dtype=np.uint8)
    out = to_uint8(a)
    assert out.dtype == np.uint8
    assert np.array_equal(out, a)


def test_to_uint8_12bit_shifts_by_4():
    # 12-bit sensor stored in uint16 (the HEX top-down camera). Values must be >> 4.
    a = np.array([[70, 1870, 4095, 0]], dtype=np.uint16)
    out = to_uint8(a, bit_depth=12)
    assert out.dtype == np.uint8
    assert out.tolist() == [[70 >> 4, 1870 >> 4, 255, 0]]  # 4, 116, 255, 0


def test_to_uint8_infers_16bit_from_dtype_when_unspecified():
    a = np.array([[1870]], dtype=np.uint16)
    out = to_uint8(a)  # bit_depth=None → infer 16 → >> 8
    assert int(out[0, 0]) == 1870 >> 8  # 7 (dark, hence why 12-bit cams must set bit_depth)


def test_ntndarray_to_rgb_mono_16bit_scaled():
    mono16 = np.array([[70, 1870], [4095, 0]], dtype=np.uint16)
    out = ntndarray_to_rgb(mono16, bit_depth=12)
    assert out.shape == (2, 2, 3) and out.dtype == np.uint8
    # bit_depth=12 → 1870 >> 4 == 116 in every channel.
    assert out[0, 1].tolist() == [116, 116, 116]


# --------------------------------------------------------------------------------------------
# ntndarray_to_rgb
# --------------------------------------------------------------------------------------------


def test_ntndarray_to_rgb_hwc_passthrough():
    arr = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)  # (H, W, 3) RGB1
    out = ntndarray_to_rgb(arr)
    assert out.shape == (2, 3, 3)
    assert np.array_equal(out, arr)


def test_ntndarray_to_rgb_planar_chw():
    # (3, H, W) RGB3 (plane interleave) → (H, W, 3)
    planar = np.zeros((3, 2, 4), dtype=np.uint8)
    planar[0] = 10  # R plane
    planar[1] = 20  # G plane
    planar[2] = 30  # B plane
    out = ntndarray_to_rgb(planar)
    assert out.shape == (2, 4, 3)
    assert (out[..., 0] == 10).all() and (out[..., 1] == 20).all() and (out[..., 2] == 30).all()


def test_ntndarray_to_rgb_row_interleaved_h3w():
    # (H, 3, W) RGB2 (row interleave) → (H, W, 3)
    rowint = np.zeros((2, 3, 5), dtype=np.uint8)
    rowint[:, 0, :] = 1
    rowint[:, 1, :] = 2
    rowint[:, 2, :] = 3
    out = ntndarray_to_rgb(rowint)
    assert out.shape == (2, 5, 3)
    assert (out[..., 0] == 1).all() and (out[..., 1] == 2).all() and (out[..., 2] == 3).all()


def test_ntndarray_to_rgb_mono_replicated():
    mono = np.array([[5, 6], [7, 8]], dtype=np.uint8)  # (H, W)
    out = ntndarray_to_rgb(mono)
    assert out.shape == (2, 2, 3)
    # Each channel equals the mono plane.
    for c in range(3):
        assert np.array_equal(out[..., c], mono)


def test_ntndarray_to_rgb_rejects_empty():
    with pytest.raises(ValueError):
        ntndarray_to_rgb(np.zeros((0,), dtype=np.uint8))


def test_ntndarray_to_rgb_rejects_weird_shape():
    # ndim==2 is always treated as mono; a 3-D array whose axes are none of {first,last}==3
    # and not (H,3,W) has no valid interpretation and must raise.
    with pytest.raises(ValueError):
        ntndarray_to_rgb(np.zeros((2, 4, 5), dtype=np.uint8))
    with pytest.raises(ValueError):
        ntndarray_to_rgb(np.zeros((5, 5, 5, 5), dtype=np.uint8))  # ndim 4 unsupported


# --------------------------------------------------------------------------------------------
# Fake p4p Context to exercise the monitor-driven PVA path without EPICS
# --------------------------------------------------------------------------------------------


class _FakeSubscription:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeContext:
    """Stand-in for ``p4p.client.thread.Context``.

    Captures the monitor callback so the test can push frames through it, and records whether
    the blocking ``get`` was ever called (it must not be on the display path).
    """

    instances: list["_FakeContext"] = []

    def __init__(self, provider):
        self.provider = provider
        self.cb = None
        self.sub = _FakeSubscription()
        self.get_calls = 0
        _FakeContext.instances.append(self)

    def monitor(self, name, cb, request=None, notify_disconnect=False, queue=None):
        self.name = name
        self.cb = cb
        self.notify_disconnect = notify_disconnect
        return self.sub

    def get(self, *a, **k):  # pragma: no cover - must never be hit by the display path
        self.get_calls += 1
        raise AssertionError("blocking Context.get must not be called on the display path")


@pytest.fixture()
def fake_p4p(monkeypatch):
    """Patch the p4p Context that CameraStream imports lazily."""
    import p4p.client.thread as p4pt

    _FakeContext.instances = []
    monkeypatch.setattr(p4pt, "Context", _FakeContext)
    return _FakeContext


def _pva_cfg():
    return EpicsConfig(
        camera_prefix="TEST:",
        image_protocol="pva",
        image_pv="Pva1:Image",
        motors={"x": "TEST:x", "y": "TEST:y", "z": "TEST:z"},
    )


def _make_pva_stream():
    cds = ColumnDataSource(data={"image": [], "x": [], "y": [], "dw": [], "dh": []})
    stream = CameraStream(camera=None, cds=cds, fallback_size=(64, 48),
                          epics_cfg=_pva_cfg(), display_max_dim=1280)
    return stream, cds


def test_pva_stream_applies_config_bit_depth(fake_p4p):
    cds = ColumnDataSource(data={"image": [], "x": [], "y": [], "dw": [], "dh": []})
    cfg = EpicsConfig(camera_prefix="TEST:", image_protocol="pva", image_pv="Pva1:Image",
                      bit_depth=12, motors={"x": "TEST:x", "y": "TEST:y", "z": "TEST:z"})
    stream = CameraStream(camera=None, cds=cds, fallback_size=(64, 48),
                          epics_cfg=cfg, display_max_dim=1280)
    ctx = fake_p4p.instances[-1]
    # A 12-bit mono frame in uint16, like the top-down HEX camera.
    ctx.cb(np.full((8, 8), 1870, dtype=np.uint16))
    rgb = stream.grab_rgb()
    assert rgb is not None and rgb.dtype == np.uint8
    assert (rgb == (1870 >> 4)).all()  # 116, scaled per bit_depth=12


def test_pva_monitor_subscribes_and_ticks_without_get(fake_p4p):
    stream, cds = _make_pva_stream()
    ctx = fake_p4p.instances[-1]
    assert ctx.name == "TEST:Pva1:Image"
    assert ctx.notify_disconnect is True
    assert callable(ctx.cb)

    # Before any frame: grab_rgb yields nothing, tick renders the placeholder (no crash).
    assert stream.grab_rgb() is None
    stream.tick()
    assert cds.data["dw"][0] == 64 and cds.data["dh"][0] == 48

    # Push a frame through the monitor callback (simulating p4p's worker thread).
    frame = np.full((48, 64, 3), 7, dtype=np.uint8)
    ctx.cb(frame)

    rgb = stream.grab_rgb()
    assert rgb is not None and rgb.shape == (48, 64, 3)
    assert (rgb == 7).all()

    stream.tick()
    assert cds.data["dw"][0] == 64 and cds.data["dh"][0] == 48
    assert stream.frame_stamp() > 0.0
    # The whole display path must have avoided a blocking get.
    assert ctx.get_calls == 0


def test_pva_empty_and_error_events_keep_last_good_frame(fake_p4p):
    stream, _ = _make_pva_stream()
    ctx = fake_p4p.instances[-1]

    good = np.full((10, 12, 3), 42, dtype=np.uint8)
    ctx.cb(good)
    assert (stream.grab_rgb() == 42).all()
    stamp_after_good = stream.frame_stamp()

    # A zero-length update (IOC placeholder between acquisitions) must be ignored.
    ctx.cb(np.zeros((0,), dtype=np.uint8))
    assert (stream.grab_rgb() == 42).all()
    assert stream.frame_stamp() == stamp_after_good  # not advanced by the empty frame

    # A disconnect/error event must also not clobber the cached frame.
    ctx.cb(RuntimeError("disconnected"))
    assert (stream.grab_rgb() == 42).all()


def test_pva_close_is_idempotent_and_closes_subscription(fake_p4p):
    stream, _ = _make_pva_stream()
    ctx = fake_p4p.instances[-1]
    assert ctx.sub.closed is False
    stream.close()
    assert ctx.sub.closed is True
    # Second close is a no-op.
    stream.close()


def test_pva_frame_stamp_advances_per_frame(fake_p4p):
    stream, _ = _make_pva_stream()
    ctx = fake_p4p.instances[-1]
    assert stream.frame_stamp() == 0.0
    ctx.cb(np.full((4, 4, 3), 1, dtype=np.uint8))
    s1 = stream.frame_stamp()
    ctx.cb(np.full((4, 4, 3), 2, dtype=np.uint8))
    s2 = stream.frame_stamp()
    assert s2 >= s1 > 0.0


def test_pva_tick_skips_unchanged_frame(fake_p4p):
    stream, cds = _make_pva_stream()
    ctx = fake_p4p.instances[-1]
    ctx.cb(np.full((5, 5, 3), 3, dtype=np.uint8))
    stream.tick()
    first = cds.data["image"][0]
    # No new monitor frame → tick must not repack/reassign the CDS.
    stream.tick()
    assert cds.data["image"][0] is first  # same object, not re-pushed

    # A new frame arrives → tick pushes again.
    ctx.cb(np.full((5, 5, 3), 9, dtype=np.uint8))
    stream.tick()
    assert cds.data["image"][0] is not first


class _FakeDoc:
    """Captures next-tick callbacks so a test can control when the CDS patch is 'flushed'."""

    def __init__(self):
        self.queued = []

    def add_next_tick_callback(self, cb):
        self.queued.append(cb)

    def flush(self):
        cbs, self.queued = self.queued, []
        for cb in cbs:
            cb()


def test_backpressure_drops_frames_while_push_pending(fake_p4p, monkeypatch):
    stream, cds = _make_pva_stream()
    ctx = fake_p4p.instances[-1]
    doc = _FakeDoc()
    # Force the "there is a live document" path so pushes go through next-tick.
    monkeypatch.setattr(CameraStream, "_doc", staticmethod(lambda: doc))

    ctx.cb(np.full((6, 6, 3), 1, dtype=np.uint8))
    stream.tick()
    assert stream._push_pending is True
    assert len(doc.queued) == 1  # one patch scheduled, not yet flushed

    # More frames arrive while the previous push is still pending → they must be dropped,
    # not queued on top of the busy loop.
    ctx.cb(np.full((6, 6, 3), 2, dtype=np.uint8))
    stream.tick()
    ctx.cb(np.full((6, 6, 3), 3, dtype=np.uint8))
    stream.tick()
    assert len(doc.queued) == 1  # still only the single coalesced push

    # Flush: the pending patch applies and the guard clears, so the next frame can push again.
    doc.flush()
    assert stream._push_pending is False
    ctx.cb(np.full((6, 6, 3), 4, dtype=np.uint8))
    stream.tick()
    assert len(doc.queued) == 1
    doc.flush()
    assert (cds.data["image"][0] is not None)

