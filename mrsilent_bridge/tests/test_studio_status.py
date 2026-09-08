"""Tests for studio_status.py — whole-Studio census aggregation
(2026-09-08)."""
from __future__ import annotations

from datetime import datetime, timezone

import approval_backlog_hygiene as abh
import failure_anomaly_discovery as fad
import job_ledger
import mission
import omni_registry_stewardship as ors
import omniregistry_manifest as orm
import studio_status
import validation_canary_selfheal_discovery as vcs
import work_graph
from evolution import founder_request, observe


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(founder_request, "ESCALATIONS_DIR", tmp_path / "escalations")
    monkeypatch.setattr(observe, "OBSERVATIONS_DIR", tmp_path / "observations")
    (tmp_path / "observations").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ors, "OMNI_REGISTRY_ROOT", tmp_path / "omni_registry")
    # TEST_LIVE_STORE_CONTAMINATION-class fix (2026-09-08, same reasoning as
    # work_graph.py's STATE_INDEX_DIR fix elsewhere this campaign): without
    # this, every test here would read the REAL, live
    # /opt/pulse5-core/omniregistry/registry.json instead of a fixture,
    # making assertions about "empty studio" false.
    monkeypatch.setattr(orm, "REGISTRY_MANIFEST_PATH", tmp_path / "omniregistry_registry.json")
    monkeypatch.setattr(mission, "MISSIONS_DIR", tmp_path / "missions")
    monkeypatch.setattr(work_graph, "ITEMS_DIR", tmp_path / "workitems" / "items")
    monkeypatch.setattr(job_ledger, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(vcs, "HEALTH_LOOP_STATE_PATH", tmp_path / "health_loop_state.json")


def test_collect_on_empty_studio_reports_all_zero(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    s = studio_status.collect()
    assert s.registry_candidates_total == 0
    assert s.approvals_pending_raw == 0
    assert s.anomalies_total == 0
    assert s.missions_total == 0
    assert s.workitems_total == 0
    assert s.omniregistry_manifest_available is False
    assert s.omniregistry_divisions_active == []


def test_collect_reports_real_omniregistry_manifest_divisions(monkeypatch, tmp_path):
    """DOMAIN_A gap-closure (2026-09-08): the real, live manifest
    (omniregistry/registry.json) has entries in several real schema shapes
    (division/active, project, system, future_entity/concept) — this proves
    each is classified correctly and a future_entity is NEVER counted as an
    active division, matching the real 'omnisim_expanded' shape found in the
    live registry."""
    import json
    _fresh(monkeypatch, tmp_path)
    orm.REGISTRY_MANIFEST_PATH.write_text(json.dumps({
        "omni_forge": {"name": "omni_forge", "type": "division", "status": "active"},
        "omni_lingua": {"name": "omni_lingua", "type": "division", "status": "active"},
        "omniworld": {"name": "omniworld", "type": "flagship_project", "status": "project"},
        "omniregistry": {"name": "omniregistry", "type": "system", "status": "active"},
        "omnisim_expanded": {"name": "omnisim_expanded", "type": "future_entity", "status": "concept",
                              "integration": "not_built"},
        "omniscraper": {"last_updated": "x", "purpose": "legacy schema, no type key"},
    }))

    s = studio_status.collect()
    assert s.omniregistry_manifest_available is True
    assert s.omniregistry_manifest_total_entries == 6
    assert s.omniregistry_divisions_active == ["omni_forge", "omni_lingua"]
    assert s.omniregistry_projects_total == 1
    assert s.omniregistry_systems_total == 1
    assert s.omniregistry_future_entities_not_built_total == 1
    assert "omnisim_expanded" not in s.omniregistry_divisions_active

    text = studio_status.natural_language_status()
    assert "2 active divisions" in text
    assert "1 future/conceptual entities explicitly not built, not eligible for auto-start" in text


def test_collect_reflects_real_backing_sources(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    founder_request.request_founder_decision(
        subject="s", finding="a real finding", capability_needed="production_promotion",
        reason_required="always founder-gated", recommended_action="do it", risk="low",
        affected={"proposal_id": "p1", "job_id": "j1"},
    )
    s = studio_status.collect()
    assert s.approvals_pending_raw == 1
    assert s.approvals_true_founder_gate == 1


def test_natural_language_status_never_fabricates_when_empty(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    text = studio_status.natural_language_status()
    assert "0 Missions" in text
    assert "No unclaimed registered Studio divisions" in text


def test_natural_language_status_reports_real_counts(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    founder_request.request_founder_decision(
        subject="s", finding="a real finding", capability_needed="production_promotion",
        reason_required="always founder-gated", recommended_action="do it", risk="low",
        affected={"proposal_id": "p1", "job_id": "j1"},
    )
    text = studio_status.natural_language_status()
    assert "1 real items need attention" in text
    assert "1 are genuine Founder gates" in text


def test_collect_includes_validation_and_maintenance_findings(monkeypatch, tmp_path):
    import json
    from dataclasses import asdict
    _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="j1", task="t", requested_by="device_client", sandbox_path="/tmp/x")
    rec = job_ledger.load("j1")
    rec.validation_result = {"passed": False, "error": "boom"}
    rec.state = job_ledger.JobState.FAILED
    job_ledger._atomic_write_json(job_ledger._path("j1"), asdict(rec))
    vcs.HEALTH_LOOP_STATE_PATH.write_text(json.dumps({"warnings": ["HIGH_DISK_PRESSURE"], "down_services": []}))

    s = studio_status.collect()
    assert s.validation_failures_real == 1
    assert s.maintenance_findings_active == 1

    text = studio_status.natural_language_status()
    assert "1 real validation failures" in text
    assert "1 active maintenance finding" in text
