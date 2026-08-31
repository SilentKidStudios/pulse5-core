#!/usr/bin/env python3
"""
Walk-Away Governance: bounded, non-protected post-completion auto-advance.

SCOPE (deliberately narrow -- read this before extending):

  AUTO_COMPLETION_AUTHORITY: NOT IMPLEMENTED HERE.
    This module never sets, infers, or simulates a "complete" status.
    founder_priority_state.json documents completion as human-write-only
    ("The governor only reads this file; it never writes completion.")
    and this module honors that: it is a read-only consumer of that file.

  POST_COMPLETION_AUTO_ADVANCE_AND_DELEGATION: this module IS that.
    It only acts on ranks/components a human has ALREADY set to
    status == "complete". For each such item it: classifies (default-deny),
    checks durable evidence, creates one activated_tasks work item using the
    existing schema, gets a sanity-filtered worker routing proposal from the
    existing safe_execution_router_v2b logic, and writes one audit ledger
    entry. Idempotent per (item_id, evidence_fingerprint).

Nothing in this module executes code, spends money, touches credentials,
promotes to production, deletes models, changes Scorpio isolation, or
deletes Render/GPU infrastructure. Those stay hard-denied regardless of
what any config file says (see PROTECTED_GATE_KEYWORDS below).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/opt/pulse5-core")

# Founder-only gates that must ALWAYS deny, independent of any JSON config,
# because config files are self-editable by the same system and must not be
# the sole barrier for these. Matched case-insensitively against item id,
# note text, and action_type/route.
PROTECTED_GATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "protected_production_promotion": ("production_promotion", "promote_to_prod", "prod_deploy", "production_release"),
    "credential_change": ("credential", "password", "api_key", "secret", "auth_token", "change_auth", "modify_credentials", "access_secrets"),
    "paid_resource_activation": ("paid_activation", "billing", "payment", "subscription_activate", "spend_money", "external_payment"),
    "model_deletion": ("model_delete", "delete_model", "model_deletion"),
    "scorpio_isolation_change": ("scorpio",),
    "render_gpu_deletion": ("render-forge", "gpu_delete", "gpu_node_delete", "render_delete", "gpu_deletion"),
    "destructive_operation": ("delete_system_files", "rm -rf", "destroy_", "wipe_"),
}

ISOLATION_BOUNDARIES = ("SCORPIOS_CORNER",)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@dataclass
class WalkawayConfig:
    priority_state_path: Path = ROOT / "mr_silent_spine/state/founder_priority_state.json"
    execution_governor_path: Path = ROOT / "mr_silent_spine/state/execution_governor.json"
    safe_runner_registry_path: Path = ROOT / "mr_silent_spine/safe_runner_registry/safe_runner_registry_v1.json"
    preflight_gate_path: Path = ROOT / "mr_silent_spine/preflight_executor/preflight_gate.py"
    validated_runner_registry_path: Path = ROOT / "omniregistry/execution_router/validated_runner_registry_v1.json"
    router_v2b_path: Path = ROOT / "mr_silent_spine/safe_execution_router/safe_execution_router_v2b.py"
    activated_tasks_dir: Path = ROOT / "studio_execution_layer/activated_tasks/by_division/walkaway_governance"
    ledger_path: Path = ROOT / "mr_silent_spine/walkaway_governance/ledger/walkaway_governance_ledger_v1.jsonl"
    idempotency_path: Path = ROOT / "mr_silent_spine/walkaway_governance/state/idempotency_v1.json"
    isolation_boundaries: tuple = ISOLATION_BOUNDARIES
    # Path fragments that disqualify a routing candidate from auto-selection
    # even if the router's own scoring ranked it first -- the router map is
    # known to contain grep-matched noise from vendored dependency trees.
    disallowed_candidate_fragments: tuple = ("/.venv/", "/venv/", "site-packages", "/node_modules/")


def is_isolated(item_path: tuple[str, ...], cfg: WalkawayConfig) -> bool:
    return any(seg in cfg.isolation_boundaries for seg in item_path)


def protected_gate_match(item_id: str, note: str, action_type: str | None, governor_cfg: dict) -> str | None:
    haystack = " ".join(filter(None, [item_id, note, action_type or ""])).lower()

    for gate_name, keywords in PROTECTED_GATE_KEYWORDS.items():
        if any(kw.lower() in haystack for kw in keywords):
            return gate_name

    for blocked in governor_cfg.get("blocked_actions", []):
        if blocked.lower() in haystack:
            return f"execution_governor_blocked_action:{blocked}"

    for kw in governor_cfg.get("requires_approval_keywords", []):
        if kw.lower() in haystack:
            return f"execution_governor_requires_approval:{kw}"

    return None


def iter_candidate_items(priority_state: dict, cfg: WalkawayConfig):
    """Yield (item_path, item_dict) for every rank and rank-component."""
    ranks = priority_state.get("ranks", {})
    for rank_id, rank in ranks.items():
        if not isinstance(rank, dict):
            continue
        yield (rank_id,), rank
        for comp_id, comp in (rank.get("components") or {}).items():
            if isinstance(comp, dict):
                yield (rank_id, comp_id), comp


def evidence_ok(item: dict) -> tuple[bool, str | None, str | None]:
    """Returns (ok, evidence_fingerprint, reason_if_not_ok)."""
    evidence_path = item.get("evidence_path")
    if not evidence_path:
        return False, None, "missing_evidence"
    p = Path(evidence_path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists() or p.stat().st_size == 0:
        return False, None, "missing_evidence"
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return True, digest, None


def run_preflight(cfg: WalkawayConfig, action_type: str) -> dict:
    proc = subprocess.run(
        ["python3", str(cfg.preflight_gate_path), action_type],
        capture_output=True, text=True, timeout=20,
    )
    try:
        parsed = json.loads(proc.stdout.strip())
    except Exception:
        parsed = {"allowed": False, "raw": proc.stdout, "stderr": proc.stderr}
    return {"exit_code": proc.returncode, "parsed": parsed}


def classify(item_path: tuple[str, ...], item: dict, cfg: WalkawayConfig,
             registry: dict, governor_cfg: dict) -> dict:
    item_id = "/".join(item_path)
    note = item.get("note", "")
    action_type = item.get("action_type") or item.get("route")

    if is_isolated(item_path, cfg):
        return {"item_id": item_id, "decision": "DENY", "classification": "protected",
                "reason": "isolation_boundary"}

    if item.get("status") != "complete":
        return {"item_id": item_id, "decision": "SKIP", "classification": "not_yet_complete",
                "reason": "status_not_complete"}

    gate = protected_gate_match(item_id, note, action_type, governor_cfg)
    if gate:
        return {"item_id": item_id, "decision": "DENY", "classification": "protected",
                "reason": gate}

    # DEFAULT-DENY: unlike preflight_gate.py (which fails OPEN for an
    # unrecognized action_type), an item with no action_type mapped into
    # the safe_runner_registry is denied, not allowed.
    routes = registry.get("routes", {})
    if not action_type or action_type not in routes:
        return {"item_id": item_id, "decision": "DENY", "classification": "unclassified",
                "reason": "unclassified_action_type_default_deny"}

    pf = run_preflight(cfg, action_type)
    pf_parsed = pf["parsed"]
    if pf["exit_code"] != 0 or pf_parsed.get("allowed") is not True:
        return {"item_id": item_id, "decision": "DENY", "classification": "non_protected",
                "reason": f"preflight_denied:{pf_parsed.get('reasons')}", "preflight": pf_parsed}

    ok, fingerprint, reason = evidence_ok(item)
    if not ok:
        return {"item_id": item_id, "decision": "DENY", "classification": "non_protected",
                "reason": reason}

    return {"item_id": item_id, "decision": "ALLOW", "classification": "non_protected",
            "action_type": action_type, "evidence_fingerprint": fingerprint,
            "preflight": pf_parsed}


def idempotency_key(item_id: str, evidence_fingerprint: str) -> str:
    return hashlib.sha256(f"{item_id}|{evidence_fingerprint}".encode()).hexdigest()


def load_idempotency_store(cfg: WalkawayConfig) -> dict:
    if not cfg.idempotency_path.exists():
        return {}
    return load_json(cfg.idempotency_path)


def save_idempotency_store(cfg: WalkawayConfig, store: dict) -> None:
    cfg.idempotency_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.idempotency_path.write_text(json.dumps(store, indent=2, sort_keys=True))


def propose_worker(cfg: WalkawayConfig, action_type: str) -> dict:
    """Reuses safe_execution_router_v2b's scoring, but filters out any
    candidate under a vendored dependency tree -- the router map is known
    to contain grep-matched noise (e.g. numba/pydantic internals matched on
    substring "patch")."""
    if not cfg.router_v2b_path.exists() or not cfg.validated_runner_registry_path.exists():
        return {"selected_runner": None, "status": "router_infra_unavailable"}

    router = _load_module_from_path("safe_execution_router_v2b_reuse", cfg.router_v2b_path)
    router.VALIDATED = cfg.validated_runner_registry_path  # allow test/config override of hardcoded path
    selection = router.choose_validated_runner(action_type)  # type: ignore[attr-defined]
    ranked = selection.get("ranked_top") or []

    safe_ranked = [
        r for r in ranked
        if r.get("path") and not any(frag in r["path"] for frag in cfg.disallowed_candidate_fragments)
        and Path(r["path"]).exists()
    ]

    if not safe_ranked:
        return {"selected_runner": None, "status": "no_safe_worker_found_manual_dispatch_required",
                "raw_top_candidate": ranked[0] if ranked else None}

    return {"selected_runner": safe_ranked[0]["path"], "status": "proposed",
            "candidates_considered": len(ranked), "candidates_after_safety_filter": len(safe_ranked)}


def create_work_item(cfg: WalkawayConfig, item_id: str, item: dict, action_type: str,
                      routing: dict, evidence_fingerprint: str) -> Path:
    cfg.activated_tasks_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    payload = {
        "phase": "walkaway_governance_v1",
        "task_id": task_id,
        "source_priority_item": item_id,
        "task_type": action_type,
        "priority": "governed_dynamic",
        "mode": "safe_internal_execution",
        "external_actions": False,
        "public_output_blocked": True,
        "status": "activated_waiting_worker",
        "created_at_utc": now_iso(),
        "evidence_fingerprint": evidence_fingerprint,
        "routing_proposal": routing,
        "instructions": [
            "Read source priority item and its evidence.",
            "Create safe internal implementation plan.",
            "Do not perform external/public or protected action.",
            "Write result, receipt, and next recommended escalation.",
            "If real-world/public/protected execution is required, return to founder approval queue.",
        ],
    }
    out_path = cfg.activated_tasks_dir / f"{task_id}_{action_type}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path


def next_governing_priority(priority_state: dict, cfg: WalkawayConfig) -> dict | None:
    """Read-only: identifies the next eligible rank in canonical order.
    Never writes to priority_state -- selection is advisory/audit-only."""
    for rank_id, rank in priority_state.get("ranks", {}).items():
        if not isinstance(rank, dict):
            continue
        if is_isolated((rank_id,), cfg):
            continue
        if rank.get("status") in ("pending", "in_progress"):
            return {"rank_id": rank_id, "status": rank.get("status")}
    return None


def process_item(item_path: tuple[str, ...], item: dict, cfg: WalkawayConfig,
                  registry: dict, governor_cfg: dict, idem_store: dict) -> dict:
    decision = classify(item_path, item, cfg, registry, governor_cfg)
    record = {
        "event": "walkaway_governance_transition",
        "timestamp_utc": now_iso(),
        "actor": "walkaway_advance_v1",
        "item_id": decision["item_id"],
        "decision": decision["decision"],
        "classification": decision["classification"],
        "reason": decision.get("reason"),
        "previous_state": item.get("status"),
        "resulting_state": item.get("status"),  # completion is never written here
    }

    if decision["decision"] != "ALLOW":
        record["idempotency_key"] = None
        append_jsonl(cfg.ledger_path, record)
        return {**decision, "ledger_written": True, "work_item_path": None}

    key = idempotency_key(decision["item_id"], decision["evidence_fingerprint"])
    record["idempotency_key"] = key

    if key in idem_store:
        record["decision"] = "ALLOW_DUPLICATE_SKIPPED"
        record["note"] = "idempotent: already processed, no new work item created"
        append_jsonl(cfg.ledger_path, record)
        return {**decision, "decision": "ALLOW_DUPLICATE_SKIPPED", "ledger_written": True,
                "work_item_path": idem_store[key].get("work_item_path")}

    routing = propose_worker(cfg, decision["action_type"])
    work_item_path = create_work_item(
        cfg, decision["item_id"], item, decision["action_type"], routing,
        decision["evidence_fingerprint"],
    )

    record["worker_routing"] = routing
    record["work_item_path"] = str(work_item_path)
    append_jsonl(cfg.ledger_path, record)

    idem_store[key] = {"processed_at_utc": now_iso(), "work_item_path": str(work_item_path)}

    return {**decision, "ledger_written": True, "work_item_path": str(work_item_path),
            "worker_routing": routing}


def run_cycle(cfg: WalkawayConfig | None = None) -> dict:
    cfg = cfg or WalkawayConfig()
    priority_state = load_json(cfg.priority_state_path)
    registry = load_json(cfg.safe_runner_registry_path)
    governor_cfg = load_json(cfg.execution_governor_path)
    idem_store = load_idempotency_store(cfg)

    results = []
    for item_path, item in iter_candidate_items(priority_state, cfg):
        result = process_item(item_path, item, cfg, registry, governor_cfg, idem_store)
        results.append(result)

    save_idempotency_store(cfg, idem_store)

    return {
        "cycle_time_utc": now_iso(),
        "auto_completion_authority": "NOT_IMPLEMENTED",
        "post_completion_auto_advance_and_delegation": "IMPLEMENTED_NARROW_SCOPE",
        "items_evaluated": len(results),
        "items_allowed": sum(1 for r in results if r["decision"] == "ALLOW"),
        "items_denied": sum(1 for r in results if r["decision"] == "DENY"),
        "items_skipped_not_complete": sum(1 for r in results if r["decision"] == "SKIP"),
        "items_duplicate_skipped": sum(1 for r in results if r["decision"] == "ALLOW_DUPLICATE_SKIPPED"),
        "next_governing_priority": next_governing_priority(priority_state, cfg),
        "results": results,
    }


def main():
    report = run_cycle()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
