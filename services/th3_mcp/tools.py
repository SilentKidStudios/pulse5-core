"""
TH3S1L3NTK1D Studios MCP -- safe read-mostly tool implementations.

Doctrine (enforced here, not just documented):
  * Every tool is READ-ONLY against canonical state, except council_post_result,
    which only ever appends a bounded signal file into the EXISTING, already-live
    mr_silent_spine/division_signal_bus/inbox/ pipeline (consumed by the existing
    mrsilent-division-bus-consumer.service loop). No tool here deletes, mutates
    credentials, promotes to production, or runs arbitrary shell.
  * BLEND_NOT_REPLACE: this reads/writes the SAME canonical files the rest of the
    Studio already uses. It does not create a second registry or a second bus.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/pulse5-core")
sys.path.insert(0, str(ROOT / "omnisim" / "api"))
import request_scenario as _omnisim_scenario  # noqa: E402
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))
import job_ledger  # noqa: E402
import studio_router  # noqa: E402
import capability_registry  # noqa: E402
import authority_policy  # noqa: E402
STATE = ROOT / "mr_silent_spine" / "state"
BUS_INBOX = ROOT / "mr_silent_spine" / "division_signal_bus" / "inbox"
BUS_RECEIPTS = ROOT / "mr_silent_spine" / "division_signal_bus" / "receipts"
OMNIREGISTRY_TRUTH = ROOT / "governance" / "state" / "omniregistry_truth.json"
OMNISIM_LOOP_REPORT = ROOT / "omnisim" / "reports" / "loop_last_run.json"

# Fixed, hardcoded unit set for runtime_health() -- never caller-controlled,
# so this can never become an "arbitrary systemctl query" capability.
_HEALTH_UNITS = (
    "th3-mcp-http.service",
    "founder-free-studio-runtime.service",
    "founder-free-studio-runtime.path",
    "founder-free-studio-runtime.timer",
    "mr-silent-awareness-loop.service",
)
_TERMINAL_JOB_STATES = {"completed", "failed", "escalated"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def studio_status() -> dict:
    """High-level, real, read-only snapshot of Studio state. No arbitrary execution."""
    queue = _read_json(STATE / "founder_top10_priority_queue.json", {})
    priority_state = _read_json(STATE / "founder_priority_state.json", {})
    omnisim_run = _read_json(OMNISIM_LOOP_REPORT, {})

    ranks = queue.get("durable_priority_queue", [])
    top_rank = ranks[0] if ranks else None
    top_rank_status = None
    if top_rank:
        top_rank_status = (priority_state.get("ranks", {}).get(top_rank.get("id"), {}) or {}).get("status")

    return {
        "canonical_authority": "pulse5-core-01",
        "engineering_compute_only": "render-forge-01",
        "governing_rank_id": top_rank.get("id") if top_rank else None,
        "governing_rank_status": top_rank_status,
        "priority_queue_length": len(ranks),
        "omnisim_last_loop_status": omnisim_run.get("status"),
        "omnisim_last_loop_at_utc": omnisim_run.get("ran_at"),
        "generated_at_utc": _now(),
        "source": "th3_mcp.tools.studio_status (reads live canonical state, no synthetic data)",
    }


def priority_status() -> dict:
    """Full current Founder Top-N durable priority queue, as literally stored."""
    queue = _read_json(STATE / "founder_top10_priority_queue.json", {})
    priority_state = _read_json(STATE / "founder_priority_state.json", {})
    ranks = queue.get("durable_priority_queue", [])
    state_by_id = priority_state.get("ranks", {})
    merged = []
    for r in ranks:
        entry = dict(r)
        entry["truth_state"] = state_by_id.get(r.get("id"), {}).get("status", "unknown")
        merged.append(entry)
    return {
        "queue_source": "mr_silent_spine/state/founder_top10_priority_queue.json (projection of ACTION-priority-governor-20260827)",
        "ranks": merged,
        "generated_at_utc": _now(),
    }


def registry_search(query: str, limit: int = 10) -> dict:
    """Keyword search over the real OmniRegistry truth-layer project list. Read-only."""
    truth = _read_json(OMNIREGISTRY_TRUTH, {"projects": []})
    q_words = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    hits = []
    for p in truth.get("projects", []):
        words = set(p.get("fingerprint_words", []))
        name_words = set(re.findall(r"[a-z0-9]+", p.get("project_name", "").lower()))
        overlap = q_words & (words | name_words)
        if overlap or not q_words:
            hits.append({
                "project_name": p.get("project_name"),
                "primary_division": p.get("primary_division"),
                "kind": p.get("kind"),
                "matched_words": sorted(overlap),
            })
    hits.sort(key=lambda h: len(h["matched_words"]), reverse=True)
    return {
        "query": query,
        "result_count": min(len(hits), limit),
        "results": hits[:limit],
        "source": "governance/state/omniregistry_truth.json",
        "generated_at_utc": _now(),
    }


def request_simulation(question: str, assumptions: list | None = None, options: list | None = None) -> dict:
    """Submit a structured scenario to OmniSim and get back a labeled estimate
    (assumptions, scored options, uncertainty note, recommendation boundaries,
    receipt path). Read/write bounded to omnisim/'s own directories."""
    return _omnisim_scenario.request_scenario(question, assumptions, options)


def council_post_result(source_label: str, in_reply_to: str, summary: str, payload: dict) -> dict:
    """
    Write a bounded result signal into the EXISTING, already-live
    mr_silent_spine/division_signal_bus/inbox/ pipeline. Does not invent a new bus.
    The live mrsilent-division-bus-consumer.service loop (30s cycle) picks this up,
    moves it to processed/, and writes a receipt under division_signal_bus/receipts/.
    This is the only write-capable tool in this module, and it can only ever append
    one bounded, schema-matching signal file -- it cannot touch any other path.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    signal_id = uuid.uuid4().hex[:16]
    signal = {
        "source_division": source_label or "CT_MCP_BRIDGE",
        "signal_type": "ct_mcp_result",
        "priority": "system",
        "message": summary,
        "in_reply_to": in_reply_to,
        "payload": payload,
        "created_at_utc": _now(),
        "expected_route": "MR. SILENT Core",
    }
    BUS_INBOX.mkdir(parents=True, exist_ok=True)
    out_path = BUS_INBOX / f"signal_ct_mcp_bridge_{signal_id}.json"
    out_path.write_text(json.dumps(signal, indent=2))
    return {
        "written_to": str(out_path),
        "signal_id": signal_id,
        "note": "Consumed by the existing mrsilent-division-bus-consumer.service loop (~30s cycle); receipt will appear under mr_silent_spine/division_signal_bus/receipts/",
        "generated_at_utc": _now(),
    }


# ---------------------------------------------------------------------------
# Read-only CT / MR. SILENT control-plane tools (all reuse canonical sources
# below; none create a job, queue entry, audit record, or capability-gap
# record). No submit/cancel/execute action exists in this module.
#
#   CANONICAL_WORK_INTAKE / CANONICAL_WORK_QUEUE = mrsilent_bridge/jobs/<id>/
#       (mrsilent_bridge/job_ledger.py: incremental, atomic, crash-safe)
#   CANONICAL_PERMISSION_CLASSIFIER = mrsilent_bridge/authority_policy.py
#   CANONICAL_WORKER_ROUTER = mrsilent_bridge/studio_router.py
#   CANONICAL_PRIORITY_GOVERNOR = mr_silent_spine/autonomous_exec/
#       founder_priority_governor.py, projected into
#       mr_silent_spine/state/founder_{top10_priority_queue,priority_state}.json
#   CANONICAL_RUNTIME = th3-mcp-http.service (this process) +
#       founder-free-studio-runtime.{service,path,timer} +
#       mr-silent-awareness-loop.service (all read via systemctl show, fixed
#       unit list only)
#   CANONICAL_EVIDENCE_SOURCE = job_ledger records themselves
#       (result.json / validation.json / ledger.json under each job dir)
# ---------------------------------------------------------------------------


def work_status(work_id: str) -> dict:
    """Read-only lookup of one work item, by id, against canonical state.
    Tries the engineering job ledger first, then the Founder Top-10 priority
    ranks. Returns found=False rather than fabricating a result."""
    record = job_ledger.load(work_id)
    if record is not None:
        return {
            "found": True,
            "source": "mrsilent_bridge.job_ledger",
            "work_id": record.job_id,
            "task": (record.task or "")[:500],
            "requested_by": record.requested_by,
            "state": record.state,
            "risk_class": record.risk_class,
            "approval_state": record.approval_state,
            "authority_state": record.authority_state,
            "selected_engine": record.selected_engine,
            "provider": record.provider,
            "promotion_eligible": record.promotion_eligible,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "heartbeat": record.heartbeat,
            "terminal_result": record.terminal_result,
            "error_class": record.error_class,
            "lock_status": job_ledger.lock_status(work_id),
            "generated_at_utc": _now(),
        }

    queue = _read_json(STATE / "founder_top10_priority_queue.json", {})
    priority_state = _read_json(STATE / "founder_priority_state.json", {})
    ranks_by_id = {r.get("id"): r for r in queue.get("durable_priority_queue", [])}
    rank = ranks_by_id.get(work_id)
    if rank is not None:
        rank_state = priority_state.get("ranks", {}).get(work_id, {})
        return {
            "found": True,
            "source": "founder_top10_priority_queue.json + founder_priority_state.json",
            "work_id": work_id,
            "classification": "founder_priority_rank",
            "rank": rank.get("rank"),
            "note": rank.get("note"),
            "state": rank_state.get("status", "unknown"),
            "generated_at_utc": _now(),
        }

    return {"found": False, "work_id": work_id, "generated_at_utc": _now()}


def active_work(limit: int = 20) -> dict:
    """Read-only bounded list of canonical queued/active/deferred/blocked work:
    non-terminal engineering jobs from the job ledger, plus Founder Top-10
    priority ranks not yet marked complete."""
    limit = max(1, min(int(limit), 100))
    records = job_ledger.list_all()
    non_terminal = [r for r in records if r.state not in _TERMINAL_JOB_STATES]
    non_terminal.sort(key=lambda r: r.updated_at or r.created_at or "", reverse=True)
    jobs_out = [
        {
            "work_id": r.job_id,
            "task": (r.task or "")[:200],
            "state": r.state,
            "risk_class": r.risk_class,
            "approval_state": r.approval_state,
            "requested_by": r.requested_by,
            "updated_at": r.updated_at,
        }
        for r in non_terminal[:limit]
    ]

    queue = _read_json(STATE / "founder_top10_priority_queue.json", {})
    priority_state = _read_json(STATE / "founder_priority_state.json", {})
    state_by_id = priority_state.get("ranks", {})
    priority_out = [
        {
            "work_id": r.get("id"),
            "rank": r.get("rank"),
            "status": state_by_id.get(r.get("id"), {}).get("status", "unknown"),
        }
        for r in queue.get("durable_priority_queue", [])
        if state_by_id.get(r.get("id"), {}).get("status") != "complete"
    ]

    return {
        "engineering_jobs": {
            "total_non_terminal": len(non_terminal),
            "returned": len(jobs_out),
            "items": jobs_out,
            "source": "mrsilent_bridge.job_ledger.list_all()",
        },
        "founder_priority_ranks_not_complete": {
            "items": priority_out,
            "source": "founder_top10_priority_queue.json + founder_priority_state.json",
        },
        "generated_at_utc": _now(),
    }


def route_preview(task_type: str, task_description: str = "", requested_tools: list | None = None) -> dict:
    """PURE DRY RUN. Shows how studio_router would rank candidates for
    task_type (studio_router.rank() is documented as side-effect-free -- no
    job, audit entry, or capability-gap record is created here) plus which
    authority_policy gating constants the task description/tools would trip.
    This is a preview using the same gating constants classify() uses, not a
    literal classify() call (that requires a real job sandbox to mean
    anything) -- never presented as a guaranteed final verdict."""
    requested_tools = set(requested_tools or [])
    open_evals = studio_router.rank(task_type, allow_founder_gated=False)
    all_evals = studio_router.rank(task_type, allow_founder_gated=True)

    open_accepted_ids = {e.capability_id for e in open_evals if e.accepted}
    all_accepted = [e for e in all_evals if e.accepted]
    founder_only = [e for e in all_accepted if e.capability_id not in open_accepted_ids]

    if open_accepted_ids:
        proposed = next(e for e in all_evals if e.capability_id in open_accepted_ids)
        founder_approval_would_unlock_a_candidate = False
    elif founder_only:
        proposed = founder_only[0]
        founder_approval_would_unlock_a_candidate = True
    else:
        proposed = None
        founder_approval_would_unlock_a_candidate = None  # true capability gap, not an approval question

    gated_tools_hit = sorted(requested_tools & authority_policy.GATED_TOOLS)
    keyword_hits = sorted({p.pattern for p in authority_policy.GATED_KEYWORDS if p.search(task_description or "")})

    return {
        "dry_run": True,
        "task_type": task_type,
        "candidates_open_now": [asdict(e) for e in open_evals],
        "candidates_if_founder_approved": [asdict(e) for e in all_evals],
        "proposed_capability_id": proposed.capability_id if proposed else None,
        "proposed_reason": proposed.reason if proposed else "no eligible capability for this task_type (capability gap)",
        "capability_gap": proposed is None,
        "founder_approval_would_unlock_a_candidate": founder_approval_would_unlock_a_candidate,
        "gated_tools_requested": gated_tools_hit,
        "task_description_matched_gated_keywords": keyword_hits,
        "generated_at_utc": _now(),
        "note": "No job, queue entry, execution, or approval item was created by this call.",
    }


def capability_status() -> dict:
    """Read-only inventory of the real Studio capability registry (which
    entries are actually invocable vs. discovery-only), for routing context."""
    entries = capability_registry.load()
    out = [
        {
            "capability_id": e.get("capability_id"),
            "task_types": e.get("task_types"),
            "status": e.get("status"),
            "availability": e.get("availability"),
            "risk_class": e.get("risk_class"),
            "locality": e.get("locality"),
            "cost_class": e.get("cost_class"),
            "quota_status": e.get("quota_status"),
            "validation_method": e.get("validation_method"),
        }
        for e in entries
    ]
    return {
        "total_entries": len(out),
        "capabilities": out,
        "note": "Only entries with a real registered adapter are ever selectable by route_preview/studio_router; others are discovery-only, never silently invented.",
        "source": "mrsilent_bridge/registry_data/capabilities.json via capability_registry.load()",
        "generated_at_utc": _now(),
    }


def _unit_state(unit: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "Result"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        props = dict(line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line)
        return {"unit": unit, **props}
    except Exception as exc:
        return {"unit": unit, "error": str(exc)}


def runtime_health() -> dict:
    """Read-only compact health of the persistent MR. SILENT runtime pieces
    and the engineering job queue. Fixed, hardcoded unit list only -- this
    tool exposes no ability to query arbitrary systemd units."""
    units = [_unit_state(u) for u in _HEALTH_UNITS]
    records = job_ledger.list_all()
    non_terminal = [r for r in records if r.state not in _TERMINAL_JOB_STATES]
    stale = [r.job_id for r in non_terminal if job_ledger.is_stale(r)]
    return {
        "systemd_units": units,
        "engineering_job_queue": {
            "total_jobs_on_disk": len(records),
            "non_terminal": len(non_terminal),
            "stale_non_terminal_job_ids": stale,
        },
        "note": "Fixed unit set only; no arbitrary systemctl capability is exposed.",
        "generated_at_utc": _now(),
    }


def continuum_status() -> dict:
    """Read-only CT-facing aggregate composed entirely from the canonical
    sources above (studio_status, active_work, runtime_health, job_ledger).
    Creates no new truth store."""
    status = studio_status()
    aw = active_work(limit=10)
    health = runtime_health()

    records = job_ledger.list_all()
    completed = [r for r in records if r.state == "completed"]
    completed.sort(key=lambda r: r.updated_at or r.created_at or "", reverse=True)
    latest_completed = None
    if completed:
        r = completed[0]
        latest_completed = {"work_id": r.job_id, "task": (r.task or "")[:200], "updated_at": r.updated_at}

    pending_gated = [
        {"work_id": r.job_id, "task": (r.task or "")[:200], "risk_class": r.risk_class}
        for r in records
        if r.approval_state == "pending_approval"
    ][:10]

    ranks_not_complete = sorted(
        aw["founder_priority_ranks_not_complete"]["items"],
        key=lambda x: (x.get("rank") is None, x.get("rank")),
    )
    next_actionable = ranks_not_complete[0] if ranks_not_complete else None

    return {
        "canonical_authority": status.get("canonical_authority"),
        "governing_priority": {
            "rank_id": status.get("governing_rank_id"),
            "status": status.get("governing_rank_status"),
        },
        "active_work_summary": {
            "engineering_jobs_non_terminal": aw["engineering_jobs"]["total_non_terminal"],
            "priority_ranks_not_complete": len(aw["founder_priority_ranks_not_complete"]["items"]),
        },
        "latest_completed_job": latest_completed,
        "pending_founder_gated_jobs": pending_gated,
        "runtime_health_summary": {
            "units": [{"unit": u["unit"], "ActiveState": u.get("ActiveState")} for u in health["systemd_units"]],
            "stale_non_terminal_job_ids": health["engineering_job_queue"]["stale_non_terminal_job_ids"],
        },
        "next_actionable_ordinary_work": next_actionable,
        "external_blockers": [],
        "generated_at_utc": _now(),
        "note": "Aggregation of existing canonical sources only; no new authority or truth store.",
    }
