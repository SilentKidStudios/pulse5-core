"""
TH3S1L3NTK1D Studios MCP -- safe read-mostly tool implementations.

Doctrine (enforced here, not just documented):
  * Every tool is READ-ONLY against canonical state, except council_post_result
    (appends a bounded signal file into the EXISTING, already-live
    mr_silent_spine/division_signal_bus/inbox/ pipeline) and submit_work/
    cancel_work, whose only write is creating/advancing one record in the
    EXISTING mrsilent_bridge self-evolution Proposal pipeline
    (evolution/proposal.py). No tool here deletes, mutates credentials,
    promotes to production, or runs arbitrary shell.
  * BLEND_NOT_REPLACE: this reads/writes the SAME canonical files the rest of the
    Studio already uses. It does not create a second registry, bus, queue,
    governor, or approval system.
  * submit_work NEVER executes anything synchronously in this process. It only
    creates a Proposal; the only thing that ever advances a Proposal is the
    already-live, already-Founder-authorized mrsilent-autonomous-cycle.timer
    (every 15 min, and only for risk_score=="low"). Whatever risk_score this
    module assigns, the real execution engines independently re-run
    authority_policy.classify() against the real task text before anything
    runs (see bridge.py/omniengineer_harness.py) -- this module's own
    keyword/capability scan is a first-pass, defense-in-depth filter, not the
    sole safety gate.
"""

from __future__ import annotations

import hashlib
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
sys.path.insert(0, str(ROOT / "divisions" / "omnioracle" / "api"))
import request_oracle as _omnioracle_forecast  # noqa: E402
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))
import job_ledger  # noqa: E402
import studio_router  # noqa: E402
import capability_registry  # noqa: E402
import authority_policy  # noqa: E402
import secret_path_policy  # noqa: E402
from evolution import proposal as proposal_mod  # noqa: E402
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


def request_oracle(domain: str, question: str | None = None, mode: str = "read", seed=None,
                    min_evidence: int = 0, also_run_omnisim_scenario: str | None = None) -> dict:
    """Submit a structured forecast/consensus request to Omni Oracle and get back a
    labeled result: real evidence-artifact counts, a synthetic_projection (deterministic,
    evidence-derived, explicitly labeled provenance='synthetic' -- never presented as a
    calibrated real-world prediction), live_signals (real local-machine telemetry where
    applicable, provenance='live'), an immutable forecast_id a later real outcome can be
    recorded against, and a durable receipt. mode='read' (default) only aggregates
    evidence already on disk; mode='generate' also triggers a fresh deterministic pipeline
    cycle (scenario_branch_generator -> recursive_simulation_engine ->
    consensus_intelligence_layer -> probabilistic_forecast_engine), reproducible given the
    same seed. also_run_omnisim_scenario, if given, also calls OmniSim's own
    request_scenario() and includes it as a separate, clearly-bounded ensemble result.
    Read/write bounded to divisions/omnioracle/'s own directories."""
    return _omnioracle_forecast.request_oracle(
        domain, question, mode=mode, seed=seed, min_evidence=min_evidence,
        also_run_omnisim_scenario=also_run_omnisim_scenario,
    )


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


# Root-cause fix (canonical engineering capability route repair): a
# Founder/ChatGPT-facing caller naturally says "engineering" for ordinary
# sandboxed coding work, but that string has NEVER been a registered
# task_type in registry_data/capabilities.json (52 real task_types exist;
# the actual one is "code_edit", and it is also the ONLY task_type
# evolution/advance.py._implementation_router() ever routes to at real
# execution time -- hardcoded, since this proposal pipeline only ever
# implements sandboxed file/code prototypes). This is a pure external-facing
# naming normalization, not a routing/authority/resource-policy change: no
# capability is registered for anything it can't do, no new task_type is
# invented, and anything NOT in this short, explicit alias list still falls
# through to the existing default-deny in _first_pass_classify unchanged.
_TASK_TYPE_ALIASES = {
    "engineering": "code_edit",
    "engineer": "code_edit",
    "software_engineering": "code_edit",
    "coding": "code_edit",
    "code": "code_edit",
    "development": "code_edit",
    "dev": "code_edit",
    "implementation": "code_edit",
}


def _normalize_task_type(task_type: str) -> tuple[str, bool]:
    """(canonical_task_type, was_normalized). Unknown/unrecognized values
    pass through completely unchanged -- this only repairs the specific,
    proven naming mismatch, it does not guess at arbitrary input."""
    if not task_type:
        return task_type, False
    key = task_type.strip().lower().replace(" ", "_").replace("-", "_")
    canonical = _TASK_TYPE_ALIASES.get(key)
    if canonical and canonical != task_type:
        return canonical, True
    return task_type, False


def route_preview(task_type: str, task_description: str = "", requested_tools: list | None = None, paid_resources_allowed: bool = True) -> dict:
    """PURE DRY RUN. Shows how studio_router would rank candidates for
    task_type (studio_router.rank() is documented as side-effect-free -- no
    job, audit entry, or capability-gap record is created here) plus which
    authority_policy gating constants the task description/tools would trip.
    This is a preview using the same gating constants classify() uses, not a
    literal classify() call (that requires a real job sandbox to mean
    anything) -- never presented as a guaranteed final verdict.

    paid_resources_allowed is the SAME hard eligibility filter submit_work
    uses (studio_router.rank(allow_paid=...)) -- when False, a metered/paid
    candidate never appears as accepted here either, so this preview and the
    real live router can never disagree on the hard policy, only on
    duty-cycle/health-based ordering among whatever remains eligible (which
    can legitimately shift between calls)."""
    requested_tools = set(requested_tools or [])
    submitted_task_type = task_type
    task_type, was_normalized = _normalize_task_type(task_type)
    open_evals = studio_router.rank(task_type, allow_founder_gated=False, allow_paid=paid_resources_allowed)
    all_evals = studio_router.rank(task_type, allow_founder_gated=True, allow_paid=paid_resources_allowed)

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

    no_eligible_free_local_route = False
    if proposed is None and not paid_resources_allowed:
        # Distinguish "genuinely no capability for this task_type" from
        # "capability exists, but only via a paid provider policy excludes".
        unrestricted = studio_router.rank(task_type, allow_founder_gated=True, allow_paid=True)
        no_eligible_free_local_route = any(e.accepted for e in unrestricted)

    gated_tools_hit = sorted(requested_tools & authority_policy.GATED_TOOLS)
    keyword_hits = sorted({p.pattern for p in authority_policy.GATED_KEYWORDS if p.search(task_description or "")})

    return {
        "dry_run": True,
        "task_type_submitted": submitted_task_type,
        "task_type": task_type,
        "task_type_was_normalized": was_normalized,
        "paid_resources_allowed": paid_resources_allowed,
        "candidates_open_now": [asdict(e) for e in open_evals],
        "candidates_if_founder_approved": [asdict(e) for e in all_evals],
        "proposed_capability_id": proposed.capability_id if proposed else None,
        "proposed_reason": proposed.reason if proposed else "no eligible capability for this task_type (capability gap)",
        "capability_gap": proposed is None,
        "no_eligible_free_local_route": no_eligible_free_local_route,
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


# ---------------------------------------------------------------------------
# Governed execution bridge: submit_work / work_result / request_founder_decision
# / cancel_work. All four operate ONLY on the existing self-evolution Proposal
# pipeline (mrsilent_bridge/evolution/proposal.py + evolution/advance.py) --
# the one real, already-live, already-Founder-authorized (2026-08-17) async
# execution path this project has (mrsilent-autonomous-cycle.timer, every 15
# min, auto-advances only risk_score=="low" proposals, and only ever as far
# as PROMOTION_CANDIDATE -- real promotion always requires a separate human
# `cli.py promote --founder-approved`). No new queue, governor, scheduler, or
# approval system is created here.
# ---------------------------------------------------------------------------


def _first_pass_classify(objective: str, task_type: str, context: str, requested_capabilities: list[str], paid_resources_allowed: bool, source_paths: list[str] | None = None) -> tuple[str, list[str]]:
    """Conservative, defense-in-depth first-pass risk_score for a submit_work
    request, using the SAME gating constants authority_policy.classify() uses
    (not a substitute for it -- the real execution engines independently
    re-run classify() against the real task text before anything runs; this
    only decides whether the proposal is even ELIGIBLE for the existing
    auto-advance timer to look at). Defaults to founder_gated whenever
    anything is ambiguous, per 'default deny if classification cannot be
    established safely.'

    paid_resources_allowed is a HARD eligibility filter (studio_router.rank(
    allow_paid=...)), not a soft score -- when False and the only capable
    route for task_type is a paid/metered one, this returns the distinct
    'no_eligible_free_local_route' classification instead of silently
    falling through to founder_gated-then-maybe-approved-into-paid.

    source_paths gets the SAME first-pass keyword scan as objective/context
    (defense in depth only) -- the real, authoritative check is
    authority_policy.classify()'s own source_paths loop against
    GATED_PATH_MARKERS, independently re-run at real execution time inside
    omniengineer_harness.submit_job_auto() (see evolution/advance.py's
    _run_omni_engineer()); this first pass cannot and does not substitute for it."""
    combined_text = " ".join(
        [objective or "", str(context or ""), task_type or "", " ".join(requested_capabilities), " ".join(source_paths or [])]
    )

    keyword_hits = sorted({p.pattern for p in authority_policy.GATED_KEYWORDS if p.search(combined_text)})
    if keyword_hits:
        return "founder_gated", [f"objective/context/source_paths matched gated keyword pattern(s): {keyword_hits}"]

    # source_paths arrives here already resolved+ROOT-joined by
    # _normalize_source_path -- an out-of-ROOT path (e.g. a caller-supplied
    # already-absolute path like /etc/shadow, or a symlink that resolved
    # outside the repo) must still never pass, regardless of marker matching.
    out_of_root_hits = [path for path in (source_paths or []) if not secret_path_policy.is_within_root(Path(path))]
    if out_of_root_hits:
        return "founder_gated", [f"source_paths resolve outside the canonical repository root ({secret_path_policy.ROOT}): {out_of_root_hits}"]

    path_marker_hits = sorted(
        {marker for path in (source_paths or []) for marker in authority_policy.GATED_PATH_MARKERS if marker.lower() in path.lower()}
    )
    if path_marker_hits:
        return "founder_gated", [f"source_paths touched protected marker(s): {path_marker_hits}"]

    gated_capability_hits = sorted(set(requested_capabilities) & authority_policy.GATED_TOOLS)
    if gated_capability_hits:
        return "founder_gated", [f"requested capability/tool(s) always gated: {gated_capability_hits}"]

    if not task_type:
        return "founder_gated", ["no task_type supplied -- default deny on ambiguous/unclassifiable work"]

    matches = studio_router.rank(task_type, allow_founder_gated=False, allow_paid=paid_resources_allowed)
    if any(m.accepted for m in matches):
        return "low", [f"no gated keyword/tool match; an eligible capability exists for task_type={task_type!r} within the paid_resources_allowed={paid_resources_allowed} constraint"]

    if not paid_resources_allowed:
        matches_if_paid = studio_router.rank(task_type, allow_founder_gated=True, allow_paid=True)
        if any(m.accepted for m in matches_if_paid):
            return "no_eligible_free_local_route", [
                f"a capable engine exists for task_type={task_type!r} but only via a paid/metered provider, "
                f"and paid_resources_allowed=False -- NEVER silently falling through to a paid engine"
            ]

    return "founder_gated", [f"no eligible non-gated capability currently registered for task_type={task_type!r} -- default deny rather than guess a route"]


def _normalize_source_path(raw: str) -> str:
    """Resolve a caller-supplied source_path to an ABSOLUTE path rooted at ROOT
    (/opt/pulse5-core) before it ever reaches Proposal.source_paths.

    ROOT-CAUSE NOTE (found via this campaign's own live canary, 2026-09-02):
    mrsilent-autonomous-cycle.service's WorkingDirectory is
    /opt/pulse5-core/mrsilent_bridge, NOT /opt/pulse5-core -- so a caller-natural
    repo-relative path like "mrsilent_bridge/context_staging.py" silently resolves
    to a NONEXISTENT path when omniengineer_harness.py's `Path(p).is_file()` check
    runs inside that service, and the file is never staged (no error, no
    exclusion record -- it just silently doesn't copy, since neither is_dir() nor
    is_file() matches). Confirmed by direct observation: a canary submitted with a
    bare relative path produced a sandbox the agent itself described as empty.
    Normalizing here, at the one shared MCP-facing entry point, fixes this for
    every caller regardless of which service's cwd ends up evaluating it later --
    an already-absolute path (or one starting with ROOT) passes through
    unchanged (modulo resolution, see below), exactly preserving the prior
    behavior for any caller that already knew to supply one. The 500-char
    bound is applied to the FINAL (possibly ROOT-prefixed) string, not the
    raw input, so a caller-supplied relative path can't exceed it once
    normalized -- a truncated result simply fails is_file()/is_dir()
    harmlessly downstream rather than resolving to something unintended.

    TRAVERSAL-SAFETY HARDENING (2026-09-02, SECRET / CREDENTIAL SOURCE-STAGING
    HARDENING campaign): also resolves '..'/'.' segments and symlinks via
    secret_path_policy.resolve_path(), so e.g. "mrsilent_bridge/../secure_keys/
    anthropic.key" or a symlink like .env.central (-> /etc/pulse5.env) can't
    reach a marker-matching bypass or escape ROOT by construction -- every
    downstream marker/boundary check (here, in _first_pass_classify, in
    authority_policy.classify(), and in context_staging) operates on this same
    canonical, already-resolved string."""
    text = str(raw)
    p = Path(text)
    joined = text if p.is_absolute() else str(ROOT / text)
    resolved = str(secret_path_policy.resolve_path(joined))
    return resolved[:500]


def submit_work(
    objective: str,
    task_type: str = "",
    context: str = "",
    priority_hint: str = "",
    requested_capabilities: list | None = None,
    idempotency_key: str | None = None,
    paid_resources_allowed: bool = False,
    source_paths: list | None = None,
) -> dict:
    """Accepts a structured Studio objective and creates exactly one canonical
    Proposal (evolution/proposal.py) if -- and only if -- a safe classification
    can be established. Never executes anything synchronously: advancement
    only ever happens via the existing mrsilent-autonomous-cycle.timer, and
    only for risk_score=='low'. Does not accept and cannot be handed
    founder_approved=True -- there is no such parameter.

    paid_resources_allowed defaults to False (safe-by-default for this
    external-facing entry point). It is persisted onto the Proposal and
    re-enforced as a HARD filter at real execution time by
    evolution/advance.py._implementation_router() -> studio_router.rank(
    allow_paid=...) -- so even if availability changes between submission
    and the timer picking it up 15 minutes later, a paid engine can never be
    silently selected for a proposal submitted with paid_resources_allowed=False.

    source_paths (SCHEMA PROPAGATION FIX, 2026-09-02): a bounded list (<=10
    entries, <=500 chars each, normalized to absolute paths under ROOT via
    _normalize_source_path -- see its docstring for the real cwd-mismatch bug
    this closes) of real canonical repository paths this Proposal is
    explicitly authorized to have staged into its engineering sandbox, if and
    when it is advanced. This is NOT new backend behavior -- Proposal.
    source_paths, evolution/advance.py's threading of it into
    omniengineer_harness.submit_job_auto(), and authority_policy.classify()'s
    GATED_PATH_MARKERS rejection of it already exist and are already
    Founder-authorized (GOD_MODE_V1 FINAL GAP CLOSURE / GOVERNED CANONICAL
    SOURCE STAGING REPAIR); this MCP-facing tool forwards the caller's
    (normalized) list onto the same, already-governed Proposal field every
    other creation path uses. Every source_path also gets the same first-pass
    gated-keyword and GATED_PATH_MARKERS scan objective/context already get
    (defense in depth only) -- the real, authoritative rejection is
    authority_policy.classify()'s own source_paths check, independently
    re-run at real execution time regardless of what this first pass finds.
    Defaults to [] (byte-for-byte unchanged behavior for every existing
    caller that doesn't pass it)."""
    objective = (objective or "").strip()
    if not objective:
        return {"accepted": False, "reason": "objective is required", "generated_at_utc": _now()}

    requested_capabilities = [str(c) for c in (requested_capabilities or [])][:20]
    source_paths = [_normalize_source_path(p) for p in (source_paths or [])][:10]
    submitted_task_type = task_type
    task_type, task_type_was_normalized = _normalize_task_type(task_type)

    fingerprint_source = idempotency_key.strip() if idempotency_key else objective.lower()
    fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()[:16]

    existing = proposal_mod.find_open_by_fingerprint(fingerprint)
    if existing is not None:
        return {
            "accepted": True,
            "work_id": existing.proposal_id,
            "classification": existing.risk_score,
            "status": existing.status,
            "idempotent_reuse": True,
            "note": "An open proposal with this same objective/idempotency_key already exists; returning it instead of creating a duplicate.",
            "generated_at_utc": _now(),
        }

    risk_score, reasons = _first_pass_classify(objective, task_type, context, requested_capabilities, paid_resources_allowed, source_paths)

    if risk_score == "no_eligible_free_local_route":
        return {
            "accepted": False,
            "reason": "NO_ELIGIBLE_FREE_LOCAL_ROUTE",
            "classification_reasons": reasons,
            "routing": {"task_type_submitted": submitted_task_type, "task_type": task_type, "task_type_was_normalized": task_type_was_normalized, "requested_capabilities": requested_capabilities, "paid_resources_allowed": paid_resources_allowed, "source_paths": source_paths},
            "note": "No Proposal was created. A capable engine exists for this task_type but only via a paid provider; resubmit with paid_resources_allowed=true if that's acceptable, or wait for a free/local capability to become available.",
            "generated_at_utc": _now(),
        }

    p = proposal_mod.create(
        observed_weakness=f"[CT/ChatGPT submit_work] {objective}"[:2000],
        proposed_upgrade=(str(context) if context else objective)[:4000],
        risk_score=risk_score,
        origin="ct_mcp_bridge",
        fingerprint=fingerprint,
        paid_resources_allowed=paid_resources_allowed,
        source_paths=source_paths or None,
    )

    return {
        "accepted": True,
        "work_id": p.proposal_id,
        "classification": risk_score,
        "classification_reasons": reasons,
        "founder_approval_required": risk_score != "low",
        "status": p.status,
        "routing": {"task_type_submitted": submitted_task_type, "task_type": task_type, "task_type_was_normalized": task_type_was_normalized, "requested_capabilities": requested_capabilities, "priority_hint": priority_hint or None, "paid_resources_allowed": paid_resources_allowed, "source_paths": source_paths},
        "execution_path": (
            "Will be auto-advanced by the existing mrsilent-autonomous-cycle.timer (15-min cadence) -- capped at PROMOTION_CANDIDATE, never auto-promoted to real files."
            if risk_score == "low" else
            "NOT auto-advanced (risk_score != 'low'). A human must review and advance it via the existing CLI."
        ),
        "idempotent_reuse": False,
        "generated_at_utc": _now(),
        "note": "This created a Proposal in the existing self-evolution pipeline; nothing was executed by this call. The real execution engines independently re-classify the actual task text before anything runs, regardless of the risk_score recorded here.",
    }


def work_result(work_id: str) -> dict:
    """Read-only. Reports the real, current lifecycle state of a submit_work
    proposal: objective, status, classification, latest implementation
    attempt if any (selected engine, validation/canary result), approval
    state, and history. Never synthesizes a result that isn't on disk."""
    try:
        p = proposal_mod.load(work_id)
    except Exception:
        return {"found": False, "work_id": work_id, "generated_at_utc": _now()}

    latest_job = None
    if p.implementation_job_ids:
        record = job_ledger.load(p.implementation_job_ids[-1])
        if record is not None:
            latest_job = {
                "job_id": record.job_id,
                "state": record.state,
                "selected_engine": record.selected_engine,
                "provider": record.provider,
                "promotion_eligible": record.promotion_eligible,
                "validation_result": record.validation_result,
                "canary_result": record.canary_result,
                "terminal_result": record.terminal_result,
                "error_class": record.error_class,
                "updated_at": record.updated_at,
            }

    if p.status == proposal_mod.ProposalStatus.PROMOTED:
        approval_state = "granted_and_promoted"
    elif p.status == proposal_mod.ProposalStatus.REJECTED:
        approval_state = "rejected"
    elif p.risk_score != "low":
        approval_state = "pending_founder_review"
    else:
        approval_state = "not_required"

    return {
        "found": True,
        "work_id": p.proposal_id,
        "objective": p.observed_weakness,
        "context_or_upgrade_text": p.proposed_upgrade,
        "classification": p.risk_score,
        "status": p.status,
        "paid_resources_allowed": p.paid_resources_allowed,
        "source_paths": p.source_paths,
        "approval_state": approval_state,
        "implementation_attempts": p.implementation_attempts,
        "implementation_job_ids": p.implementation_job_ids,
        "latest_job": latest_job,
        "lesson": p.lesson,
        "deferred_until": p.deferred_until,
        "created_at": p.created_at,
        "history": p.history[-10:],
        "next_canonical_step": (
            "Awaiting the next mrsilent-autonomous-cycle.timer run (<=15 min)" if p.risk_score == "low" and p.status in (proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED)
            else "Awaiting human review/decision (see request_founder_decision)" if p.risk_score != "low" and p.status not in proposal_mod.CLOSED_STATUSES
            else "Awaiting human `cli.py promote --founder-approved`" if p.status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE
            else None
        ),
        "source": "mrsilent_bridge.evolution.proposal + job_ledger",
        "generated_at_utc": _now(),
    }


def request_founder_decision(work_id: str, reason: str = "") -> dict:
    """Read-only surfacing of an EXISTING Founder-gated decision point on a
    submit_work proposal -- creates no second approval system, no new file,
    and never auto-approves. If the proposal isn't actually awaiting a
    decision, says so honestly instead of fabricating one."""
    try:
        p = proposal_mod.load(work_id)
    except Exception:
        return {"found": False, "work_id": work_id, "generated_at_utc": _now()}

    if p.status in proposal_mod.CLOSED_STATUSES:
        return {
            "found": True,
            "work_id": work_id,
            "decision_pending": False,
            "reason": f"proposal status={p.status!r} is already closed; no decision is pending",
            "generated_at_utc": _now(),
        }

    if p.risk_score != "low":
        return {
            "found": True,
            "decision_id": p.proposal_id,
            "work_id": p.proposal_id,
            "decision_pending": True,
            "protected_category": p.risk_score,
            "requested_decision": "review and, if appropriate, manually advance this founder_gated proposal (or reject it) via the existing CLI",
            "why_founder_approval_is_required": "risk_score != 'low' -- the existing auto-advance timer only ever picks up risk_score=='low' proposals",
            "current_status": p.status,
            "caller_supplied_reason": (reason or "")[:1000],
            "evidence": {"objective": p.observed_weakness, "created_at": p.created_at},
            "generated_at_utc": _now(),
        }

    if p.status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE:
        return {
            "found": True,
            "decision_id": p.proposal_id,
            "work_id": p.proposal_id,
            "decision_pending": True,
            "protected_category": "production_promotion",
            "requested_decision": "run `cli.py promote --founder-approved` if this sandboxed, validated, canaried change should become real -- this tool cannot do that itself",
            "why_founder_approval_is_required": "promotion.py hard-blocks any real promotion without an explicit human --founder-approved run; not overridable from here",
            "current_status": p.status,
            "caller_supplied_reason": (reason or "")[:1000],
            "evidence": {"implementation_job_ids": p.implementation_job_ids},
            "generated_at_utc": _now(),
        }

    return {
        "found": True,
        "work_id": p.proposal_id,
        "decision_pending": False,
        "reason": f"risk_score=='low' and status={p.status!r} -- this proposal does not currently need a Founder decision (it will be auto-advanced by the existing timer, or already was)",
        "generated_at_utc": _now(),
    }


def cancel_work(work_id: str) -> dict:
    """Governed cancellation, ONLY for proposals with zero implementation
    attempts (i.e. the existing auto-advance timer has not yet started any
    real job for it) -- moves the proposal to REJECTED via the existing
    proposal_mod.advance() lifecycle function, the same mechanism a human
    reviewer uses. Idempotent. Does not kill any process, PID, or systemd
    unit, and does not touch a proposal that already has an implementation
    attempt in flight or done -- there is no canonical safe way to interrupt
    that, so this reports unsupported instead of inventing one."""
    try:
        p = proposal_mod.load(work_id)
    except Exception:
        return {"found": False, "work_id": work_id, "generated_at_utc": _now()}

    if p.status == proposal_mod.ProposalStatus.REJECTED:
        return {"found": True, "work_id": work_id, "cancelled": True, "status": p.status, "already_cancelled": True, "generated_at_utc": _now()}

    if p.status in proposal_mod.CLOSED_STATUSES:
        return {
            "found": True, "work_id": work_id, "cancelled": False, "status": p.status,
            "reason": f"status={p.status!r} is already closed and not REJECTED -- not cancellable",
            "generated_at_utc": _now(),
        }

    if p.implementation_attempts > 0:
        return {
            "found": True, "work_id": work_id, "cancelled": False, "status": p.status,
            "reason": "no canonical safe cancellation path exists once an implementation attempt has started (no process-kill mechanism in this project) -- not cancellable via this tool",
            "generated_at_utc": _now(),
        }

    updated = proposal_mod.advance(work_id, proposal_mod.ProposalStatus.REJECTED, note="cancelled via CT/ChatGPT cancel_work (no implementation attempt had started)")
    return {"found": True, "work_id": work_id, "cancelled": True, "status": updated.status, "already_cancelled": False, "generated_at_utc": _now()}
