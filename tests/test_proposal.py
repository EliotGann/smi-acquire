"""
Tests for the read-only proposal reader (:mod:`smi_acquire.proposal`).

The GUI never sets the proposal (it's a session/queue fact set in the beamline IPython session);
it only displays it best-effort from a shared redis key.  These tests drive the parser with a
fake redis client (no real redis), and confirm graceful degradation when unconfigured/down.
"""

from __future__ import annotations

import json

from smi_acquire.proposal import Proposal


class _FakeRedis:
    def __init__(self, value):
        self._value = value

    def get(self, key):
        return self._value


def _reader(stored, *, key="re_md"):
    """A Proposal wired to a fake client returning ``stored`` (a JSON string or None)."""
    return Proposal(_FakeRedis(stored), key=key)


def test_unconfigured_reader_is_disabled():
    """No key configured -> disabled, current() is None, never raises."""
    p = Proposal.from_redis(key="")
    assert p.configured is False
    assert p.current() is None


def test_reads_data_session_field():
    p = _reader(json.dumps({"data_session": "pass-311234", "scan_id": 5}))
    assert p.current() == "pass-311234"


def test_field_priority_prefers_data_session():
    p = _reader(json.dumps({"proposal_id": "311234", "data_session": "pass-311234"}))
    assert p.current() == "pass-311234"      # data_session wins


def test_falls_back_to_proposal_id():
    p = _reader(json.dumps({"proposal_id": "311234"}))
    assert p.current() == "311234"


def test_absent_key_returns_none():
    p = _reader(None)
    assert p.current() is None


def test_malformed_json_returns_none_not_raise():
    p = _reader("not json{{")
    assert p.current() is None


def test_non_dict_value_returns_none():
    p = _reader(json.dumps([1, 2, 3]))
    assert p.current() is None


def test_no_proposal_fields_returns_none():
    p = _reader(json.dumps({"scan_id": 7, "something": "else"}))
    assert p.current() is None
