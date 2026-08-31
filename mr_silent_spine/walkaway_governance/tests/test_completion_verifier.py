import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import completion_verifier as cv  # noqa: E402


REAL_ROOT = Path("/opt/pulse5-core")


def make_cfg(tmp_path: Path, **overrides) -> cv.VerifierConfig:
    cfg = cv.VerifierConfig(
        priority_state_path=tmp_path / "founder_priority_state.json",
        execution_governor_path=tmp_path / "execution_governor.json",
        founder_priority_governor_path=REAL_ROOT / "mr_silent_spine/autonomous_exec/founder_priority_governor.py",
        contracts_dir=tmp_path / "completion_contracts",
        receipts_dir=tmp_path / "verifier_receipts",
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


def base_state(**rank_overrides):
    ranks = {"SOME_ORDINARY_ITEM": {"status": "pending", "note": "not complete yet"}}
    ranks.update(rank_overrides)
    return {"ranks": ranks}


def setup_common(tmp_path):
    write_json(tmp_path / "execution_governor.json", DEFAULT_GOVERNOR)


def write_contract(cfg, item_id, **overrides):
    contract = {
        "completion_contract_version": "1.0",
        "task_or_priority_id": item_id,
        "action_type": "test_action",
        "required_classification": "NON_PROTECTED",
        "evidence_sources": {"note": "test fixture"},
        "required_checks": [],
    }
    contract.update(overrides)
    write_json(cfg.contracts_dir / cv._contract_filename(item_id), contract)
    return contract


# ---------------------------------------------------------------------------
# core verdict matrix
# ---------------------------------------------------------------------------

def test_nonprotected_valid_contract_verified(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        DEMO_ITEM={"status": "pending", "note": "ordinary internal work"},
    ))
    cfg = make_cfg(tmp_path)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("evidence content")
    result_json = tmp_path / "job_result.json"
    write_json(result_json, {"status": "executed_safe_internal"})

    write_contract(cfg, "DEMO_ITEM", required_checks=[
        {"type": "artifact_exists", "label": "artifact_present", "path": str(artifact)},
        {"type": "governed_job_terminal_status", "label": "job_terminal",
         "path": str(result_json), "field_path": "status", "expected_value": "executed_safe_internal"},
        {"type": "command_exit_code", "label": "true_exits_zero",
         "command": ["python3", "-c", "import sys; sys.exit(0)"], "expected_exit_code": 0},
        {"type": "named_test", "label": "pytest_self_check",
         "command": ["python3", "-m", "pytest", "-q", __file__ + "::test_helper_always_passes"],
         "min_passed": 1},
    ])

    result = cv.verify_completion("DEMO_ITEM", cfg)
    assert result["verdict"] == "VERIFIED_COMPLETE"
    assert result["classification"] == "NON_PROTECTED"
    assert set(result["checks_run"]) == {"artifact_exists", "governed_job_terminal_status",
                                          "command_exit_code", "named_test"}
    assert all(c["passed"] for c in result["check_results"])
    assert result["failure_reasons"] == []
    receipts = list(cfg.receipts_dir.glob("*.json"))
    assert len(receipts) == 1


def test_helper_always_passes():
    assert True


def test_missing_contract_denied(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        NO_CONTRACT_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("NO_CONTRACT_ITEM", cfg)
    assert result["verdict"] == "DENIED_MISSING_CONTRACT"


def test_missing_evidence_denied(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        MISSING_EVIDENCE_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    write_contract(cfg, "MISSING_EVIDENCE_ITEM", required_checks=[
        {"type": "artifact_exists", "label": "never_written", "path": str(tmp_path / "does_not_exist.txt")},
    ])
    result = cv.verify_completion("MISSING_EVIDENCE_ITEM", cfg)
    assert result["verdict"] == "DENIED_MISSING_EVIDENCE"


def test_invalid_evidence_denied_bad_json(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        BAD_JSON_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    bad = tmp_path / "not_json.json"
    bad.write_text("{ this is not valid json ")
    write_contract(cfg, "BAD_JSON_ITEM", required_checks=[
        {"type": "json_field_check", "label": "field", "path": str(bad),
         "field_path": "status", "expected_value": "complete"},
    ])
    result = cv.verify_completion("BAD_JSON_ITEM", cfg)
    assert result["verdict"] == "DENIED_INVALID_EVIDENCE"


def test_self_attested_text_only_denied(tmp_path):
    """A contract that declares no recognized deterministic check type --
    i.e. only a prose attestation -- must be rejected at contract-shape
    validation, never evaluated as if it were evidence."""
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        PROSE_ONLY_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    write_contract(cfg, "PROSE_ONLY_ITEM", required_checks=[
        {"type": "self_attested_text", "label": "trust_me", "claim": "the work is done"},
    ])
    result = cv.verify_completion("PROSE_ONLY_ITEM", cfg)
    assert result["verdict"] == "DENIED_INVALID_EVIDENCE"
    assert "unsupported_check_type" in result["failure_reasons"][0]


def test_self_attested_text_only_denied_empty_checks(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        EMPTY_CHECKS_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    write_contract(cfg, "EMPTY_CHECKS_ITEM", required_checks=[])
    result = cv.verify_completion("EMPTY_CHECKS_ITEM", cfg)
    assert result["verdict"] == "DENIED_INVALID_EVIDENCE"


def test_ambiguous_classification_denied_blocked_status(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        BLOCKED_ITEM={"status": "blocked", "note": "waiting on something"},
    ))
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("BLOCKED_ITEM", cfg)
    assert result["verdict"] == "DENIED_AMBIGUOUS"
    assert result["failure_reasons"] == ["blocked_status_requires_human_review"]


def test_ambiguous_classification_denied_unrecognized_gate(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        UNKNOWN_GATE_ITEM={"status": "pending", "note": "ordinary", "gate": "totally_made_up_gate"},
    ))
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("UNKNOWN_GATE_ITEM", cfg)
    assert result["verdict"] == "DENIED_AMBIGUOUS"
    assert "unrecognized_gate_field" in result["failure_reasons"][0]


def test_unclassified_action_denied_item_not_found(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state())
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("ITEM_THAT_DOES_NOT_EXIST_ANYWHERE", cfg)
    assert result["verdict"] == "DENIED_UNCLASSIFIED"


# ---------------------------------------------------------------------------
# protected-gate regression matrix -- reuses walkaway_advance's EXISTING
# canonical classifier; proves this module inherits the same hard denials.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item_id,note,expected_reason", [
    ("PROD_PROMOTE_ITEM", "requires production_promotion of the new build", "protected_production_promotion"),
    ("CRED_CHANGE_ITEM", "rotate the api_key for the service", "credential_change"),
    ("CRED_EXPOSURE_ITEM", "task would expose a secret token publicly", "credential_change"),
    ("PAID_ITEM", "activate the paid billing tier", "paid_resource_activation"),
    ("MODEL_DEL_ITEM", "run model_delete on the stale checkpoint", "model_deletion"),
    ("MODEL_REPLACE_ITEM", "replace the protected model via model_delete then reload", "model_deletion"),
    ("GPU_ITEM", "gpu_delete the render-forge node", "render_gpu_deletion"),
])
def test_protected_gates_denied(tmp_path, item_id, note, expected_reason):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(**{
        item_id: {"status": "pending", "note": note},
    }))
    cfg = make_cfg(tmp_path)
    write_contract(cfg, item_id, required_checks=[
        {"type": "artifact_exists", "label": "irrelevant", "path": str(tmp_path)},
    ])
    result = cv.verify_completion(item_id, cfg)
    assert result["verdict"] == "DENIED_PROTECTED"
    assert result["failure_reasons"] == [expected_reason]


def test_scorpio_isolation_change_denied(tmp_path):
    setup_common(tmp_path)
    state = base_state()
    state["ranks"]["SCORPIOS_CORNER"] = {
        "status": "in_progress",
        "components": {"SOME_COMPONENT": {"status": "pending", "note": "totally ordinary work"}},
    }
    write_json(tmp_path / "founder_priority_state.json", state)
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("SCORPIOS_CORNER/SOME_COMPONENT", cfg)
    assert result["verdict"] == "DENIED_PROTECTED"
    assert result["failure_reasons"] == ["isolation_boundary"]


# ---------------------------------------------------------------------------
# schema / determinism / idempotency
# ---------------------------------------------------------------------------

def test_verifier_result_schema(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        SCHEMA_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    result = cv.verify_completion("SCHEMA_ITEM", cfg)
    required_keys = {
        "task_id", "contract_version", "classification", "checks_run", "check_results",
        "artifacts_verified", "evidence_hashes", "verdict", "failure_reasons",
        "timestamp_utc", "validator_version",
    }
    assert required_keys <= set(result.keys())
    assert result["verdict"] in cv.VERDICTS


def test_verifier_determinism(tmp_path):
    setup_common(tmp_path)
    write_json(tmp_path / "founder_priority_state.json", base_state(
        DETERMINISM_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("evidence")
    write_contract(cfg, "DETERMINISM_ITEM", required_checks=[
        {"type": "artifact_exists", "label": "a", "path": str(artifact)},
    ])
    r1 = cv.verify_completion("DETERMINISM_ITEM", cfg)
    r2 = cv.verify_completion("DETERMINISM_ITEM", cfg)

    def strip_volatile(r):
        return {k: v for k, v in r.items() if k != "timestamp_utc"}

    assert strip_volatile(r1) == strip_volatile(r2)


def test_verifier_idempotency_read_only_on_priority_state(tmp_path):
    setup_common(tmp_path)
    state_path = tmp_path / "founder_priority_state.json"
    write_json(state_path, base_state(
        IDEMPOTENT_ITEM={"status": "pending", "note": "ordinary"},
    ))
    cfg = make_cfg(tmp_path)
    write_contract(cfg, "IDEMPOTENT_ITEM", required_checks=[
        {"type": "command_exit_code", "label": "noop", "command": ["python3", "-c", "pass"]},
    ])
    before = state_path.read_text()
    for _ in range(3):
        result = cv.verify_completion("IDEMPOTENT_ITEM", cfg)
        assert result["verdict"] == "VERIFIED_COMPLETE"
    after = state_path.read_text()
    assert before == after  # never writes canonical state, however many times it is called
    assert len(list(cfg.receipts_dir.glob("*.json"))) == 3  # each call is an independent, honest re-check


# ---------------------------------------------------------------------------
# verify_all / --all convenience wrapper
# ---------------------------------------------------------------------------

def test_verify_all_covers_every_rank_and_component(tmp_path):
    setup_common(tmp_path)
    state = base_state(
        RANK_A={"status": "pending", "note": "ordinary"},
        RANK_B={"status": "blocked", "note": "waiting"},
    )
    write_json(tmp_path / "founder_priority_state.json", state)
    cfg = make_cfg(tmp_path)
    results = cv.verify_all(cfg)
    ids = {r["task_id"] for r in results}
    assert {"SOME_ORDINARY_ITEM", "RANK_A", "RANK_B"} <= ids
