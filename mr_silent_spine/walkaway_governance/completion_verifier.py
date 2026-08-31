#!/usr/bin/env python3
"""
Completion Contract + Deterministic Evidence Verifier -- decision-support only.

SCOPE (deliberately narrow -- read this before extending):

  This module NEVER writes to founder_priority_state.json, or to any other
  canonical priority/state store. VerifierConfig does not even carry a write
  path for canonical state. It has no completion-granting authority.

  It answers exactly one question, deterministically: given a completion
  contract for a given item, and the real, current, independently-inspected
  evidence on this machine, does that item's claimed completion hold up?
  It returns a structured verdict (see VERDICTS below) and writes a durable
  receipt. A human, or a separately-authorized transition adapter reading
  VERIFIED_COMPLETE through the interface documented at the bottom of this
  file, decides what to do with that verdict. This module is not that
  authority, and does not attempt to become it.

WHY THIS IS DELIBERATELY *NOT* THE COMPLETION AUTHORITY ITSELF
----------------------------------------------------------------
A verifier that (a) runs inside the same AI-operated, unattended pipeline
that produces the evidence it checks, AND (b) has write access to canonical
completion state, is not independent of the thing it corroborates -- it is
a longer self-attestation chain, not a break from one. See
founder_signal_accountability_loop/receipts/
walkaway_governance_campaign_receipt_20260831T202259Z.json, addendum
20260831T213543Z, for the prior, separate decision on this exact point
(same conclusion, reached independently by this session before reading that
receipt). This module keeps the one boundary that makes its checks mean
something: it can prove "the declared evidence, independently re-inspected
right now, says X" -- it cannot, and does not try to, turn that proof into
an action. See INTEGRATION INTERFACE at the bottom of this file.

Nothing in this module executes destructive commands, spends money, touches
credentials, promotes to production, deletes models, changes Scorpio
isolation, or deletes Render/GPU infrastructure -- it reuses the same
protected-gate keyword list and isolation boundary that walkaway_advance.py
hard-denies regardless of what any config file says.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
import walkaway_advance as wa  # noqa: E402  (same-package reuse of the canonical classifier)

ROOT = Path("/opt/pulse5-core")

VALIDATOR_VERSION = "completion_verifier_v1"
SUPPORTED_CONTRACT_VERSION = "1.0"

VERDICTS = (
    "VERIFIED_COMPLETE",
    "NOT_COMPLETE",
    "DENIED_PROTECTED",
    "DENIED_UNCLASSIFIED",
    "DENIED_AMBIGUOUS",
    "DENIED_MISSING_CONTRACT",
    "DENIED_MISSING_EVIDENCE",
    "DENIED_INVALID_EVIDENCE",
)

REQUIRED_CONTRACT_FIELDS = {
    "completion_contract_version", "task_or_priority_id", "action_type",
    "required_classification", "required_checks", "evidence_sources",
}


@dataclass
class VerifierConfig:
    priority_state_path: Path = ROOT / "mr_silent_spine/state/founder_priority_state.json"
    execution_governor_path: Path = ROOT / "mr_silent_spine/state/execution_governor.json"
    founder_priority_governor_path: Path = ROOT / "mr_silent_spine/autonomous_exec/founder_priority_governor.py"
    contracts_dir: Path = ROOT / "mr_silent_spine/walkaway_governance/completion_contracts"
    receipts_dir: Path = ROOT / "mr_silent_spine/walkaway_governance/verifier_receipts"
    isolation_boundaries: tuple = wa.ISOLATION_BOUNDARIES


# ---------------------------------------------------------------------------
# path / hash helpers
# ---------------------------------------------------------------------------

def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _dot_get(d: Any, path: str) -> tuple[Any, bool]:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


# ---------------------------------------------------------------------------
# deterministic check handlers -- each returns (ok: bool, detail: dict).
# On failure, detail SHOULD include "failure_kind" in {"missing", "invalid",
# "not_yet_true"} so the aggregator can pick the correct DENIED_* verdict.
# ---------------------------------------------------------------------------

def _check_artifact_exists(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    p = _resolve_path(check["path"])
    exists = p.exists() and p.is_file() and p.stat().st_size > 0
    detail = {"path": str(p), "exists": p.exists(), "size": (p.stat().st_size if p.exists() else 0)}
    if not exists:
        detail["failure_kind"] = "missing"
    return exists, detail


def _check_artifact_hash(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    p = _resolve_path(check["path"])
    if not p.exists():
        return False, {"path": str(p), "failure_kind": "missing", "error": "artifact_missing"}
    digest = _sha256(p)
    if check.get("record_only"):
        return True, {"path": str(p), "sha256": digest, "mode": "record_only"}
    expected = check.get("expected_sha256")
    ok = digest == expected
    detail = {"path": str(p), "sha256": digest, "expected_sha256": expected}
    if not ok:
        detail["failure_kind"] = "invalid"
    return ok, detail


def _check_artifact_hash_compare(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    a, b = _resolve_path(check["path_a"]), _resolve_path(check["path_b"])
    if not a.exists() or not b.exists():
        return False, {"path_a": str(a), "path_b": str(b), "failure_kind": "missing", "error": "artifact_missing"}
    ha, hb = _sha256(a), _sha256(b)
    equal = ha == hb
    relation = check.get("relation", "match")
    ok = equal if relation == "match" else (not equal)
    detail = {"path_a": str(a), "path_b": str(b), "sha256_a": ha, "sha256_b": hb, "relation": relation, "equal": equal}
    if not ok:
        detail["failure_kind"] = "not_yet_true"
    return ok, detail


def _check_json_field(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    p = _resolve_path(check["path"])
    if not p.exists():
        return False, {"path": str(p), "failure_kind": "missing", "error": "artifact_missing"}
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        return False, {"path": str(p), "failure_kind": "invalid", "error": f"invalid_json:{e}"}
    field_path = check.get("field_path", "status")
    value, found = _dot_get(data, field_path)
    if not found:
        return False, {"path": str(p), "field_path": field_path, "failure_kind": "invalid", "error": "field_not_found"}
    expected = check["expected_value"]
    ok = value == expected
    detail = {"path": str(p), "field_path": field_path, "actual_value": value, "expected_value": expected}
    if not ok:
        detail["failure_kind"] = "not_yet_true"
    return ok, detail


def _check_command_exit_code(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    cmd = check["command"]
    timeout = check.get("timeout_seconds", 20)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    except Exception as e:
        return False, {"command": cmd, "failure_kind": "missing", "error": f"could_not_execute:{e}"}
    expected = check.get("expected_exit_code", 0)
    ok = proc.returncode == expected
    detail = {
        "command": cmd, "exit_code": proc.returncode, "expected_exit_code": expected,
        "stdout_tail": (proc.stdout or "")[-500:], "stderr_tail": (proc.stderr or "")[-500:],
    }
    if not ok:
        detail["failure_kind"] = "not_yet_true"
    return ok, detail


def _check_named_test(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    cmd = check["command"]
    timeout = check.get("timeout_seconds", 60)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    except Exception as e:
        return False, {"command": cmd, "failure_kind": "missing", "error": f"could_not_execute:{e}"}
    stdout = proc.stdout or ""
    passed_m = re.search(r"(\d+) passed", stdout)
    failed_m = re.search(r"(\d+) failed", stdout)
    passed = int(passed_m.group(1)) if passed_m else 0
    failed = int(failed_m.group(1)) if failed_m else 0
    min_passed = check.get("min_passed", 1)
    expected_exit = check.get("expected_exit_code", 0)
    ok = proc.returncode == expected_exit and passed >= min_passed and failed == 0
    detail = {
        "command": cmd, "exit_code": proc.returncode, "passed": passed, "failed": failed,
        "min_passed": min_passed, "stdout_tail": stdout[-800:],
    }
    if not ok:
        detail["failure_kind"] = "not_yet_true"
    return ok, detail


def _check_service_health(check: dict, cfg: VerifierConfig) -> tuple[bool, dict]:
    unit = check["unit"]
    expected = check.get("expected_state", "active")
    try:
        proc = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except Exception as e:
        return False, {"unit": unit, "failure_kind": "invalid", "error": f"systemctl_unavailable:{e}"}
    actual = (proc.stdout or "").strip()
    ok = actual == expected
    detail = {"unit": unit, "actual_state": actual, "expected_state": expected}
    if not ok:
        detail["failure_kind"] = "not_yet_true"
    return ok, detail


CHECK_HANDLERS = {
    "artifact_exists": _check_artifact_exists,
    "artifact_hash": _check_artifact_hash,
    "artifact_hash_compare": _check_artifact_hash_compare,
    "json_field_check": _check_json_field,
    "registry_state": _check_json_field,               # same mechanism, contract-level label only
    "governed_job_terminal_status": _check_json_field,  # same mechanism, contract-level label only
    "command_exit_code": _check_command_exit_code,
    "regression_check": _check_command_exit_code,        # same mechanism, contract-level label only
    "named_test": _check_named_test,
    "service_health": _check_service_health,
}


# ---------------------------------------------------------------------------
# contract loading / shape validation
# ---------------------------------------------------------------------------

def _contract_filename(item_id: str) -> str:
    return item_id.replace("/", "__") + ".json"


def load_contract(cfg: VerifierConfig, item_id: str) -> tuple[dict | None, Path]:
    path = cfg.contracts_dir / _contract_filename(item_id)
    if not path.exists():
        return None, path
    try:
        return json.loads(path.read_text()), path
    except Exception:
        return None, path


def validate_contract_shape(contract: dict) -> tuple[bool, str | None]:
    if contract.get("completion_contract_version") != SUPPORTED_CONTRACT_VERSION:
        return False, f"unsupported_contract_version:{contract.get('completion_contract_version')}"
    missing = REQUIRED_CONTRACT_FIELDS - set(contract.keys())
    if missing:
        return False, f"contract_missing_fields:{sorted(missing)}"
    if contract.get("required_classification") != "NON_PROTECTED":
        return False, "contract_required_classification_must_be_NON_PROTECTED"
    checks = contract.get("required_checks")
    if not isinstance(checks, list) or not checks:
        return False, "contract_has_no_deterministic_required_checks"
    for c in checks:
        if not isinstance(c, dict) or c.get("type") not in CHECK_HANDLERS:
            bad = c.get("type") if isinstance(c, dict) else c
            return False, f"contract_has_unsupported_check_type:{bad}"
    return True, None


# ---------------------------------------------------------------------------
# classification -- reuses the EXISTING canonical protection classifier
# (walkaway_advance.is_isolated / protected_gate_match) rather than inventing
# a new one. Adds one reconciliation walkaway_advance does not need: a rank's
# own "gate" field is checked against founder_priority_governor's
# RECOGNIZED_PROTECTED_GATES, since that is the second, separately-defined
# protected-gate list identified during architecture mapping.
# ---------------------------------------------------------------------------

def _load_recognized_gates(cfg: VerifierConfig) -> frozenset:
    if not cfg.founder_priority_governor_path.exists():
        return frozenset()
    try:
        module = wa._load_module_from_path("founder_priority_governor_reuse", cfg.founder_priority_governor_path)
        return frozenset(getattr(module, "RECOGNIZED_PROTECTED_GATES", ()))
    except Exception:
        return frozenset()


def _find_item(priority_state: dict, item_path: tuple[str, ...]) -> dict | None:
    ranks = priority_state.get("ranks", {})
    rank = ranks.get(item_path[0])
    if not isinstance(rank, dict):
        return None
    if len(item_path) == 1:
        return rank
    comp = (rank.get("components") or {}).get(item_path[1])
    return comp if isinstance(comp, dict) else None


def classify_for_verification(item_path: tuple[str, ...], item: dict | None, cfg: VerifierConfig,
                               governor_cfg: dict, recognized_gates: frozenset) -> dict:
    """Returns {"item_id", "verdict" (None if NON_PROTECTED and eligible to
    continue to contract evaluation, else one of the DENIED_* verdicts),
    "classification", "reason"}."""
    item_id = "/".join(item_path)

    if item is None:
        return {"item_id": item_id, "verdict": "DENIED_UNCLASSIFIED", "classification": "unclassified",
                "reason": "item_id_not_found_in_canonical_priority_state"}

    if wa.is_isolated(item_path, cfg):
        return {"item_id": item_id, "verdict": "DENIED_PROTECTED", "classification": "protected",
                "reason": "isolation_boundary"}

    note = item.get("note", "")
    action_type = item.get("action_type") or item.get("route") or ""
    gate_hit = wa.protected_gate_match(item_id, note, action_type, governor_cfg)
    if gate_hit:
        return {"item_id": item_id, "verdict": "DENIED_PROTECTED", "classification": "protected", "reason": gate_hit}

    status = (item.get("status") or "pending").lower()
    if status == "blocked":
        return {"item_id": item_id, "verdict": "DENIED_AMBIGUOUS", "classification": "ambiguous",
                "reason": "blocked_status_requires_human_review"}

    gate_field = item.get("gate")
    if gate_field and gate_field not in recognized_gates:
        return {"item_id": item_id, "verdict": "DENIED_AMBIGUOUS", "classification": "ambiguous",
                "reason": f"unrecognized_gate_field:{gate_field}"}

    return {"item_id": item_id, "verdict": None, "classification": "NON_PROTECTED", "reason": None}


# ---------------------------------------------------------------------------
# result / receipt
# ---------------------------------------------------------------------------

def _build_result(item_id: str, contract_version: str | None, classification: str,
                   checks_run: list[str], check_results: list[dict], artifacts_verified: list[str],
                   verdict: str, failure_reasons: list[str], evidence_hashes: dict | None = None) -> dict:
    assert verdict in VERDICTS, f"invalid verdict emitted: {verdict}"
    return {
        "task_id": item_id,
        "contract_version": contract_version,
        "classification": classification,
        "checks_run": checks_run,
        "check_results": check_results,
        "artifacts_verified": artifacts_verified,
        "evidence_hashes": evidence_hashes or {},
        "verdict": verdict,
        "failure_reasons": failure_reasons,
        "timestamp_utc": wa.now_iso(),
        "validator_version": VALIDATOR_VERSION,
    }


def _write_receipt(cfg: VerifierConfig, result: dict) -> Path:
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    safe_id = result["task_id"].replace("/", "__")
    stamp = result["timestamp_utc"].replace(":", "").replace("+00:00", "Z")
    path = cfg.receipts_dir / f"{safe_id}_{stamp}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def verify_completion(item_id: str, cfg: VerifierConfig | None = None) -> dict:
    """Deterministic, side-effect-free-on-canonical-state verification.

    Reads founder_priority_state.json (read-only), reads the completion
    contract for item_id if one exists, independently re-executes every
    declared check, and returns a structured verdict. Writes only a receipt
    under verifier_receipts/ -- never touches founder_priority_state.json or
    any other canonical store.
    """
    cfg = cfg or VerifierConfig()
    priority_state = wa.load_json(cfg.priority_state_path)
    governor_cfg = wa.load_json(cfg.execution_governor_path)
    recognized_gates = _load_recognized_gates(cfg)

    item_path = tuple(item_id.split("/"))
    item = _find_item(priority_state, item_path)

    classification = classify_for_verification(item_path, item, cfg, governor_cfg, recognized_gates)
    if classification["verdict"] is not None:
        result = _build_result(item_id, None, classification["classification"], [], [], [],
                                classification["verdict"], [classification["reason"]])
        _write_receipt(cfg, result)
        return result

    contract, contract_path = load_contract(cfg, item_id)
    if contract is None:
        result = _build_result(item_id, None, "NON_PROTECTED", [], [], [],
                                "DENIED_MISSING_CONTRACT", ["no_completion_contract_found_for_item"])
        _write_receipt(cfg, result)
        return result

    shape_ok, shape_err = validate_contract_shape(contract)
    if not shape_ok:
        result = _build_result(item_id, contract.get("completion_contract_version"), "NON_PROTECTED", [], [], [],
                                "DENIED_INVALID_EVIDENCE", [shape_err])
        _write_receipt(cfg, result)
        return result

    check_results: list[dict] = []
    failure_reasons: list[str] = []
    evidence_hashes: dict[str, str] = {}
    artifacts_verified: list[str] = []
    failure_kinds: set[str] = set()

    for check in contract["required_checks"]:
        ctype = check["type"]
        handler = CHECK_HANDLERS[ctype]
        try:
            ok, detail = handler(check, cfg)
        except Exception as e:
            ok, detail = False, {"failure_kind": "invalid", "error": f"check_execution_error:{e}"}
        entry = {"type": ctype, "label": check.get("label", ctype), "passed": ok, "detail": detail}
        check_results.append(entry)
        if isinstance(detail, dict):
            if "sha256" in detail:
                evidence_hashes[detail.get("path", ctype)] = detail["sha256"]
            if detail.get("path") and ctype in ("artifact_exists", "artifact_hash", "json_field_check",
                                                  "registry_state", "governed_job_terminal_status"):
                artifacts_verified.append(detail["path"])
        if not ok:
            failure_kinds.add(detail.get("failure_kind", "not_yet_true"))
            failure_reasons.append(f"{ctype}:{check.get('label', '')}:{json.dumps(detail, default=str)[:400]}")

    if not failure_kinds:
        verdict = "VERIFIED_COMPLETE"
    elif "missing" in failure_kinds:
        verdict = "DENIED_MISSING_EVIDENCE"
    elif "invalid" in failure_kinds:
        verdict = "DENIED_INVALID_EVIDENCE"
    else:
        verdict = "NOT_COMPLETE"

    result = _build_result(item_id, contract.get("completion_contract_version"), "NON_PROTECTED",
                            [c["type"] for c in check_results], check_results, artifacts_verified,
                            verdict, failure_reasons, evidence_hashes=evidence_hashes)
    _write_receipt(cfg, result)
    return result


def verify_all(cfg: VerifierConfig | None = None) -> list[dict]:
    """Convenience: verify every rank/component currently in canonical
    priority state. Read-only; does not require a contract to exist for
    every item (items without one simply report DENIED_MISSING_CONTRACT)."""
    cfg = cfg or VerifierConfig()
    priority_state = wa.load_json(cfg.priority_state_path)
    return [verify_completion("/".join(item_path), cfg)
            for item_path, _item in wa.iter_candidate_items(priority_state, cfg)]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: completion_verifier.py <item_id> | --all"}, indent=2))
        sys.exit(2)
    if sys.argv[1] == "--all":
        print(json.dumps(verify_all(), indent=2, default=str))
        return
    print(json.dumps(verify_completion(sys.argv[1]), indent=2, default=str))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# INTEGRATION INTERFACE FOR A FUTURE TRANSITION ADAPTER (NOT IMPLEMENTED HERE)
# ---------------------------------------------------------------------------
# A future, separately-authorized transition adapter may safely gate on
# exactly two fields -- no prose parsing required or supported:
#
#     result = verify_completion(item_id, cfg)
#     if result["verdict"] == "VERIFIED_COMPLETE" and result["classification"] == "NON_PROTECTED":
#         ... perform the atomic transition ...
#
# check_results / evidence_hashes / artifacts_verified are for the audit
# trail; a caller does not need to interpret them to make the decision.
#
# The eventual adapter (deliberately NOT built in this session) must, in
# order:
#
#   1. Accept only verdict == "VERIFIED_COMPLETE" and
#      classification == "NON_PROTECTED".
#   2. Re-run classify_for_verification() itself immediately before writing
#      -- never trust a verdict computed even seconds earlier, since state
#      may have changed -- against the SAME canonical store as step 4.
#   3. Acquire an exclusive lock before reading-modifying-writing canonical
#      state. No lock file exists yet for founder_priority_state.json; the
#      adapter must create and hold one (flock, LOCK_EX) across steps 4-7,
#      e.g. mr_silent_spine/state/.founder_priority_state.lock.
#   4. Re-read the CANONICAL store --
#      mr_silent_spine/state/founder_priority_state.json (NEVER
#      founder_top10_priority_queue.json, which is explicitly documented as
#      a read-cache projection, not the source of truth) -- and confirm the
#      item's status is still pending/in_progress, not already complete and
#      not now blocked/protected.
#   5. Atomically transition status -> "complete" for exactly the one
#      item_path verified, via write-to-temp-file-in-the-same-directory +
#      os.replace() (never edit the live file in place), preserving every
#      other field and every other rank untouched. Bump updated_at_utc.
#   6. Append one audit record to a NEW ledger, e.g.
#      mr_silent_spine/walkaway_governance/ledger/completion_transitions_v1.jsonl
#      (append_jsonl() from walkaway_advance.py is directly reusable),
#      containing: item_id, prior_state, resulting_state, contract_path,
#      contract_version, verifier_receipt_path, evidence_hashes,
#      idempotency_key (same shape as walkaway_advance.idempotency_key():
#      sha256 of item_id + a fingerprint of the verifier result),
#      authorization_basis="completion_verifier_v1:VERIFIED_COMPLETE",
#      timestamp_utc.
#   7. Release the lock.
#   8. Do nothing else. Writing "complete" to founder_priority_state.json is
#      itself the trigger: the EXISTING founder-free-studio-runtime.path
#      unit (PathModified=.../founder_priority_state.json) fires
#      founder-free-studio-runtime.service, which calls
#      walkaway_advance.run_cycle() -- already idempotent, already does
#      classification + evidence + work-item-creation + worker-routing for
#      any item now status=="complete". No new event path, governor,
#      scheduler, or delegation logic belongs in that adapter.
#      (event unit: /etc/systemd/system/founder-free-studio-runtime.path;
#       fallback timer: /etc/systemd/system/founder-free-studio-runtime.timer,
#       OnBootSec=5min OnUnitActiveSec=15min, unchanged by this session.)
