"""Tests for omniregistry_manifest.py — Domain-A whole-Studio visibility
gap-closure (2026-09-08): a real, read-only census of the canonical
omniregistry/registry.json division manifest, previously read by no durable
source at all. See that module's own docstring for why this is a separate,
visibility-only registry, never wired into Mission-intake."""
from __future__ import annotations

import json

import omniregistry_manifest as orm


def _write(tmp_path, data):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(data))
    return p


def test_missing_file_returns_none_never_zero(monkeypatch, tmp_path):
    """Absence must be distinguishable from 'zero real divisions' — a
    caller silently treating None as empty would misreport a genuine data
    outage as 'no work exists'."""
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", tmp_path / "does_not_exist.json")
    assert orm.census() is None


def test_malformed_json_returns_none_never_fabricates(monkeypatch, tmp_path):
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", tmp_path / "registry.json")
    (tmp_path / "registry.json").write_text("{not valid json")
    assert orm.census() is None


def test_classifies_real_schema_shapes_correctly(monkeypatch, tmp_path):
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", _write(tmp_path, {
        "omni_forge": {"name": "omni_forge", "type": "division", "status": "active"},
        "omni_lingua": {"name": "omni_lingua", "type": "division", "status": "active"},
        "omniworld": {"name": "omniworld", "type": "flagship_project", "status": "project"},
        "omnitrade": {"name": "omnitrade", "type": "project", "status": "project"},
        "omniregistry": {"name": "omniregistry", "type": "system", "status": "active"},
        "omnisim_expanded": {"name": "omnisim_expanded", "type": "future_entity",
                              "status": "concept", "integration": "not_built"},
        "some_signal": {"name": "some_signal", "type": "discovered_signal", "status": "learned"},
        "legacy_entry": {"last_updated": "x", "purpose": "no type key at all"},
    }))
    c = orm.census()
    assert c is not None
    assert c.total_entries == 8
    assert c.divisions_active == ["omni_forge", "omni_lingua"]
    assert set(c.projects) == {"omniworld", "omnitrade"}
    assert c.systems == ["omniregistry"]
    assert c.future_entities_not_built == ["omnisim_expanded"]
    assert c.discovered_signals_total == 1
    assert c.legacy_untyped_total == 1


def test_inactive_division_never_counted_active(monkeypatch, tmp_path):
    """A division present but not status=='active' must never be reported
    as an active division — this module never guesses a status forward."""
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", _write(tmp_path, {
        "paused_div": {"name": "paused_div", "type": "division", "status": "paused"},
    }))
    c = orm.census()
    assert c.divisions_active == []


def test_non_dict_entries_are_skipped_not_fabricated(monkeypatch, tmp_path):
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", _write(tmp_path, {
        "weird": ["not", "a", "dict"],
        "real_div": {"name": "real_div", "type": "division", "status": "active"},
    }))
    c = orm.census()
    assert c.total_entries == 2
    assert c.divisions_active == ["real_div"]


def test_empty_registry_reports_zero_not_none(monkeypatch, tmp_path):
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", _write(tmp_path, {}))
    c = orm.census()
    assert c is not None
    assert c.total_entries == 0
    assert c.divisions_active == []
