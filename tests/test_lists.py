"""
Tests for the named-list store boundary (:mod:`smi_acquire.lists`).

Mirrors the sample-store tests: driven OFFLINE (in-memory dict backend, no redis). Covers the
GUI-shaped CRUD the energy/incidence/temperature/time editors use — save (authoritative values +
editable spec + free md extras), load back for editing, list names for an 'open' dropdown,
overwrite-by-name, and delete — plus that a stored list resolves through ``smi_plans.resolve_list``.
"""

from __future__ import annotations

from smi_acquire.lists import AcquireListStore, KINDS
from smi_plans import resolve_list


def _store() -> AcquireListStore:
    return AcquireListStore.connect(offline=True)


def test_offline_connect_is_not_live():
    acq = _store()
    assert acq.live is False
    assert acq.list_names("energy") == []


def test_save_and_load_energy_list_roundtrips_spec_and_md():
    acq = _store()
    spec = {"boundaries": [2470.0, 2472.0, 2476.0], "steps": [1.0, 0.25]}
    md = {"flux_threshold": 50, "flux_signal": "xbpm2.sumX"}
    values = [2470.0, 2471.0, 2472.0, 2472.25, 2472.5]
    acq.save_list("Fe_K_XANES", "energy", values, spec=spec, units="eV", md=md)

    nl = acq.get_list("Fe_K_XANES", "energy")
    assert nl is not None
    assert nl.kind == "energy"
    assert nl.values == values
    assert nl.spec == spec
    assert nl.units == "eV"
    assert nl.md == md


def test_list_names_per_kind_sorted():
    acq = _store()
    acq.save_list("b_edge", "energy", [100.0])
    acq.save_list("a_edge", "energy", [200.0])
    acq.save_list("grazing_fine", "incidence", [0.1, 0.2])
    assert acq.list_names("energy") == ["a_edge", "b_edge"]
    assert acq.list_names("incidence") == ["grazing_fine"]
    assert acq.list_names("temperature") == []


def test_save_same_name_overwrites_keeps_id():
    acq = _store()
    first = acq.save_list("anneal", "temperature", [30.0, 60.0])
    second = acq.save_list("anneal", "temperature", [30.0, 60.0, 90.0],
                           md={"ramp_rate": 5.0})
    assert second.id == first.id                         # stable identity on overwrite
    assert acq.list_names("temperature") == ["anneal"]   # not duplicated
    assert acq.get_list("anneal", "temperature").values == [30.0, 60.0, 90.0]


def test_names_unique_within_kind_not_across():
    acq = _store()
    acq.save_list("fine", "incidence", [0.1, 0.2])
    acq.save_list("fine", "time", [0.5, 1.0])
    assert acq.get_list("fine", "incidence").values == [0.1, 0.2]
    assert acq.get_list("fine", "time").values == [0.5, 1.0]


def test_delete_list():
    acq = _store()
    acq.save_list("tmp", "energy", [1.0])
    assert acq.exists("tmp", "energy")
    acq.delete_list("tmp", "energy")
    assert not acq.exists("tmp", "energy")
    assert acq.get_list("tmp", "energy") is None


def test_stored_list_resolves_by_name_via_smi_plans():
    """A saved list is resolvable by the same seam the generated plan uses."""
    acq = _store()
    acq.save_list("S_K_XANES", "energy", [2470.0, 2472.0, 2474.0])
    # resolve_list(name, kind, store=<smi_plans.ListStore>) -> the values
    assert resolve_list("S_K_XANES", kind="energy", store=acq.store) == [2470.0, 2472.0, 2474.0]
    # a literal passes through untouched, no store needed
    assert resolve_list([1.0, 2.0], kind="energy") == [1.0, 2.0]


def test_kinds_constant_covers_the_four_editors():
    assert set(KINDS) == {"energy", "incidence", "temperature", "time"}


def test_incidence_list_roundtrips_range_spec():
    acq = _store()
    acq.save_list("grazing_fine", "incidence", [0.1, 0.2, 0.3, 0.4],
                  spec={"range": [0.1, 0.4, 0.1]}, units="deg")
    nl = acq.get_list("grazing_fine", "incidence")
    assert nl.kind == "incidence"
    assert nl.values == [0.1, 0.2, 0.3, 0.4]
    assert nl.spec == {"range": [0.1, 0.4, 0.1]}
    assert resolve_list("grazing_fine", kind="incidence", store=acq.store) == [0.1, 0.2, 0.3, 0.4]


def test_temperature_list_roundtrips_cycle_and_rate():
    acq = _store()
    acq.save_list("anneal_cycle", "temperature", [30.0, 60.0, 90.0, 90.0, 60.0, 30.0],
                  spec={"values": [30.0, 60.0, 90.0], "cycle": True},
                  units="C", md={"ramp_rate": 5.0, "soak": 120.0, "first_soak": 300.0})
    nl = acq.get_list("anneal_cycle", "temperature")
    assert nl.values == [30.0, 60.0, 90.0, 90.0, 60.0, 30.0]   # materialized post-cycle
    assert nl.spec == {"values": [30.0, 60.0, 90.0], "cycle": True}
    assert nl.md["ramp_rate"] == 5.0 and nl.md["first_soak"] == 300.0

