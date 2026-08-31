import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import walkaway_advance as wa  # noqa: E402


REAL_ROOT = Path("/opt/pulse5-core")


def make_cfg(tmp_path: Path, **overrides) -> wa.WalkawayConfig:
    cfg = wa.WalkawayConfig(
        priority_state_path=tmp_path / "founder_priority_state.json",
        execution_governor_path=tmp_path / "execution_governor.json",
        safe_runner_registry_path=tmp_path / "safe_runner_registry_v1.json",
        preflight_gate_path=REAL_ROOT / "mr_silent_spine/preflight_executor/preflight_gate.py",
        validated_runner_registry_path=tmp_path / "validated_runner_registry_v1.json",
        router_v2b_path=REAL_ROOT / "mr_silent_spine/safe_execution_router/safe_execution_router_v2b.py",
        activated_tasks_dir=tmp_path / "activated_tasks",
        ledger_path=tmp_path / "ledger.jsonl",
        idempotency_path=tmp_path / "idempotency.json",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


DEFAULT_GOVERNOR = {
    "safe_auto_execute": True,
    "blocked_actions": ["delete_system_files", "modify_credentials", "access_secrets",
                         "spend_money", "external_payment", "change_auth"],
    "requires_approval_keywords": ["payment", "billing", "api_key", "password",
                                    "credential", "external_account"],
}

DEFAULT_REGISTRY = {
    "routes": {
        "service_health": {"risk": "low_readonly", "requires_storage_ok": False,
                            "requires_approval_for_live": False,
                            "validation": ["status_output", "report_file"]},
    }
}


def base_state(**rank_overrides):
    ranks = {
        "SOME_ORDINARY_ITEM": {"status": "pending", "note": "not complete yet"},
    }
    ranks.update(rank_overrides)
    return {"ranks": ranks}


def setup_common(tmp_path):
    write_json(tmp_path / "execution_governor.json", DEFAULT_GOVERNOR)
    write_json(tmp_path / "safe_runner_registry_v1.json", DEFAULT_REGISTRY)
    write_json(tmp_path / "validated_runner_registry_v1.json", {"routes": []})


def make_evidence(tmp_path, name="receipt.json", content=None):
    p = tmp_path / name
    p.write_text(json.dumps(content or {"certified": True}))
    return str(p)


# ---------------------------------------------------------------------------
# Safety tests
# ---------------------------------------------------------------------------

def test_nonprotected_certified_completion_allowed(tmp_path):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        DEMO_NONPROTECTED_ITEM={
            "status": "complete", "note": "read-only health check followup",
            "action_type": "service_health", "evidence_path": evidence,
        }
    ))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "DEMO_NONPROTECTED_ITEM")
    assert item["decision"] == "ALLOW"
    assert item["work_item_path"] is not None
    assert Path(item["work_item_path"]).exists()
    assert report["auto_completion_authority"] == "NOT_IMPLEMENTED"


def test_missing_evidence_denied(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        NO_EVIDENCE_ITEM={"status": "complete", "note": "health followup",
                           "action_type": "service_health"},
    ))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "NO_EVIDENCE_ITEM")
    assert item["decision"] == "DENY"
    assert item["reason"] == "missing_evidence"


def test_ambiguous_classification_denied(tmp_path):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        UNKNOWN_ACTION_ITEM={"status": "complete", "note": "some new kind of work",
                              "action_type": "totally_unregistered_action_xyz",
                              "evidence_path": evidence},
    ))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "UNKNOWN_ACTION_ITEM")
    assert item["decision"] == "DENY"
    assert item["reason"] == "unclassified_action_type_default_deny"


@pytest.mark.parametrize("item_id,note,expected_gate", [
    ("PROD_PROMOTE_ITEM", "requires production_promotion of the new build", "protected_production_promotion"),
    ("CRED_ITEM", "rotate the api_key for the service", "credential_change"),
    ("PAID_ITEM", "activate the paid billing tier", "paid_resource_activation"),
    ("MODEL_DEL_ITEM", "run model_delete on the stale checkpoint", "model_deletion"),
    ("SCORPIO_ITEM", "adjust scorpio isolation boundary", "scorpio_isolation_change"),
    ("GPU_ITEM", "gpu_delete the render-forge node", "render_gpu_deletion"),
])
def test_protected_gates_denied_without_founder(tmp_path, item_id, note, expected_gate):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(**{
        item_id: {"status": "complete", "note": note, "action_type": "service_health",
                  "evidence_path": evidence},
    }))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == item_id)
    assert item["decision"] == "DENY"
    assert item["classification"] == "protected"
    assert item["reason"] == expected_gate


def test_scorpio_isolation_boundary_never_crossed_regardless_of_content(tmp_path):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    state = base_state()
    state["ranks"]["SCORPIOS_CORNER"] = {
        "status": "in_progress",
        "components": {
            "SOME_COMPONENT": {"status": "complete", "note": "totally ordinary work",
                                "action_type": "service_health", "evidence_path": evidence},
        },
    }
    write_json(tmp_path / "founder_priority_state.json", state)
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "SCORPIOS_CORNER/SOME_COMPONENT")
    assert item["decision"] == "DENY"
    assert item["reason"] == "isolation_boundary"


def test_duplicate_completion_idempotent(tmp_path):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        REPEAT_ITEM={"status": "complete", "note": "read-only health check followup",
                      "action_type": "service_health", "evidence_path": evidence},
    ))
    cfg = make_cfg(tmp_path)

    report1 = wa.run_cycle(cfg)
    item1 = next(r for r in report1["results"] if r["item_id"] == "REPEAT_ITEM")
    assert item1["decision"] == "ALLOW"
    work_items_after_1 = list(cfg.activated_tasks_dir.glob("*.json"))
    assert len(work_items_after_1) == 1

    report2 = wa.run_cycle(cfg)
    item2 = next(r for r in report2["results"] if r["item_id"] == "REPEAT_ITEM")
    assert item2["decision"] == "ALLOW_DUPLICATE_SKIPPED"
    work_items_after_2 = list(cfg.activated_tasks_dir.glob("*.json"))
    assert len(work_items_after_2) == 1  # no duplicate created
    assert item2["work_item_path"] == item1["work_item_path"]


def test_audit_record_written_for_every_decision(tmp_path):
    setup_common(tmp_path)
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        ALLOWED_ITEM={"status": "complete", "note": "read-only health check followup",
                      "action_type": "service_health", "evidence_path": evidence},
        DENIED_ITEM={"status": "complete", "note": "api_key rotation",
                     "action_type": "service_health", "evidence_path": evidence},
    ))
    cfg = make_cfg(tmp_path)
    wa.run_cycle(cfg)
    assert cfg.ledger_path.exists()
    lines = [json.loads(l) for l in cfg.ledger_path.read_text().splitlines() if l.strip()]
    ids = {l["item_id"] for l in lines}
    assert "ALLOWED_ITEM" in ids and "DENIED_ITEM" in ids
    for l in lines:
        assert l["event"] == "walkaway_governance_transition"
        assert l["actor"] == "walkaway_advance_v1"
        assert l["timestamp_utc"]
        assert l["decision"] in ("ALLOW", "DENY", "SKIP", "ALLOW_DUPLICATE_SKIPPED")


# ---------------------------------------------------------------------------
# Continuation tests
# ---------------------------------------------------------------------------

def test_autonomous_work_item_creation_and_worker_routing(tmp_path):
    setup_common(tmp_path)
    fake_runner = tmp_path / "fake_health_probe_runner.py"
    fake_runner.write_text("# stand-in for a real, on-disk, non-vendored runner\n")
    write_json(tmp_path / "validated_runner_registry_v1.json", {
        "routes": [{
            "problem_type": "service_health",
            "valid_runners": [{"path": str(fake_runner), "reason": "health probe"}],
            "services": [],
            "status": "validated",
        }]
    })
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        ROUTABLE_ITEM={"status": "complete", "note": "health followup",
                        "action_type": "service_health", "evidence_path": evidence},
    ))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "ROUTABLE_ITEM")
    assert item["decision"] == "ALLOW"
    assert item["worker_routing"]["selected_runner"] == str(fake_runner)
    work_item = json.loads(Path(item["work_item_path"]).read_text())
    assert work_item["status"] == "activated_waiting_worker"
    assert work_item["routing_proposal"]["selected_runner"] == str(fake_runner)


def test_worker_routing_rejects_vendored_noise_candidates(tmp_path):
    setup_common(tmp_path)
    fake_vendor_path = tmp_path / ".venv" / "lib" / "some_pkg" / "unrelated_internal.py"
    fake_vendor_path.parent.mkdir(parents=True, exist_ok=True)
    fake_vendor_path.write_text("# vendored noise that happens to keyword-match\n")
    write_json(tmp_path / "validated_runner_registry_v1.json", {
        "routes": [{
            "problem_type": "service_health",
            "valid_runners": [{"path": str(fake_vendor_path), "reason": "keyword match only"}],
            "services": [],
            "status": "validated",
        }]
    })
    evidence = make_evidence(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        NOISY_ROUTE_ITEM={"status": "complete", "note": "health followup",
                           "action_type": "service_health", "evidence_path": evidence},
    ))
    cfg = make_cfg(tmp_path)
    report = wa.run_cycle(cfg)
    item = next(r for r in report["results"] if r["item_id"] == "NOISY_ROUTE_ITEM")
    assert item["decision"] == "ALLOW"  # item itself is still fine
    assert item["worker_routing"]["selected_runner"] is None
    assert item["worker_routing"]["status"] == "no_safe_worker_found_manual_dispatch_required"


def test_live_governor_next_priority_selection_is_read_only(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", {
        "ranks": {
            "RANK_A": {"status": "complete"},
            "RANK_B": {"status": "pending"},
            "RANK_C": {"status": "pending"},
        }
    })
    cfg = make_cfg(tmp_path)
    before = json.loads(cfg.priority_state_path.read_text())
    report = wa.run_cycle(cfg)
    after = json.loads(cfg.priority_state_path.read_text())
    assert report["next_governing_priority"] == {"rank_id": "RANK_B", "status": "pending"}
    assert before == after  # governor advance never writes priority state


def test_second_autonomous_continuation_cycle(tmp_path):
    """Simulates two timer firings: cycle 1 processes an already-complete
    item; a human then marks a second item complete between cycles; cycle 2
    picks up the new item (fresh delegation) while correctly no-op'ing the
    first (idempotent), proving continuation across cycles without founder
    re-approval per item."""
    setup_common(tmp_path)
    evidence_a = make_evidence(tmp_path, "receipt_a.json")
    state_path = tmp_path / "founder_priority_state.json"
    write_json(state_path, base_state(
        ITEM_A={"status": "complete", "note": "first evidenced item",
                "action_type": "service_health", "evidence_path": evidence_a},
        ITEM_B={"status": "pending", "note": "not yet complete"},
    ))
    cfg = make_cfg(tmp_path)

    report1 = wa.run_cycle(cfg)
    a1 = next(r for r in report1["results"] if r["item_id"] == "ITEM_A")
    assert a1["decision"] == "ALLOW"
    assert len(list(cfg.activated_tasks_dir.glob("*.json"))) == 1

    # Human marks ITEM_B complete between cycles (only a human/verification
    # process may do this -- simulated here as the fixture's next state).
    evidence_b = make_evidence(tmp_path, "receipt_b.json")
    state = json.loads(state_path.read_text())
    state["ranks"]["ITEM_B"] = {"status": "complete", "note": "second evidenced item",
                                 "action_type": "service_health", "evidence_path": evidence_b}
    write_json(state_path, state)

    report2 = wa.run_cycle(cfg)
    a2 = next(r for r in report2["results"] if r["item_id"] == "ITEM_A")
    b2 = next(r for r in report2["results"] if r["item_id"] == "ITEM_B")
    assert a2["decision"] == "ALLOW_DUPLICATE_SKIPPED"
    assert b2["decision"] == "ALLOW"
    assert len(list(cfg.activated_tasks_dir.glob("*.json"))) == 2
    assert a2["work_item_path"] != b2["work_item_path"]
