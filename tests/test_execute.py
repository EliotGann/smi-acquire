"""
Tests for the execution seam (``execute``) and the advisory RE-busy interlock (``interlock``).

Pure / no hardware: motors are tiny fakes; redis is a stub client.  Verifies the local
direct-jog backend (interlock-gated), the copy-paste submission, the queueserver stub, and the
interlock's inert-by-default / busy-detection / fail-open behavior.
"""

from __future__ import annotations

import pytest

from smi_acquire.execute import (LocalExecutor, QueueServerExecutor, Submission,
                                 InterlockedError)
from smi_acquire.interlock import Interlock


class _FakeMotor:
    def __init__(self, pos=1.0):
        self.position = pos
        self.last_set = None
        self.stopped = False

    def set(self, target):
        self.last_set = target
        return "status->{}".format(target)

    def stop(self):
        self.stopped = True
        return "stopped"


class _FakeRedis:
    def __init__(self, val=None):
        self.val = val

    def get(self, key):
        return self.val

    def ping(self):
        return True


# ---------------------------------------------------------------------------
# interlock
# ---------------------------------------------------------------------------
def test_interlock_inert_when_disabled():
    il = Interlock(None, enabled=False)
    assert il.is_busy() is False
    assert il.active is False
    assert il.banner() == ""


def test_interlock_absent_flag_is_not_busy():
    il = Interlock(_FakeRedis(None), enabled=True)
    assert il.is_busy() is False


def test_interlock_busy_from_json_object():
    il = Interlock(_FakeRedis(b'{"busy": true, "plan": "giwaxs", "scan_id": 42, "host": "ws1"}'),
                   enabled=True)
    assert il.is_busy() is True
    banner = il.banner()
    assert "giwaxs" in banner
    assert "42" in banner


def test_interlock_defaults_to_db3_and_status_key():
    il = Interlock(_FakeRedis(None), enabled=True)
    assert il.db == 3
    assert il.key == "swaxsstatus:re_busy"


def test_interlock_reads_fresh_each_call_no_cache():
    """The TTL anti-latch requires uncached reads: a flag flip is seen immediately."""
    client = _FakeRedis(b'{"busy": true}')
    il = Interlock(client, enabled=True)
    assert il.is_busy() is True
    client.val = None              # worker finished / key expired
    assert il.is_busy() is False   # seen on the very next poll (not cached)


def test_interlock_busy_from_truthy_scalar():
    il = Interlock(_FakeRedis(b"1"), enabled=True)
    assert il.is_busy() is True


def test_interlock_fail_open_on_error():
    class Boom:
        def get(self, key):
            raise RuntimeError("redis down")

    il = Interlock(Boom(), enabled=True)
    assert il.is_busy() is False     # advisory: any error -> not busy


# ---------------------------------------------------------------------------
# local executor
# ---------------------------------------------------------------------------
def test_local_jog_moves_relative():
    ex = LocalExecutor(interlock=Interlock(None, enabled=False))
    m = _FakeMotor(pos=2.0)
    ex.jog(m, 0.5)
    assert m.last_set == 2.5


def test_local_move_abs():
    ex = LocalExecutor(interlock=Interlock(None, enabled=False))
    m = _FakeMotor()
    ex.move_abs(m, 7.0)
    assert m.last_set == 7.0


def test_local_jog_blocked_when_busy():
    il = Interlock(_FakeRedis(b'{"busy": true}'), enabled=True)
    ex = LocalExecutor(interlock=il)
    with pytest.raises(InterlockedError):
        ex.jog(_FakeMotor(), 0.5)


def test_local_stop_allowed_even_when_busy():
    il = Interlock(_FakeRedis(b'{"busy": true}'), enabled=True)
    ex = LocalExecutor(interlock=il)
    m = _FakeMotor()
    ex.stop(m)
    assert m.stopped is True


def test_local_submit_returns_copy_text():
    ex = LocalExecutor(interlock=Interlock(None, enabled=False))
    sub = ex.submit("RE(acquire(...))")
    assert isinstance(sub, Submission)
    assert sub.kind == "copy" and sub.ok
    assert sub.text == "RE(acquire(...))"


# ---------------------------------------------------------------------------
# queueserver stub
# ---------------------------------------------------------------------------
def test_queueserver_submit_raises():
    with pytest.raises(NotImplementedError):
        QueueServerExecutor().submit("x")


def test_queueserver_jog_raises():
    with pytest.raises(NotImplementedError):
        QueueServerExecutor().jog(_FakeMotor(), 0.1)
