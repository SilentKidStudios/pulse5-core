"""
MR. SILENT Local Autonomous Engineering Cycle Runner.

ONE bounded, repeatable, one-shot pass over the entire existing pipeline:

    START -> cycle-level lock -> health snapshot -> startup recovery
    -> OBSERVE -> correlate/dedupe/propose -> advance eligible LOW-risk
    proposals (risk-classify -> local-first route -> OmniEngineer -> Claude
    fallback -> VALIDATE -> CANARY -> PROMOTION_CANDIDATE) -> durable cycle
    record -> release lock -> EXIT CLEANLY

This module builds NOTHING new at the execution layer — it orchestrates the
fully-existing job_ledger, studio_router, evolution/observe,
evolution/advance, and evolution/proposal machinery every prior milestone
already proved safe in isolation. Running the SAME battle-tested functions
in a fixed order is the entire point: this is glue, not a new engine.
Risk gating, local-first routing, Codex's structural unreachability from any
autonomous path, Scorpio/credential isolation, and Founder-gated promotion
are all inherited unchanged — nothing here re-implements or weakens any of
them.

Deliberately NOT a daemon: no loop, no thread, no scheduler, no systemd
unit, no cron entry. `cli.py autonomous-cycle` runs exactly one pass and
exits. A human (or a future, separately-authorized process) decides when to
run it again. Putting this behind a scheduler for 24x7 operation is an
explicit, separate, Founder-gated decision — see the README's readiness
assessment, written after this module's real proof runs.

QUIET IDLE, ON PURPOSE: if startup recovery finds nothing to do, OBSERVE
creates/reuses nothing new, and no proposal is eligible to advance, the
cycle's final_status is "idle" with zero fabricated work. Nothing in this
module invents a task to look busy — the proposal-creation logic all lives
in evolution/observe.py and studio_router.py, both already governed by
duplicate-suppression/dedup/cooldown; this module never creates a proposal
directly.

WORK BUDGETS, harness-enforced: MAX_PROPOSALS_PER_CYCLE (passed straight to
advance_eligible(limit=...)), MAX_RECOVERY_JOBS_PER_CYCLE, and
MAX_CYCLE_WALLCLOCK_S (checked between phases, AND passed into
advance_eligible() as a deadline_monotonic so it can stop STARTING new
proposals mid-batch too — added during the PRE-24x7 certification soak,
which found that advance_eligible() is otherwise a single blocking call: up
to MAX_PROPOSALS_PER_CYCLE proposals could each spend their own full
per-engine timeout budget back-to-back, multiplying the intended ceiling
several times over. This still never kills an in-flight job — each job's own
timeout_s remains the real backstop for that one call — it only bounds how
many NEW proposals a single cycle can start once the budget is already
spent). Each individual job is itself bounded by
omniengineer_agent.MAX_MODEL_CALLS/MAX_MALFORMED_RETRIES and
evolution.advance.MAX_IMPLEMENTATION_ATTEMPTS, inherited transitively rather
than re-declared here. None of these are reachable from model output.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import audit
import campaign as campaign_mod
import capability_registry
import job_ledger
import local_model_health
import omniengineer_harness
import omni_registry_stewardship
import organ_discovery
from evolution import advance as advance_mod
from evolution import external_evolution
from evolution import founder_request
from evolution import observe as observe_mod
from evolution import proposal as proposal_mod
from job_ledger import RecoveryPolicy

BRIDGE_ROOT = Path(__file__).resolve().parent
CYCLES_ROOT = BRIDGE_ROOT / "cycles"

CYCLE_LOCK_KEY = "autonomous-cycle"  # single global lock — only one cycle system-wide, ever

# FOUNDER TOP-10 PERSISTENT-CLOCK BINDING ------------------------------------
# The canonical Founder Studio-wide priority order is authored in the ACTION
# action-queue record ACTION-priority-governor-20260827 (the canonical
# authority) and projected — cache only — into
# mr_silent_spine/state/founder_top10_priority_queue.json. The already-existing
# read-only selector/governor is mr_silent_spine/autonomous_exec/
# founder_priority_governor.py; it is also consulted by the legacy
# multi_goal_arbiter.py selector. This persistent 15-minute clock consumes the
# SAME canonical order as a first-class input EVERY natural cycle — after the
# recovery/budget safety checks, before OBSERVE — read-only, and WITHOUT ever
# invoking the multi_goal_arbiter selector a second time (no duplicate selector
# invocation). If the governor module or its state is unavailable the hook is a
# fail-safe no-op and the rest of the cycle is byte-for-byte unchanged.
_SPINE_ROOT = Path("/opt/pulse5-core/mr_silent_spine")
_SPINE_STATE_DIR = _SPINE_ROOT / "state"
_FOUNDER_PRIORITY_GOVERNOR_PATH = _SPINE_ROOT / "autonomous_exec" / "founder_priority_governor.py"
_CANONICAL_ACTION_AUTHORITY_ID = "ACTION-priority-governor-20260827"
_CANONICAL_ACTION_AUTHORITY_PATH = _SPINE_ROOT / "action_queue" / f"{_CANONICAL_ACTION_AUTHORITY_ID}.json"

# Work budgets (#5) — harness-enforced, never model-influenced.
MAX_PROPOSALS_PER_CYCLE = 5
MAX_RECOVERY_JOBS_PER_CYCLE = 5
MAX_CYCLE_WALLCLOCK_S = 900  # 15 minutes hard ceiling for the whole cycle

# PRE-24x7 Phase L: bounded, cooldown-protected cognitive self-audit. Organs
# don't change systemd state every 15 minutes, so re-running
# organ_discovery.reconcile() (read-only systemctl checks) EVERY cycle would
# be wasteful noise, not genuine vigilance — this cooldown is the difference
# between "continuous" and "every single tick". Gated by the mtime of the
# most recent discovery_data/reconciliation_*.json, not a new state file.
COGNITIVE_AUDIT_COOLDOWN_S = 6 * 3600

# PRE-24x7 Phase K: at most ONE genuine, deduped capability-gap gets an
# external-evolution attempt per cycle, LOCAL DISCOVERY ONLY — this never
# sets allow_live_discovery/founder_approved, so it can never make a network
# call or reach OmniForge/OmniEngineer without a real, matching, license-
# clean local-catalog candidate. Reactive only (fires only for a gap
# studio_router already recorded), never proactive/manufactured busywork.
MAX_CAPABILITY_GAPS_PURSUED_PER_CYCLE = 1

# Phase N: proactive technology scouting runs far slower than the 15-minute
# Studio health cadence — gated the same way as the cognitive audit (by the
# mtime of its own durable state file, no new state-tracking mechanism).
# NEVER live/founder_approved from the autonomous path — local-catalog-only,
# same restraint as the capability-gap trigger above; live scouting remains
# a human-invoked `cli.py scout-proactively --founder-approved` action.
PROACTIVE_SCOUTING_COOLDOWN_S = 24 * 3600


@dataclass
class CycleRecord:
    cycle_id: str
    started_at: str
    completed_at: str | None
    owner: str
    health_snapshot: dict[str, Any] = field(default_factory=dict)
    recovery_actions: list[dict[str, Any]] = field(default_factory=list)
    signals_observed: dict[str, Any] = field(default_factory=dict)
    root_causes_correlated: list[dict[str, Any]] = field(default_factory=list)
    proposals_created: list[str] = field(default_factory=list)
    proposals_deduped: list[str] = field(default_factory=list)
    proposals_advanced: list[dict[str, Any]] = field(default_factory=list)
    implementation_jobs: list[str] = field(default_factory=list)
    engines_used: list[str] = field(default_factory=list)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    canary_results: list[dict[str, Any]] = field(default_factory=list)
    promotion_candidates: list[str] = field(default_factory=list)
    authority_blocks: list[dict[str, Any]] = field(default_factory=list)
    capability_gaps: list[str] = field(default_factory=list)
    maintenance: dict[str, Any] = field(default_factory=dict)
    cognitive_audit: dict[str, Any] = field(default_factory=dict)  # Phase L — see _maybe_run_cognitive_audit()
    phase_k_pursuits: list[dict[str, Any]] = field(default_factory=list)  # Phase K — see _maybe_pursue_capability_gaps()
    phase_n_scouting: dict[str, Any] = field(default_factory=dict)  # Phase N — see _maybe_scout_proactively()
    phase_r_campaigns: dict[str, Any] = field(default_factory=dict)  # Phase R — see _maybe_advance_one_campaign()
    omni_registry_stewardship: dict[str, Any] = field(default_factory=dict)  # Phase S — see _maybe_pursue_omni_registry_stewardship() (Permanent Studio Stewardship, second bounded slice, Founder-authorized 2026-08-24)
    founder_top10: dict[str, Any] = field(default_factory=dict)  # Persistent-clock Founder Top-10 consumption — see _consume_founder_top10()
    errors: list[str] = field(default_factory=list)
    final_status: str = "running"  # idle | work_performed | blocked | error | crashed (reconciled post-hoc by a later cycle)
    next_recommended_action: str | None = None


def _path(cycle_id: str) -> Path:
    return CYCLES_ROOT / f"{cycle_id}.json"


def _save(record: CycleRecord) -> None:
    CYCLES_ROOT.mkdir(parents=True, exist_ok=True)
    job_ledger._atomic_write_json(_path(record.cycle_id), asdict(record))  # reuse — no second atomic-write helper


def load(cycle_id: str) -> CycleRecord | None:
    p = _path(cycle_id)
    if not p.exists():
        return None
    return CycleRecord(**json.loads(p.read_text()))


def list_all() -> list[CycleRecord]:
    if not CYCLES_ROOT.exists():
        return []
    out = []
    for f in sorted(CYCLES_ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            out.append(CycleRecord(**json.loads(f.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


# evolution/observe.py's _NON_PRODUCTION_REQUESTERS covers job-level requested_by
# noise; this is the CycleRecord-level analogue, needed because
# tests/test_autonomous_cycle.py deliberately runs the REAL run_cycle() (the
# same genuine-live-call testing practice used for OmniEngineer) rather than
# mocking it, and every one of those real runs persists a genuine CycleRecord
# into the SAME live cycles/ store production cycles use. That's correct test
# design, not a bug — but left unfiltered it means Founder-facing status views
# (latest_cycle/last_successful_cycle/cycle_history_summary) can end up
# reporting a test run as if it were real autonomous activity. retention.py's
# accounting and _reconcile_abandoned_cycles() deliberately do NOT use this
# filter — they must see every record on disk regardless of owner.
_NON_PRODUCTION_CYCLE_OWNERS = frozenset({"test", "diag_test"})


def _production_cycles() -> list[CycleRecord]:
    return [c for c in list_all() if c.owner not in _NON_PRODUCTION_CYCLE_OWNERS]


def latest_cycle() -> CycleRecord | None:
    cycles = _production_cycles()
    return cycles[-1] if cycles else None


def last_successful_cycle() -> CycleRecord | None:
    for c in reversed(_production_cycles()):
        if c.final_status in ("idle", "work_performed"):
            return c
    return None


def _self_correct_duty_findings(record: CycleRecord) -> dict[str, Any]:
    """Phase O, folded into the SAME cognitive audit rather than a parallel
    mechanism: 'is this engine actually doing its job', not just 'is the
    process alive'. Runs EVERY cycle (cheap — job_ledger is already local,
    no network) unlike the registry-reconciliation half below, which stays
    on its own 6h cooldown. A silent_failure_detected engine first checks
    Known Failure Non-Recurrence memory (find_known_remediation); a known
    remediation is surfaced in the proposal for a human/OmniEngineer to
    apply — this does NOT auto-execute a repair, which would be a much
    larger, separately-justified authority expansion. An unknown failure
    gets a fresh, deduped, low-risk investigative proposal instead."""
    duty = organ_discovery.duty_check()
    created, deduped = [], []
    for engine, stats in duty["engines"].items():
        if not stats.get("silent_failure_detected"):
            continue
        error_sig = ",".join(stats.get("recent_failure_error_classes") or ["unknown"])
        fingerprint = f"duty_self_correction:{engine}:{error_sig}"
        existing_p = proposal_mod.find_open_by_fingerprint(fingerprint)
        if existing_p is not None:
            deduped.append(existing_p.proposal_id)
            continue
        recurrence, known = proposal_mod.classify_recurrence(affected_organ=engine, error_signature=error_sig)
        if recurrence == proposal_mod.RECURRENCE_SAME_ROOT_CAUSE:
            upgrade = (f"SAME_ROOT_CAUSE: a known remediation exists from a prior incident (see remediation: "
                       f"{known.get('remediation')}) — apply the same fix if the current context still "
                       f"matches; if it does not resolve this, record a NEW experience rather than repeating it blindly.")
        elif recurrence == proposal_mod.RECURRENCE_SIMILAR_SYMPTOM:
            upgrade = (f"SIMILAR_SYMPTOM_DIFFERENT_ROOT_CAUSE: {engine} has prior recorded experience, but not for "
                       f"this exact error signature ({error_sig!r}) — treat as a NEW incident, do not reuse a "
                       f"prior remediation; investigate root cause fresh, informed by (not replacing) that history.")
        else:
            upgrade = "UNKNOWN: no known remediation on record for this organ at all — investigate root cause fresh."
        p = proposal_mod.create(
            observed_weakness=(f"Division self-correction: {engine} is process-reachable but its own recent job "
                                f"outcomes are degraded ({stats['reason']}); recent failure error_classes={error_sig!r}. "
                                f"This is a 'running but not doing its job' signal, not a process-down signal."),
            proposed_upgrade=upgrade, risk_score="low", origin="observe_engine", fingerprint=fingerprint,
        )
        created.append(p.proposal_id)
    return {"duty_snapshot": duty["engines"], "self_correction_proposals_created": created,
            "self_correction_proposals_deduped": deduped}


def _maybe_run_cognitive_audit(record: CycleRecord) -> None:
    """Phase L: 'What do I believe exists (the registry) vs. what actually
    exists (live systemctl state)?' — bounded, cooldown-gated, read-only.
    Quiet-idle applies here exactly as everywhere else: if the cooldown
    hasn't elapsed, or reconcile() finds zero discrepancies, this records
    that plainly and creates NOTHING. A discrepancy becomes a real, low-risk
    proposal ONLY through the exact same fingerprint-dedup path OBSERVE
    already uses (proposal_mod.find_open_by_fingerprint) — never a duplicate
    for drift already known and still open."""
    duty_result = _self_correct_duty_findings(record)
    record.proposals_created.extend(duty_result["self_correction_proposals_created"])
    record.proposals_deduped.extend(duty_result["self_correction_proposals_deduped"])

    audit_result: dict[str, Any] = {"ran": False, "reason_skipped": None, "discrepancies_found": 0, **duty_result}
    existing = sorted(organ_discovery.OUT_DIR.glob("reconciliation_*.json")) if organ_discovery.OUT_DIR.exists() else []
    if existing:
        age_s = time.time() - existing[-1].stat().st_mtime
        if age_s < COGNITIVE_AUDIT_COOLDOWN_S:
            audit_result["reason_skipped"] = f"last reconciliation was {age_s:.0f}s ago, cooldown is {COGNITIVE_AUDIT_COOLDOWN_S}s"
            record.cognitive_audit = audit_result
            return

    result = organ_discovery.reconcile()
    audit_result["ran"] = True
    audit_result["discrepancies_found"] = len(result["discrepancies"])
    created, deduped = [], []
    for d in result["discrepancies"]:
        fingerprint = f"cognitive_audit:{d['capability_id']}:{d['live_systemctl_state']}"
        existing_p = proposal_mod.find_open_by_fingerprint(fingerprint)
        if existing_p is not None:
            deduped.append(existing_p.proposal_id)
            continue
        p = proposal_mod.create(
            observed_weakness=(f"Cognitive self-audit: registry_data/capabilities.json records "
                                f"{d['capability_id']} as status={d['recorded_status']!r}, but live "
                                f"`systemctl is-active {d['unit']}` currently reports {d['live_systemctl_state']!r}."),
            proposed_upgrade="Human/Claude review: confirm live state, then update the registry entry accordingly "
                              "(this proposal does not auto-mutate the registry).",
            risk_score="low",
            origin="observe_engine",
            fingerprint=fingerprint,
        )
        created.append(p.proposal_id)
    audit_result["proposals_created"] = created
    audit_result["proposals_deduped"] = deduped
    record.cognitive_audit = audit_result
    record.proposals_created.extend(created)
    record.proposals_deduped.extend(deduped)


def _maybe_pursue_capability_gaps(record: CycleRecord) -> None:
    """Phase K reactive trigger: for at most
    MAX_CAPABILITY_GAPS_PURSUED_PER_CYCLE genuinely open, never-yet-attempted
    capability_gap proposals (origin='studio_router', the exact fingerprint
    studio_router._record_capability_gap() already uses for dedup), run the
    external-evolution pipeline in LOCAL-CATALOG-ONLY mode. `p.lesson` is the
    idempotency marker — record_experience()/record_lesson() always set it,
    so an already-attempted gap is never re-attempted automatically (a human
    can still re-invoke `cli.py pursue-capability` manually with new
    evidence). Quiet-idle applies: zero eligible gaps -> record that, create
    nothing."""
    candidates = [
        p for p in proposal_mod.list_all()
        if p.origin == "studio_router" and (p.fingerprint or "").startswith("studio_router_capability_gap:")
        and p.status not in proposal_mod.CLOSED_STATUSES and p.lesson is None
    ]
    for p in candidates[:MAX_CAPABILITY_GAPS_PURSUED_PER_CYCLE]:
        task_type = (p.fingerprint or "").split(":", 1)[-1]
        result = external_evolution.pursue_external_capability(
            objective=p.observed_weakness, task_type=task_type, query=task_type,
            requested_by="autonomous_cycle", allow_live_discovery=False, founder_approved=False,
        )
        record.phase_k_pursuits.append({"proposal_id": p.proposal_id, "task_type": task_type, "status": result.status})
        # Mark the GAP proposal itself as attempted regardless of outcome —
        # black_vault/omniforge record their own experience on a DIFFERENT,
        # newly-created proposal when a candidate is actually evaluated/
        # integrated, so without this the gap proposal's own .lesson would
        # stay None and get re-attempted every single cycle forever.
        proposal_mod.record_lesson(p.proposal_id, f"Phase K auto-pursuit (local-catalog-only) result: "
                                                    f"{result.status} for task_type={task_type!r} "
                                                    f"(candidates_considered={result.candidates_considered}, "
                                                    f"see external_evolution.pursue_external_capability for detail)")


def _maybe_scout_proactively(record: CycleRecord) -> None:
    """Phase N: bounded, cooldown-gated, LOCAL-CATALOG-ONLY proactive tech
    scouting — one category/query per eligible run, never live/founder_
    approved from this unattended path. Cooldown is gated by the mtime of
    external_evolution's own scouting_state.json (no new state file, same
    pattern as _maybe_run_cognitive_audit's reconciliation-snapshot check).
    Quiet-idle applies: nothing eligible/nothing found -> record that,
    fabricate nothing."""
    result: dict[str, Any] = {"ran": False, "reason_skipped": None}
    state_path = external_evolution._SCOUTING_STATE_PATH
    if state_path.exists():
        age_s = time.time() - state_path.stat().st_mtime
        if age_s < PROACTIVE_SCOUTING_COOLDOWN_S:
            result["reason_skipped"] = f"last scouting pass was {age_s:.0f}s ago, cooldown is {PROACTIVE_SCOUTING_COOLDOWN_S}s"
            record.phase_n_scouting = result
            return

    scouting = external_evolution.scout_proactively(requested_by="autonomous_cycle", allow_live_discovery=False,
                                                      founder_approved=False)
    result.update({
        "ran": True, "category": scouting.category, "query": scouting.query, "status": scouting.status,
        "classifications": [{"name": c.name, "classification": c.classification} for c in scouting.classifications],
    })
    record.phase_n_scouting = result


MAX_CAMPAIGNS_ADVANCED_PER_CYCLE = 1  # bounded — never more than one campaign touched per cycle


def _maybe_advance_one_campaign(record: CycleRecord) -> None:
    """Phase R: at most ONE active campaign gets reconciled+advanced by ONE
    step per cycle. Never creates a campaign (that remains an explicit,
    deliberate action via campaign.create(), never automatic — 'do not
    create campaigns simply to appear productive'). Reconciles real step
    outcomes first (sync_step_outcomes — this is where authority-required
    pauses and full completion get detected, from REAL proposal state, never
    guessed), then starts the next planned step's proposal ONLY if the
    campaign is still active after that reconciliation and has no proposal
    already in flight. That new proposal is picked up by the EXISTING
    advance_eligible() in this or a later cycle — this function itself
    never calls advance_one() or bypasses any budget/gate."""
    # Priority-aware selection (Founder-authorized 2026-08-18): lower
    # priority value = advanced first. Sorted, not filtered — every active
    # campaign is still eligible, this only changes WHICH ONE(S) get the
    # bounded per-cycle slot(s) first. Same Phase R mechanism, no new
    # scheduler.
    active = sorted((c for c in campaign_mod.list_all() if c.status == campaign_mod.STATUS_ACTIVE),
                     key=lambda c: c.priority)
    if not active:
        record.phase_r_campaigns = {"ran": False, "reason_skipped": "no active campaigns"}
        return

    result: dict[str, Any] = {"ran": True, "campaigns_touched": []}
    for c in active[:MAX_CAMPAIGNS_ADVANCED_PER_CYCLE]:
        sync = campaign_mod.sync_step_outcomes(c.campaign_id)
        reloaded = campaign_mod.load(c.campaign_id)
        started_proposal_id = None
        if reloaded.status == campaign_mod.STATUS_ACTIVE and not reloaded.active_jobs:
            started_proposal_id = campaign_mod.start_next_step(c.campaign_id, requested_by="autonomous_cycle")
        result["campaigns_touched"].append({
            "campaign_id": c.campaign_id, "sync": sync, "status_after": campaign_mod.load(c.campaign_id).status,
            "started_proposal_id": started_proposal_id,
        })
    record.phase_r_campaigns = result


def _maybe_pursue_omni_registry_stewardship(record: CycleRecord) -> None:
    """Phase S — Permanent Studio Stewardship, second bounded slice
    (Founder-authorized 2026-08-24). Called ONLY when the caller has already
    determined every earlier phase this cycle found nothing to do (no
    proposal created/advanced, no recovery/phase_k/phase_r activity) — this
    function never preempts real higher-priority work, it only fills a
    genuinely idle cycle. Consults the LIVE OmniRegistry fresh every call via
    omni_registry_stewardship.select_next_idle_division() — never a
    hard-coded division name — and, only if a genuinely eligible,
    not-yet-campaigned division is found, hands it to the SAME existing
    mission_decomposition -> campaign.create() -> Phase R pipeline every
    other campaign already goes through. This function itself never
    executes anything and never bypasses any existing gate (authority
    policy, promotion, duplicate suppression all apply exactly as they do
    to any other campaign)."""
    try:
        division = omni_registry_stewardship.select_next_idle_division()
    except Exception as e:  # noqa: BLE001 — a stewardship failure must never crash the cycle
        record.omni_registry_stewardship = {"ran": True, "campaign_created": False, "error": repr(e)}
        return
    if division is None:
        record.omni_registry_stewardship = {
            "ran": True, "campaign_created": False,
            "reason_skipped": "no eligible, not-yet-campaigned division found in the live OmniRegistry this cycle",
        }
        return
    result = omni_registry_stewardship.pursue_division(division, requested_by="autonomous_cycle")
    record.omni_registry_stewardship = {"ran": True, "campaign_created": bool(result.get("campaign_id")), **result}


def _reconcile_abandoned_cycles(this_cycle_id: str) -> None:
    """Marks any OTHER cycle record still at final_status=="running" as
    "crashed" — provably safe to do here specifically: this function is only
    called immediately after THIS process just successfully claimed
    CYCLE_LOCK_KEY, which is only possible if no live process currently
    holds it. Any "running" record found at that exact moment cannot belong
    to a still-executing cycle (it would be holding the lock we just got) —
    it can only be bookkeeping left behind by a process that was killed
    externally (e.g. SIGTERM) before its own `finally:` could run. This is
    the CycleRecord-level analogue of job_ledger's heartbeat/is_stale() —
    reusing the SAME proof technique (the lock itself is the ground truth),
    not a new staleness heuristic."""
    for c in list_all():
        if c.cycle_id == this_cycle_id or c.final_status != "running":
            continue
        c.final_status = "crashed"
        c.completed_at = datetime.now(timezone.utc).isoformat()
        c.errors.append("reconciled: this cycle's process was killed/crashed before it could record a final status "
                         "(discovered because a later cycle was able to claim the lock, which is only possible if "
                         "no live process still held it)")
        c.next_recommended_action = "no action needed — this is a historical record correction, not an active problem"
        _save(c)


def _reconcile_stale_proposal_locks() -> list[str]:
    """Phase O reliability fix, found via real testing (an externally-killed
    process left a stale proposal-impl-* lock that silently blocked
    advance_eligible() from ever retrying that proposal again — 'work_
    performed' kept firing with nothing productive happening). job_ledger's
    own cycle-level lock already gets this exact reconciliation
    (_reconcile_abandoned_cycles, above) and job-level locks get it via
    heartbeat/is_stale() inside resume_job(); proposal-impl-* locks
    (evolution/advance.py's _proposal_lock_key) are a virtual claim with no
    ledger.json, so neither existing mechanism ever scans them. Same 'only
    one cycle process runs at a time' proof: this only runs immediately
    after THIS process claimed CYCLE_LOCK_KEY, so any proposal-impl lock
    still present cannot belong to a live advance_one() call from THIS
    cycle (it hasn't started one yet) or any other cycle (none can be
    running concurrently) — break_stale_lock()'s own dead-pid/aged-out
    check still gates every individual break, so this never touches a
    lock that isn't provably stale."""
    broken = []
    for lock_file in job_ledger.JOBS_ROOT.glob("proposal-impl-*/ledger.lock"):
        key = lock_file.parent.name
        if job_ledger.break_stale_lock(key, requested_by="autonomous_cycle_startup_reconciliation"):
            broken.append(key)
    return broken


def cycle_history_summary(limit: int = 20) -> dict[str, Any]:
    """Trend view over the most recent `limit` cycles — for 24x7-readiness
    assessment and Founder visibility via `cli.py cycle-status --history`.
    Read-only; never consulted by run_cycle() itself, so it can never
    influence a cycle's own behavior. Excludes test-owned records — see
    _NON_PRODUCTION_CYCLE_OWNERS — so trend numbers reflect genuine
    autonomous operation, not test-suite noise."""
    cycles = _production_cycles()[-limit:]
    if not cycles:
        return {"count": 0}
    durations = []
    for c in cycles:
        if not c.completed_at:
            continue
        try:
            started = datetime.fromisoformat(c.started_at)
            completed = datetime.fromisoformat(c.completed_at)
            durations.append((completed - started).total_seconds())
        except ValueError:
            continue
    return {
        "count": len(cycles),
        "status_counts": dict(Counter(c.final_status for c in cycles)),
        "avg_duration_s": round(sum(durations) / len(durations), 3) if durations else None,
        "max_duration_s": round(max(durations), 3) if durations else None,
        "total_proposals_created": sum(len(c.proposals_created) for c in cycles),
        "total_proposals_advanced": sum(len(c.proposals_advanced) for c in cycles),
        "total_promotion_candidates": sum(len(c.promotion_candidates) for c in cycles),
        "total_recovery_actions": sum(len(c.recovery_actions) for c in cycles),
        "total_errors": sum(len(c.errors) for c in cycles),
    }


def _startup_recovery(cycle_id: str, *, requested_by: str, max_jobs: int) -> list[dict[str, Any]]:
    """Scans EVERY non-terminal job_ledger record, system-wide — not just
    ones linked to a proposal (evolution/advance.py's own advance_one()
    already re-checks its OWN linked job's staleness when it runs later this
    same cycle; this step additionally covers raw/direct submit_job() jobs
    that aren't tied to any proposal). Never bypasses authority: resume_job()
    re-runs authority_policy.classify() from scratch internally.

    Real gap found via live evidence (2026-08-19 Omni God Mode audit): a
    genuinely stuck job (stale lock held by a dead process) was silently
    re-logged as "escalated_not_recovered"/"resume_refused_escalate" in
    cycle_history for 23 consecutive 15-minute cycles (~6 hours) with NO
    Founder-visible surfacing anywhere — only found via a manual deep audit
    of raw cycle records, not by anything MR. SILENT itself raised. Fixed
    by routing any genuinely unresolved stale job through the EXISTING
    founder_request.request_founder_decision() escalation mechanism (never
    a new notification channel — same durable, deduplicated record every
    other Founder-gated finding in this project already uses). Dedup is
    fingerprint-based (subject+capability_needed), so re-observing the SAME
    stuck job every cycle safely updates the SAME record's update_count/
    last_seen_at instead of spamming a new escalation each time."""
    actions: list[dict[str, Any]] = []
    terminal_values = {s.value for s in job_ledger.TERMINAL_STATES}
    non_terminal = [r for r in job_ledger.list_all() if r.state not in terminal_values]
    stale = [r for r in non_terminal if job_ledger.is_stale(r)]
    active_not_stale = [r for r in non_terminal if not job_ledger.is_stale(r)]

    for r in stale[:max_jobs]:
        policy = job_ledger.classify(r)
        if policy in (RecoveryPolicy.SAFE_RESUME, RecoveryPolicy.RESTART_FROM_SANDBOX):
            try:
                result = omniengineer_harness.resume_job(r.job_id, requested_by=f"autonomous_cycle:{cycle_id}")
                outcome = result.status
            except Exception as e:  # noqa: BLE001 — a recovery failure must not crash the whole cycle
                outcome = f"recovery_error: {e!r}"
            actions.append({"job_id": r.job_id, "prior_state": r.state, "policy": policy.value, "outcome": outcome})
        else:
            # ESCALATE / TERMINAL_FAILURE / FOUNDER_REQUIRED — never auto-acted on.
            outcome = "escalated_not_recovered"
            actions.append({"job_id": r.job_id, "prior_state": r.state, "policy": policy.value, "outcome": outcome})

        if outcome == "escalated_not_recovered" or outcome.startswith("resume_refused_"):
            try:
                founder_request.request_founder_decision(
                    subject=f"stuck job_ledger record {r.job_id}",
                    finding=f"job {r.job_id} (task: {r.task[:150]!r}) has been stuck in state={r.state!r} "
                            f"since {r.created_at} and startup recovery could not resolve it "
                            f"(policy={policy.value}, outcome={outcome!r})",
                    capability_needed="manual investigation and/or job_ledger.break_stale_lock()+resume/checkpoint",
                    reason_required="repeated automated recovery attempts have not resolved this job; it may "
                                     "need a human to inspect why (e.g. a stale lock from an externally-killed "
                                     "process, or a genuine unresolvable failure) before it can be safely closed",
                    recommended_action="inspect job_ledger.lock_status()/load() for this job_id and either "
                                        "break_stale_lock()+resume_job() or checkpoint it to a terminal state",
                    risk="low — the job is already stuck and inert; this is a visibility gap, not a live hazard",
                    affected={"job_id": r.job_id, "state": r.state, "policy": policy.value},
                    rollback_recovery="no action is taken automatically; this is a durable, dedup'd escalation "
                                        "record only — resolving it is entirely a human/founder decision",
                    requested_by=f"autonomous_cycle:{cycle_id}",
                )
            except Exception as e:  # noqa: BLE001 — surfacing a finding must never crash the cycle
                actions[-1]["escalation_error"] = repr(e)

    for r in active_not_stale:
        actions.append({"job_id": r.job_id, "prior_state": r.state, "policy": "active_not_stale", "outcome": "left_untouched"})

    return actions


def _health_snapshot() -> dict[str, Any]:
    local = local_model_health.check_json()
    claude_entry = capability_registry.find("claude_code_engineer_v1") or {}
    codex_entry = capability_registry.find("codex_engineer_v1") or {}
    # Cross-provider resilience (Founder-authorized 2026-08-18): report
    # provider_b's status too, without starting it — is_running()/
    # circuit_status() are both read-only, ensure_running() is never
    # called from a health snapshot.
    try:
        import provider_b_bridge
        provider_b_snapshot = {
            "running": provider_b_bridge.is_running(),
            "circuit": local_model_health.circuit_status("provider_b"),
        }
    except Exception as e:  # noqa: BLE001 — a health snapshot must never break a cycle
        provider_b_snapshot = {"running": False, "error": repr(e)}
    # Contradiction detection (Founder-authorized 2026-08-18): cheap,
    # registry-internal-only check (no network, no filesystem scan beyond
    # the registry file already read every cycle) — catches a capability
    # falsely claimed absent while another active entry actually covers it.
    try:
        contradictions = organ_discovery.detect_capability_contradictions()
    except Exception as e:  # noqa: BLE001 — a health snapshot must never break a cycle
        contradictions = {"contradictions_found": 0, "error": repr(e)}
    return {
        "local_model": local,
        "provider_b": provider_b_snapshot,
        "ollama_circuit": local_model_health.circuit_status("ollama"),
        "capability_contradictions": contradictions,
        "claude_code": {"availability": claude_entry.get("availability"), "quota_status": claude_entry.get("quota_status")},
        "codex": {"availability": codex_entry.get("availability"), "quota_status": codex_entry.get("quota_status"),
                  "note": "never invoked from the autonomous cycle regardless of quota state — structurally unreachable, see evolution/advance.py"},
    }


def _capability_gap_proposal_ids(observe_report) -> set[str]:
    """Cross-references OBSERVE's own observations (signal_type=='capability_gap'
    — a real, discovered gap) against created proposals, plus studio_router's
    runtime capability-gap proposals (distinct fingerprint prefix). Never
    includes an authority_gate_blocked outcome — that is a real, existing,
    just-currently-gated capability, not a missing one (see studio_router.py)."""
    from_observe = {
        o["linked_proposal_id"] for o in observe_report.observations
        if o["signal_type"] == "capability_gap" and o.get("linked_proposal_id")
    }
    from_router = set()
    for pid in observe_report.proposals_created:
        try:
            p = proposal_mod.load(pid)
        except (FileNotFoundError, json.JSONDecodeError):
            continue  # defensive: proposals_created should always exist, but never crash a whole cycle over one
        if (p.fingerprint or "").startswith("studio_router_capability_gap:"):
            from_router.add(pid)
    return (from_observe | from_router) & set(observe_report.proposals_created)


def _load_founder_priority_governor():
    """Import the already-existing spine governor module
    (mr_silent_spine/autonomous_exec/founder_priority_governor.py) by file
    path — no sys.path mutation of this long-lived process. Returns the module
    or None when it is unavailable, in which case the persistent-clock hook
    degrades to a no-op."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "founder_priority_governor", _FOUNDER_PRIORITY_GOVERNOR_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — a priority read must never break a cycle
        return None


def _canonical_action_authority_order() -> list[str] | None:
    """The rank-ordered id list from the CANONICAL authority record
    (ACTION-priority-governor-20260827). Returns None if unreadable."""
    try:
        raw = json.loads(_CANONICAL_ACTION_AUTHORITY_PATH.read_text())
        q = sorted(raw.get("durable_priority_queue", []), key=lambda r: r.get("rank", 10**9))
        return [r.get("id") for r in q]
    except Exception:  # noqa: BLE001
        return None


def _consume_founder_top10() -> dict[str, Any]:
    """PERSISTENT-CLOCK Founder Top-10 consumption — read-only.

    Walks the already-existing governor's decision logic to record, onto the
    CycleRecord, which Founder rank currently governs, the full #1-#10 order,
    the per-rank explicit-truth-state evaluations, and whether the cache
    projection (founder_top10_priority_queue.json) still matches the canonical
    authority (ACTION-priority-governor-20260827).

    Invariants (all preserved here):
      * Does NOT reorder / replace any existing cycle precedence — startup
        recovery has already run above; OBSERVE / advance / stewardship below
        are untouched.
      * NEVER writes priority or completion state.
      * NEVER invokes the multi_goal_arbiter selector (no duplicate selector
        invocation) — it only reads the shared governor's pure functions.
      * Treats ACTION-priority-governor-20260827 as canonical authority and the
        queue JSON as cache/projection only.
    """
    base = {
        "hook_position": "after_recovery_budget_before_observe",
        "selector_invoked": False,
        "blend_not_replace": True,
        "canonical_action_authority": _CANONICAL_ACTION_AUTHORITY_ID,
        "projection_is_cache_only": True,
        "projection_file": "mr_silent_spine/state/founder_top10_priority_queue.json",
    }
    gov = _load_founder_priority_governor()
    if gov is None:
        return {**base, "consumed": False, "reason": "founder_priority_governor_unavailable"}
    try:
        governing, evaluations = gov.choose_governing_rank(_SPINE_STATE_DIR)
        projection_order = gov.full_top10_order(_SPINE_STATE_DIR)
    except Exception as e:  # noqa: BLE001
        return {**base, "consumed": False, "reason": f"governor_read_error: {e!r}"}

    authority_order = _canonical_action_authority_order()
    consistent = authority_order is not None and authority_order == projection_order
    out = {
        **base,
        "consumed": True,
        "top10_full_order": projection_order,
        "top10_full_order_present": len(projection_order) == 10,
        "governing_rank": governing.get("rank") if governing else None,
        "governing_id": governing.get("id") if governing else None,
        "evaluated": evaluations,
        "authority_order": authority_order,
        "authority_projection_consistent": consistent,
    }
    if not consistent:
        out["authority_projection_drift"] = {
            "authority_order": authority_order,
            "projection_order": projection_order,
            "note": "cache projection is stale vs canonical ACTION authority — regenerate the projection",
        }
    return out


def run_cycle(*, requested_by: str = "autonomous_cycle") -> CycleRecord:
    cycle_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    record = CycleRecord(cycle_id=cycle_id, started_at=started_at, completed_at=None, owner=requested_by)
    _save(record)

    # CYCLE-LEVEL LOCK (#2): only one cycle system-wide, ever. A second
    # invocation detects active ownership and exits cleanly (not an error).
    # Deliberately NEVER auto-recovers a stale lock from inside this
    # function, even when lock_status() says stale=True — see
    # tests/test_autonomous_cycle.py::test_stale_cycle_lock_safely_
    # classified for the explicit "never a blind takeover" invariant this
    # preserves. (A real gap in the OTHER direction — nothing upstream of
    # this function ever called job_ledger.break_stale_lock() either, so a
    # genuinely dead owner's lock could block every future cycle forever
    # with no path back to healthy short of manual intervention — is fixed
    # at cli.py's autonomous-cycle entrypoint instead: an explicit,
    # human-configured production entrypoint pre-flight-checking for a
    # confirmed-dead (not merely aged-out) owner before ever calling this
    # function, rather than changing what run_cycle() itself will do when
    # called directly, including by tests and other callers that rely on
    # its current strict behavior.)
    if not job_ledger.claim(CYCLE_LOCK_KEY, owner=requested_by):
        status = job_ledger.lock_status(CYCLE_LOCK_KEY)
        record.final_status = "blocked"
        record.errors.append(
            f"another autonomous cycle already holds the lock (owner={status.get('owner')!r}, "
            f"pid={status.get('pid')}, stale={status.get('stale')})"
        )
        record.next_recommended_action = (
            "the active cycle's lock looks stale — a later run may recover it" if status.get("stale")
            else "wait for the active cycle to finish before re-running"
        )
        record.completed_at = datetime.now(timezone.utc).isoformat()
        _save(record)
        _maybe_run_cognitive_audit(record)
        return record

    _reconcile_abandoned_cycles(cycle_id)

    try:
        record.health_snapshot = _health_snapshot()
        _save(record)

        # Non-destructive housekeeping (#retention): moves old audit entries
        # into a dated archive file if the live log has grown past threshold
        # — never deletes anything, no-op below threshold. Deliberately NOT
        # "work" for final_status purposes (see the idle/work_performed
        # computation below) — file-size management, not eligible-proposal work.
        try:
            record.maintenance = {"audit_log_rotation": audit.rotate()}
        except Exception as e:  # noqa: BLE001 — housekeeping must never break a cycle
            record.maintenance = {"audit_log_rotation": {"rotated": False, "reason": f"error: {e!r}"}}

        # Provider B idle reclaim (24X7_RUNTIME_HEALTH, cross-provider
        # resilience, Founder-authorized 2026-08-18): if the standalone
        # llama-server process has been running but unused for
        # IDLE_RECLAIM_S, stop it to free its ~13GB RAM footprint. A no-op
        # the vast majority of the time (it's usually not even running —
        # only ever started on-demand during a real cross-provider
        # failover). Never touches Ollama. Reuses this EXISTING per-cycle
        # pass rather than a new scheduler.
        try:
            import provider_b_bridge
            record.maintenance["provider_b_idle_reclaim"] = {"reclaimed": provider_b_bridge.reclaim_if_idle()}
        except Exception as e:  # noqa: BLE001 — housekeeping must never break a cycle
            record.maintenance["provider_b_idle_reclaim"] = {"reclaimed": False, "reason": f"error: {e!r}"}
        _save(record)

        record.recovery_actions = _startup_recovery(cycle_id, requested_by=requested_by, max_jobs=MAX_RECOVERY_JOBS_PER_CYCLE)
        for broken_key in _reconcile_stale_proposal_locks():
            record.recovery_actions.append({"job_id": broken_key, "prior_state": "stale_proposal_impl_lock",
                                              "policy": "break_stale_lock", "outcome": "broken"})
        _save(record)

        if time.monotonic() - t0 > MAX_CYCLE_WALLCLOCK_S:
            record.final_status = "blocked"
            record.errors.append("cycle wall-clock budget exceeded during startup recovery")
            record.next_recommended_action = "re-run — recovery actions taken so far are already durable"
            record.completed_at = datetime.now(timezone.utc).isoformat()
            _save(record)
            return record

        # FOUNDER TOP-10 PERSISTENT-CLOCK BINDING — after the recovery/budget
        # safety checks above, before OBSERVE below. Read-only consumption of
        # the canonical Founder Top-10 order every natural cycle
        # (PERSISTENT_CLOCK_CONSUMES_TOP10). Never invokes the selector a
        # second time; never mutates priority/completion state; a no-op if the
        # governor is unavailable. Deliberately NOT counted as cycle "work" —
        # reading a priority order is not eligible-proposal activity.
        try:
            record.founder_top10 = _consume_founder_top10()
        except Exception as e:  # noqa: BLE001 — a priority read must never break a cycle
            record.founder_top10 = {"consumed": False, "reason": f"error: {e!r}",
                                     "hook_position": "after_recovery_budget_before_observe",
                                     "selector_invoked": False}
        _save(record)

        # FOUNDER TOP-10 -> CANONICAL PROPOSAL BRIDGE: pass the already-computed
        # governing rank (from _consume_founder_top10() above, itself read-only
        # and never re-invoking the selector) straight into OBSERVE's existing
        # signal battery. This is the SAME proposal store/dedup/advance path
        # every other signal already uses -- no second selector, no second
        # proposal system, no second scheduler. See
        # observe.signal_governing_priority_needs_proposal() for the (always
        # founder_gated, never auto-executable) proposal this can create.
        observe_report = observe_mod.run(
            auto_propose=True,
            governing_priority_id=record.founder_top10.get("governing_id"))
        record.signals_observed = observe_report.summary
        record.root_causes_correlated = observe_report.root_causes_correlated
        record.proposals_created = observe_report.proposals_created
        record.proposals_deduped = (observe_report.proposals_reused
                                     + observe_report.proposals_known_deferred
                                     + observe_report.proposals_on_cooldown)
        record.capability_gaps = sorted(_capability_gap_proposal_ids(observe_report))
        # Truthful, tri-state Founder Top-10 proposal classification -- reuses
        # the SAME evolution/advance.py::_eligible() semantics as the bridge
        # signal above (single source of truth, computed once). Replaces a
        # prior coarse "any proposal exists" boolean that let a rejected/
        # terminal proposal silently suppress this forever even when nothing
        # was actionable AND nothing was pending Founder review -- a real
        # deadlock (mechanically proven 2026-09-03, commit e240d80 gap).
        # A genuinely open founder_gated match becomes an explicit
        # authority_block (the SAME field advance_eligible()'s own
        # authority-block reporting already uses) so final_status correctly
        # reports "blocked" with a truthful reason instead of a misleading
        # "work_performed" / "none -- re-run later" when a real Founder
        # decision, not more autonomous activity, is what's actually needed.
        if record.founder_top10.get("consumed") and record.founder_top10.get("governing_id"):
            gov_state = observe_mod.classify_governing_priority_proposals(record.founder_top10["governing_id"])
            record.founder_top10["governing_rank_has_actionable_proposal"] = bool(gov_state.actionable)
            record.founder_top10["governing_rank_has_founder_gated_proposal"] = bool(gov_state.founder_gated_open)
            record.founder_top10["governing_rank_has_terminal_only_proposals"] = gov_state.terminal_only
            if gov_state.founder_gated_open and not gov_state.actionable:
                for p in gov_state.founder_gated_open:
                    record.authority_blocks.append({
                        "proposal_id": p.proposal_id,
                        "reason": (f"Founder Top-10 rank '{record.founder_top10['governing_id']}' has an open "
                                   f"{p.risk_score} proposal (status={p.status}) awaiting explicit Founder "
                                   f"review/approval -- preserved, never auto-advanced or auto-downgraded"),
                    })
        _save(record)

        if time.monotonic() - t0 > MAX_CYCLE_WALLCLOCK_S:
            record.final_status = "blocked"
            record.errors.append("cycle wall-clock budget exceeded after OBSERVE")
            record.next_recommended_action = "re-run — proposals created/deduped this cycle are already durable"
            record.completed_at = datetime.now(timezone.utc).isoformat()
            _save(record)
            return record

        advancement_results = advance_mod.advance_eligible(
            limit=MAX_PROPOSALS_PER_CYCLE, requested_by=requested_by,
            deadline_monotonic=t0 + MAX_CYCLE_WALLCLOCK_S)
        for r in advancement_results:
            record.proposals_advanced.append({
                "proposal_id": r.proposal_id, "final_status": r.final_status,
                "selected_engine": r.selected_engine, "attempt_number": r.attempt_number,
                "blocked_reason": r.blocked_reason,
            })
            if r.implementation_job_id:
                record.implementation_jobs.append(r.implementation_job_id)
            if r.selected_engine and r.selected_engine not in record.engines_used:
                record.engines_used.append(r.selected_engine)
            if r.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE:
                record.promotion_candidates.append(r.proposal_id)
            if r.blocked_reason == "implementing task was policy-rejected":
                detail = next((s["detail"] for s in r.stages if s["stage"] == "implementation_router"), r.blocked_reason)
                record.authority_blocks.append({"proposal_id": r.proposal_id, "reason": detail})
            if r.implementation_job_id:
                jr_path = omniengineer_harness.JOBS_ROOT / r.implementation_job_id / "result.json"
                if jr_path.exists():
                    try:
                        data = json.loads(jr_path.read_text())
                        if data.get("validation"):
                            record.validation_results.append({"job_id": r.implementation_job_id, "passed": data["validation"].get("passed")})
                        if data.get("canary"):
                            record.canary_results.append({"job_id": r.implementation_job_id, "passed": data["canary"].get("passed")})
                    except json.JSONDecodeError:
                        pass
        _save(record)

        _maybe_run_cognitive_audit(record)
        _save(record)

        _maybe_pursue_capability_gaps(record)
        _save(record)

        _maybe_scout_proactively(record)
        _save(record)

        _maybe_advance_one_campaign(record)
        _save(record)

        recovery_did_something = any(a["outcome"] not in ("left_untouched",) for a in record.recovery_actions)
        phase_k_did_something = any(pk["status"] == "integration_succeeded" for pk in record.phase_k_pursuits)
        phase_r_did_something = any(t.get("started_proposal_id") for t in record.phase_r_campaigns.get("campaigns_touched", []))
        work_happened_before_stewardship = bool(record.proposals_created or record.proposals_advanced or recovery_did_something
                              or phase_k_did_something or phase_r_did_something)

        # Phase S only ever runs on a cycle every earlier phase already left
        # idle — it never preempts or races real higher-priority work, and
        # never runs twice in the same cycle.
        if not work_happened_before_stewardship:
            _maybe_pursue_omni_registry_stewardship(record)
            _save(record)

        phase_s_did_something = bool(record.omni_registry_stewardship.get("campaign_created"))
        work_happened = work_happened_before_stewardship or phase_s_did_something

        if not work_happened:
            record.final_status = "idle"
            record.next_recommended_action = "none — nothing eligible found this cycle; re-run later or after new signals appear"
        elif record.authority_blocks and not record.promotion_candidates:
            record.final_status = "blocked"
            record.next_recommended_action = "review authority_blocks — a Founder decision may be required"
        else:
            record.final_status = "work_performed"
            record.next_recommended_action = (
                f"review {len(record.promotion_candidates)} promotion candidate(s) via "
                f"`cli.py promote <job_id> --target <path> --founder-approved`"
                if record.promotion_candidates else "none — re-run later"
            )

        record.completed_at = datetime.now(timezone.utc).isoformat()
        _save(record)
        return record
    except Exception as e:  # noqa: BLE001 — a cycle-level crash must still leave a durable, honest record
        record.errors.append(f"unhandled error: {e!r}")
        record.final_status = "error"
        record.next_recommended_action = "investigate the error before re-running"
        record.completed_at = datetime.now(timezone.utc).isoformat()
        _save(record)
        raise
    finally:
        job_ledger.release(CYCLE_LOCK_KEY, owner=requested_by)
