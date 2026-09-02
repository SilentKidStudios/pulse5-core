"""
SAFE AUTONOMOUS PROPOSAL ADVANCEMENT — now durable/idempotent via the
existing OmniEngineer job ledger, not a parallel durability system.

    OBSERVE -> CORRELATE ROOT CAUSE -> PROPOSE -> RISK CLASSIFY
    -> CONCURRENCY CLAIM -> STABLE IMPLEMENTATION IDENTITY -> IMPLEMENTATION
       ROUTER (Claude Code primary -> Omni Engineer fallback ->
       Codex only under existing Founder gate -> human escalation)
    -> AUTOMATIC VALIDATION -> CANARY -> PROMOTION CANDIDATE

    ENGINE PRIORITY CORRECTION (Founder-authorized 2026-08-19): the router
    used to be Omni-Engineer-local-first purely on studio_router.py's
    generic locality/cost doctrine — an accident of reusing that generic
    ranking for an engineering-specific decision, not a deliberate choice.
    Claude Code is now the default primary engineering engine, Omni Engineer
    the default fallback, with automatic (state-free, re-evaluated every
    call) failover to Omni Engineer when organ_discovery.duty_check() shows
    Claude Code is currently silently failing, and a real evidence-based
    promotion path if Omni Engineer ever sustainably outperforms it. See
    _rank_engineering_engines() below. studio_router.py itself, and every
    OTHER consumer of it, is completely untouched — the generic local/free
    doctrine still applies everywhere else in the Studio.

OBSERVE, CORRELATE, and IDENTIFY are evolution/observe.py's job. This module
picks up from PROPOSE and only ever carries a proposal to PROMOTION_CANDIDATE
— never to PROMOTED. Actually writing anything to a real path outside a
job's sandbox still goes through promotion.py's existing founder-gated
`promote --founder-approved`, run by a human, choosing a target — untouched
by this module, and untouched by anything added this milestone: a recovered
or reused implementation is not promotion-authorized any differently than a
fresh one (see the PROMOTION CANDIDATE stage below — it is reached the same
way regardless of how `job` was obtained).

STABLE IMPLEMENTATION IDENTITY (#1): `proposal.implementation_job_ids` is a
proposal's full implementation lineage, in order. It is appended to via an
`on_job_created` callback threaded into BOTH bridge.submit_job() and
omniengineer_harness.submit_job() — fired the INSTANT a job_id is generated,
before any real work happens. This closes the one genuine gap the previous
milestone left open: without it, a crash between "job created" and "advance_one()
records the job_id" would leave the proposal with no memory of that job_id at
all, and a restart would create a second, duplicate implementation job. With
it, even a crash one line after job creation still leaves the proposal
knowing about that exact job_id, ready to resume or reuse it.

CONCURRENCY CLAIM (#2): before touching implementation at all, advance_one()
claims `job_ledger.claim(f"proposal-impl-{proposal_id}", owner=requested_by)`
— the EXACT SAME claim/release/lock_status primitives job_ledger.py already
uses for job-level locking, just applied to a proposal-scoped virtual key
instead of a real job_id (no ledger.json is created for this key — it is a
lock-only claim, so it never appears in `cli.py status`'s job listing). Held
for the whole call, released in a `finally`. A second concurrent
advance-proposals process touching the same proposal is refused outright,
never races the first.

STALE RECOVERY, NOT BLIND TAKEOVER (#2/#3): `_resolve_prior_attempt()` looks
at the proposal's most recent linked job_id and asks the EXACT SAME
questions job_ledger.py already answers for any job: is it terminal-success
(reuse the result, don't re-implement)? terminal-failure (this attempt is
spent, try another if budget remains)? non-terminal but stale (classify()
via the existing recovery-policy function, resume only if SAFE_RESUME/
RESTART_FROM_SANDBOX)? non-terminal and fresh (a live process may still own
it — refuse to duplicate, re-check on a later run, never blind-takeover)?
Both OmniEngineer and Claude Code jobs now carry a job_ledger record and a
resume_job() (bridge.py closed this asymmetry) — this module is engine-
agnostic about it, reading the winning job's own `selected_engine` field
rather than assuming. A result.json-only fallback (no ledger record) is
kept for historical jobs created before bridge.py had ledger integration.

FALLBACK LINEAGE (#4): every job_id any engine attempt creates — including a
failed OmniEngineer attempt that then fell back to Claude Code within the
SAME router call — is linked to the SAME proposal via the same
on_job_created callback, so `proposal.implementation_job_ids` shows the full
lineage regardless of how many engines were tried. If NO engine completes
cleanly in a given attempt (e.g. local unavailable AND Claude quota-limited),
that is CONTROLLED ESCALATION, not permanent rejection: the proposal stays
PROPOSED (still eligible) as long as MAX_IMPLEMENTATION_ATTEMPTS hasn't been
reached, so a later advance-proposals run can try again once conditions
change — it is never silently dropped, and never retried in an unbounded
loop within one call (each router call tries each accepted engine exactly
once).

MAX_IMPLEMENTATION_ATTEMPTS (#5) counts distinct job_ids ever created for a
proposal, hard-coded, never model-influenced. Once exhausted, the proposal
is REJECTED with an explicit "bounded_attempts_exceeded" reason — no more
attempts, ever, for that proposal.

Everything else — the engine-selection scoring, the rejected_policy /
validation-failure non-retry rules, the independent canary re-validation,
Codex's structural unreachability — is UNCHANGED from the prior milestone;
see the "why this is safe to run unattended" notes preserved below.

Why this is safe to run unattended for low-risk proposals:
  - eligibility requires risk_score == "low" AND status in {observed, proposed}
    — anything else (medium/founder_gated, or already past this pipeline)
    is left alone
  - SAFE SANDBOX IMPLEMENT: at BOTH engines (_run_claude_code and
    _run_omni_engineer), source_paths is threaded from the proposal's OWN
    p.source_paths field (GOD_MODE_V1 FINAL GAP CLOSURE, extended to
    Claude Code by the GOVERNED CANONICAL SOURCE STAGING REPAIR) -- defaults
    to [] for every existing/automatic proposal (identical empty-sandbox
    behavior, unchanged), and only ever carries real paths when a caller
    EXPLICITLY authorizes them on the proposal itself; still GOVERNED by
    the same authority_policy.GATED_PATH_MARKERS check and the SAME shared
    context_staging.py default-exclusion filter at both engines (bridge.py
    and omniengineer_harness.py each apply it independently, so neither
    engine can be reached with a noisy/historical/secret-adjacent path even
    if the other engine's own filtering were ever bypassed). "No recursive
    self-modification" therefore still holds by construction for every
    proposal that does not explicitly opt in.
  - every implementing job, at every engine, still goes through
    authority_policy.classify() same as any other — a rejected_policy
    outcome is TERMINAL (deterministic across engines, never routed around).
  - AUTOMATIC VALIDATION is the same validation.py gate every other adapter
    uses; a validation failure halts advancement and rejects the proposal,
    never retried (content failure, deterministic).
  - CANARY is a second, independent validation.validate() pass, run
    unconditionally regardless of whether `job` came from a fresh attempt, a
    resume, or a reuse — catches drift between "when it last passed" and now.
  - every stage is recorded on the proposal's own history (proposal.advance())
    and in one audit.record() call per proposal per run — auditable.
  - recovery NEVER bypasses authority: job_ledger.classify() returns
    FOUNDER_REQUIRED for a job that was awaiting approval when interrupted,
    and this module never sets founder_approved=True for itself, ever.
  - no daemon: this module has no loop, thread, or scheduler. It runs when a
    human runs `cli.py advance-proposals`, processes a capped number of
    proposals, and returns. No uncontrolled spawning.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import audit
import bridge
import capability_registry
import job_ledger
import omniengineer_harness
import organ_discovery
import studio_router
import validation
from authority_policy import DEFAULT_LOW_RISK_TOOLS
from evolution import prioritization
from evolution import proposal as proposal_mod
from job_ledger import JobState, RecoveryPolicy

IMPLEMENT_TOOLS = sorted(DEFAULT_LOW_RISK_TOOLS)  # Read/Grep/Glob/Edit/Write — must include Write or nothing can be created

MAX_PROPOSALS_PER_RUN = 5  # hard ceiling regardless of what the caller asks for
IMPLEMENT_TIMEOUT_S = 120         # claude_code
OMNI_IMPLEMENT_TIMEOUT_S = 180    # omni_engineer — whole-loop budget across up to MAX_ITERATIONS model calls
MAX_IMPLEMENTATION_ATTEMPTS = 3   # harness-enforced, counts distinct job_ids ever created for one proposal — the model cannot change this


def _proposal_lock_key(proposal_id: str) -> str:
    """Reuses job_ledger's exact claim/release/lock_status primitives against
    a proposal-scoped virtual key — never a real job_id, never given a
    ledger.json record, so it never appears in cli.py status's job listing.
    Pure lock-only reuse, not a second locking system."""
    return f"proposal-impl-{proposal_id}"


@dataclass
class StageEvent:
    stage: str
    outcome: str  # ok | blocked | retried | failed
    detail: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AdvancementResult:
    proposal_id: str
    final_status: str
    stages: list[dict[str, Any]]
    implementation_job_id: str | None
    blocked_reason: str | None
    selected_engine: str | None = None
    attempt_number: int = 0


@dataclass
class EngineAttempt:
    engine: str
    outcome: str  # ran_cleanly | rejected_policy | infra_failure
    detail: str
    job_id: str | None
    job_status: str | None
    promotion_eligible: bool | None = None


@dataclass
class ImplementationOutcome:
    selected_engine: str | None
    engine_selection_reason: str
    engines_considered: list[dict[str, Any]]
    quota_state: dict[str, str]
    authority_state: dict[str, str]
    fallback_reason: str | None
    local_attempt_result: dict[str, Any] | None
    job: Any | None  # winning JobResult (bridge.JobResult or omniengineer_harness.JobResult), or None
    terminal_reject: bool
    terminal_reject_detail: str | None
    engineering_routing_reason: str = ""


def _build_task_text(p: proposal_mod.Proposal) -> str:
    # GOD_MODE_V1 FINAL GAP CLOSURE: a proposal with explicit source_paths
    # (GOVERNED, authority-checked, context-staging-filtered -- see
    # _run_omni_engineer()) gets staged real files, so the "sandbox is
    # EMPTY" claim below would be false for it. The overwhelming majority
    # of proposals (source_paths defaults to []) get byte-for-byte the same
    # text as before.
    if p.source_paths:
        sandbox_note = (
            "This sandbox has been pre-staged with SPECIFIC, EXPLICITLY AUTHORIZED canonical "
            "source file(s)/director(y/ies) relevant to this task -- inspect what is actually "
            "there before making changes. Do not assume any files exist beyond what you find "
            "staged; nothing outside this sandbox is available to you regardless."
        )
    else:
        sandbox_note = (
            "This sandbox directory is EMPTY and isolated — do not assume any other files exist "
            "anywhere, and do not attempt to reference, open, or reason about files outside this "
            "directory (there are none available to you regardless)."
        )
    return (
        f"You are implementing a small, self-contained prototype for a proposed upgrade. {sandbox_note}\n\n"
        f"Observed weakness: {p.observed_weakness}\n"
        f"Proposed upgrade: {p.proposed_upgrade}\n\n"
        "Create one small, correct, self-contained file (or a couple of files) that "
        "demonstrates/implements this upgrade as a standalone prototype. Keep it minimal. "
        "No comments explaining what you did, just the working code/content."
    )


def _eligible(p: proposal_mod.Proposal) -> tuple[bool, str]:
    if p.risk_score != "low":
        return False, f"risk_score={p.risk_score!r}, only 'low' proposals auto-advance"
    if p.status not in (proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED):
        return False, f"status={p.status!r} is not an eligible starting point"
    return True, ""


def _load_job_result_from_disk(job_id: str) -> SimpleNamespace | None:
    """Reads jobs/<job_id>/result.json (written identically by bridge.py and
    omniengineer_harness.py) and exposes exactly the attributes the rest of
    this module reads off a live JobResult — used to REUSE a prior
    successful implementation without re-running anything."""
    result_path = omniengineer_harness.JOBS_ROOT / job_id / "result.json"
    if not result_path.exists():
        return None
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return None
    return SimpleNamespace(
        job_id=data.get("job_id", job_id), status=data.get("status"),
        workdir=data.get("workdir"), files_changed=data.get("files_changed", {}),
        promotion_eligible=data.get("promotion_eligible", False),
    )


def _job_terminal_status(job_id: str) -> tuple[str, job_ledger.LedgerRecord | None, RecoveryPolicy | None]:
    """Classifies a linked job_id into: 'success' | 'failure' |
    'stale_resumable' | 'stale_not_resumable' | 'active_not_stale' |
    'unknown'. Both OmniEngineer and Claude Code jobs now carry a
    job_ledger record and reuse classify()/is_stale() directly, engine-
    agnostically. The result.json-only fallback below only fires for a
    historical job with no ledger record at all (created before bridge.py
    had ledger integration) — such a job is always 'success'/'failure'/
    'unknown', never resumable."""
    record = job_ledger.load(job_id)
    if record is not None:
        if record.state == JobState.COMPLETED.value and record.terminal_result == "succeeded":
            return "success", record, None
        if record.state in {s.value for s in job_ledger.TERMINAL_STATES}:
            return "failure", record, None
        if job_ledger.is_stale(record):
            policy = job_ledger.classify(record)
            if policy in (RecoveryPolicy.SAFE_RESUME, RecoveryPolicy.RESTART_FROM_SANDBOX):
                return "stale_resumable", record, policy
            return "stale_not_resumable", record, policy
        return "active_not_stale", record, None

    result_path = omniengineer_harness.JOBS_ROOT / job_id / "result.json"
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text())
            return ("success" if data.get("status") == "succeeded" else "failure"), None, None
        except json.JSONDecodeError:
            return "unknown", None, None
    return "unknown", None, None


def _resolve_prior_attempt(p: proposal_mod.Proposal, *, requested_by: str) -> tuple[Any | None, str, bool]:
    """Looks at the proposal's most recent linked job_id, if any. Returns
    (job_or_None, note, refuse_to_duplicate). `job` is set only when a prior
    attempt can be REUSED (terminal success) or was just RESUMED and
    succeeded. `refuse_to_duplicate=True` means: something looks still
    genuinely active and not stale — do nothing this run, don't create a
    new job, don't resume; a later run will re-check."""
    if not p.implementation_job_ids:
        return None, "no prior implementation attempt for this proposal", False

    last_job_id = p.implementation_job_ids[-1]
    status, record, policy = _job_terminal_status(last_job_id)

    if status == "success":
        job = _load_job_result_from_disk(last_job_id)
        if job is not None:
            return job, f"reusing prior successful implementation job {last_job_id} — not re-implementing", False
        return None, f"prior job {last_job_id} ledger says succeeded but result.json is missing/unreadable — treating as unknown", False

    if status == "active_not_stale":
        return None, f"prior job {last_job_id} still appears active and not stale — refusing to duplicate; will re-check on a later run", True

    if status == "stale_resumable":
        # Dispatch to the SAME engine that originally ran this job — both
        # bridge.py and omniengineer_harness.py now expose resume_job() with
        # the identical SAFE_RESUME/RESTART_FROM_SANDBOX contract.
        resume_fn = bridge.resume_job if (record and record.selected_engine == "claude_code") else omniengineer_harness.resume_job
        resumed = resume_fn(last_job_id, requested_by=requested_by)
        if resumed.status == "succeeded":
            return resumed, f"resumed stale job {last_job_id} (policy={policy.value}) — succeeded", False
        return None, f"resumed stale job {last_job_id} (policy={policy.value}) — did not complete cleanly (status={resumed.status}); will try a fresh attempt if budget remains", False

    if status == "stale_not_resumable":
        # DEFECT_SIGNAL=07387e2947ac43d5: a job that is definitively
        # stale/dead-owner (is_stale() already confirmed this) but has
        # exhausted its bounded MAX_RESUME_ATTEMPTS budget was previously
        # left non-terminal forever here -- classify() correctly refuses to
        # auto-resume it again (that bound exists on purpose), but nothing
        # ever durably reconciled the RECORD itself, so
        # find_active_by_fingerprint() kept reporting it "active" and every
        # subsequent fresh attempt was deduped against it: a permanent
        # mutated=false stalemate, the exact failure mode a second dead
        # owner (this time mid-resume) reproduces.
        #
        # NEVER applies to policy == FOUNDER_REQUIRED: a job awaiting
        # Founder approval when it died stays awaiting Founder approval --
        # "no recovery may bypass authority" (see job_ledger.py's module
        # docstring). Only the genuinely-exhausted-budget case is
        # superseded here.
        exhausted_budget = policy == RecoveryPolicy.ESCALATE and record is not None and record.resume_count >= job_ledger.MAX_RESUME_ATTEMPTS
        reason = "already auto-resumed the maximum number of times" if exhausted_budget else f"recovery policy={policy.value if policy else 'unknown'}"
        if exhausted_budget:
            # B/C/D/E: durably reconcile the stale attempt, release/supersede
            # its lock ownership, and independently reread canonical state
            # to reconfirm it's still genuinely dead — using the SAME
            # atomic claim/checkpoint/release primitives resume_job() uses,
            # so a job any live process still legitimately owns is never
            # raced or superseded (fail-closed: if the claim fails, this is
            # skipped entirely, unchanged from the old behavior).
            fresh = job_ledger.load(last_job_id)
            if fresh is not None and job_ledger.is_stale(fresh) and fresh.resume_count >= job_ledger.MAX_RESUME_ATTEMPTS:
                claimed, _broke = job_ledger.claim_or_break_stale(last_job_id, owner=requested_by)
                if claimed:
                    try:
                        job_ledger.checkpoint(
                            last_job_id, JobState.FAILED,
                            terminal_result="stale_resume_exhausted", error_class="stale_owner_unresumable",
                            note=f"durably reconciled: stale/dead-owner and auto-resume budget exhausted "
                                 f"({fresh.resume_count}/{job_ledger.MAX_RESUME_ATTEMPTS}) — superseded by a fresh implementation attempt",
                        )
                    finally:
                        job_ledger.release(last_job_id, owner=requested_by)
        # F/G: only now does the caller proceed to fresh routing -- by this
        # point the old record is either freshly terminal (dedup no longer
        # sees it) or, if we couldn't safely claim it, genuinely still
        # someone else's live concern (dedup correctly still suppresses a
        # duplicate, unchanged from prior behavior).
        return None, f"prior job {last_job_id} is stale but not resumable ({reason}); will try a fresh attempt if budget remains", False

    # "failure" or "unknown"
    return None, f"prior job {last_job_id} ended in state={status!r}; will try a fresh attempt if budget remains", False


# ---- engine runners -----------------------------------------------------

def run_local_static_validation(target_dir: str, *, requested_by: str, files_changed: dict[str, list[str]] | None = None,
                                 config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Phase S: the programmatic entry point for local_static_validation_v1
    — studio_router.select('static_validation') now returns this capability
    (local, free) ahead of claude_code_engineer_v1. Deliberately NOT shaped
    like _run_omni_engineer/_run_claude_code above: those wrap a full agent
    job (task text -> sandbox -> job_ledger record); this wraps a single,
    instant, deterministic function call — validation.validate() already IS
    the capability, unconditionally run by every engine's own job already.
    This function adds nothing new to what validate() does; it only gives
    it its own audited, directly-callable entry point (matching cli.py's
    pre-existing `validate` subcommand for humans) so a proposal/campaign
    step can request 'just validate this' without spinning up an agent."""
    target = Path(target_dir)
    if files_changed is None:
        files_changed = {"added": [str(f.relative_to(target)) for f in target.rglob("*") if f.is_file()],
                          "modified": [], "removed": []} if target.exists() else {"added": [], "modified": [], "removed": []}
    result = validation.validate(target, files_changed, config=config)
    audit.record(job_id=f"local-static-validation-{uuid.uuid4().hex[:8]}", requested_by=requested_by,
                 task_summary=f"local static validation of {target_dir}", tool_agent_selected="local_static_validation",
                 permissions_granted=["read/run-only within target_dir, ALLOWED_BINARIES={python3,bash}"],
                 files_touched={"added": [], "modified": [], "removed": []}, commands_executed=[],
                 test_results={"passed": result.passed, "checks": result.to_json()["checks"]}, risk_class="low",
                 approval_state="not_required", final_disposition="ok" if result.passed else "validation_failed",
                 lesson=f"local static validation of {target_dir}: passed={result.passed}, "
                        f"{len(result.checks)} check(s) run, {len(result.skipped)} skipped (no local/Claude model call)")
    return {"target_dir": target_dir, "passed": result.passed, "checks": result.to_json()["checks"],
            "skipped": result.skipped, "engine": "local_static_validation"}


def _run_omni_engineer(task_text: str, requested_by: str, proposal_id: str) -> tuple[EngineAttempt, Any]:
    # ROUTER_INTEGRATION (Phase 3): submit_job_auto() classifies task_text
    # complexity inside the Omni capability boundary and dispatches to
    # submit_job() (simple) or submit_job_decomposed() (complex) -- this is
    # the ONLY change to the canonical Proposal->router->engine pipeline;
    # _implementation_router()/_rank_engineering_engines()/studio_router are
    # untouched.
    #
    # GOD_MODE_V1 FINAL GAP CLOSURE: source_paths is now threaded through
    # from the proposal itself (p.source_paths, GOVERNED, authority-checked,
    # context-staging-filtered -- see omniengineer_harness.py). The
    # PROPOSAL is re-read here (a cheap, isolated disk read) rather than
    # widening the shared 3-arg _ENGINE_RUNNERS signature every engine
    # runner (including _run_claude_code, unchanged/out of this campaign's
    # scope) is called with -- keeps this a one-function, one-engine change.
    # p.source_paths defaults to [] for every existing/automatic proposal,
    # so this is a NO-OP for the overwhelming majority of proposals --
    # "no recursive self-modification" stays true by construction unless a
    # caller explicitly authorizes specific real paths on the proposal
    # itself (never inferred, never guessed, never widened by this code).
    p = proposal_mod.load(proposal_id)
    job = omniengineer_harness.submit_job_auto(
        task=task_text, requested_by=requested_by,
        timeout_s=OMNI_IMPLEMENT_TIMEOUT_S, founder_approved=False,
        source_paths=p.source_paths or None,
        on_job_created=lambda jid: proposal_mod.append_implementation_job(
            proposal_id, jid, engine="omni_engineer", note="job created (linked before execution)"),
    )
    if job.status == "rejected_policy":
        return EngineAttempt("omni_engineer", "rejected_policy", f"policy_reasons={job.policy_reasons}",
                              job.job_id, job.status), job
    if job.status == "local_model_unavailable":
        # Local Ollama is unreachable — a real, explicit infra_failure (not a
        # guess): fall back to the next accepted engine. See #7: local
        # unavailability applies existing routing/fallback policy, it never
        # silently skips the task.
        return EngineAttempt("omni_engineer", "infra_failure", job.agent_summary_or_reason,
                              job.job_id, job.status), job
    if job.status == "duplicate_suppressed":
        return EngineAttempt("omni_engineer", "infra_failure",
                              f"duplicate of an already in-flight job: {job.agent_summary_or_reason}",
                              job.job_id, job.status), job

    # TASK_MODEL_AFFINITY / provider-affinity experience learning
    # (SINGLE_MODEL_DEPENDENCY + cross-provider resilience): only worth
    # recording when failover actually happened, whether same-provider
    # (multiple models tried) or cross-provider (a second provider was
    # used at all — even a pre-flight route-around where only ONE model
    # was ever attempted, just on provider_b instead of ollama, still
    # counts) — a plain single-provider single-model success/failure is
    # already fully captured by organ_discovery.duty_check(); what's
    # genuinely NEW information here is "which model/provider ultimately
    # worked after which failed for this kind of task", which nothing
    # else records.
    attempted_models = getattr(job, "attempted_models", []) or []
    attempted_providers = getattr(job, "attempted_providers", []) or ["ollama"]
    if len(attempted_models) > 1 or len(attempted_providers) > 1:
        outcome_success = job.agent_final_action == "finish"
        proposal_mod.record_experience(
            proposal_id,
            incident_fingerprint={"symptom": f"model/provider failover occurred: models={attempted_models} providers={attempted_providers}",
                                   "error_signature": f"failover:{','.join(attempted_models)}|{','.join(attempted_providers)}",
                                   "affected_organ": "omni_engineer",
                                   "environment_context": f"final_provider={getattr(job, 'provider', 'ollama')} final_model={job.model}"},
            root_cause={"explanation": "; ".join(f"{m}: {r}" for m, r in (job.model_failure_reasons or {}).items()),
                        "confidence": "high", "evidence": job.fallback_reason or ""},
            remediation={"procedure": f"failed over to provider={getattr(job, 'provider', 'ollama')!r} model={job.model!r}",
                         "authority_required": "none", "affected_files_or_services": []},
            outcome={"success": outcome_success,
                     "performance": f"{len(attempted_models)} model(s) across {len(attempted_providers)} provider(s) attempted",
                     "side_effects": "none — same sandbox/job throughout, no duplicate implementation job"},
        )

    # SINGLE_VALIDATOR_DEPENDENCY resilience: validation.py + canary both
    # passed, but the genuinely independent recheck (evolution.
    # independent_validation) disagreed — this is exactly the "one
    # validator implementation is wrong" scenario redundancy exists to
    # catch, and it's durable, Founder-visible operational knowledge worth
    # keeping regardless of this specific job's outcome.
    ivres = getattr(job, "independent_validation", None) or {}
    if job.status == "succeeded_validator_disagreement" and ivres.get("ran"):
        proposal_mod.record_experience(
            proposal_id,
            incident_fingerprint={"symptom": f"validator disagreement: {ivres.get('reason', '')}",
                                   "error_signature": "validator_disagreement:" + ",".join(
                                       f["name"] for f in ivres.get("findings", []) if not f.get("passed")),
                                   "affected_organ": "validation.py", "environment_context": f"job={job.job_id}"},
            root_cause={"explanation": ivres.get("reason", "independent recheck disagreed with validation.py+canary"),
                        "confidence": "high", "evidence": str(ivres.get("findings", []))},
            remediation={"procedure": "promotion_eligible forced to False; job held for Founder review, never auto-promoted",
                         "authority_required": "founder_review", "affected_files_or_services": []},
            outcome={"success": False, "performance": "disagreement caught before promotion",
                     "side_effects": "none — no promotion occurred, no duplicate job created"},
        )

    if job.agent_final_action == "finish":
        return EngineAttempt("omni_engineer", "ran_cleanly", job.agent_summary_or_reason,
                              job.job_id, job.status, job.promotion_eligible), job
    return EngineAttempt("omni_engineer", "infra_failure",
                          f"agent_final_action={job.agent_final_action!r} reason={job.agent_summary_or_reason!r}",
                          job.job_id, job.status), job


def _run_claude_code(task_text: str, requested_by: str, proposal_id: str) -> tuple[EngineAttempt, Any]:
    # GOVERNED CANONICAL SOURCE STAGING REPAIR: source_paths is now threaded
    # from the proposal itself (p.source_paths), the exact same mechanism
    # _run_omni_engineer() already uses -- re-reads the proposal (a cheap,
    # isolated disk read) rather than widening the shared 3-arg
    # _ENGINE_RUNNERS signature. p.source_paths defaults to [] for every
    # existing/automatic proposal, so this is a no-op unless a caller
    # explicitly authorized specific real paths on the proposal itself.
    # bridge.submit_job()'s own context-staging filter (shared with
    # omniengineer_harness.py via context_staging.py) still applies.
    p = proposal_mod.load(proposal_id)
    job = bridge.submit_job(
        task=task_text, requested_by=requested_by, tools=IMPLEMENT_TOOLS, source_paths=p.source_paths or None,
        timeout_s=IMPLEMENT_TIMEOUT_S, founder_approved=False,
        on_job_created=lambda jid: proposal_mod.append_implementation_job(
            proposal_id, jid, engine="claude_code", note="job created (linked before execution)"),
    )
    if job.status == "rejected_policy":
        return EngineAttempt("claude_code", "rejected_policy", f"policy_reasons={job.policy_reasons}",
                              job.job_id, job.status), job
    if job.status == "duplicate_suppressed":
        return EngineAttempt("claude_code", "infra_failure",
                              f"duplicate of an already in-flight job: {job.policy_reasons}",
                              job.job_id, job.status), job
    if job.status == "succeeded":
        return EngineAttempt("claude_code", "ran_cleanly", "claude code completed",
                              job.job_id, job.status, job.promotion_eligible), job
    return EngineAttempt("claude_code", "infra_failure", f"status={job.status} error={job.error}",
                          job.job_id, job.status), job


# Exposed as a module-level dict (not a hardcoded if/elif chain) specifically
# so tests can monkeypatch one engine's runner in isolation to prove fallback
# behavior deterministically, without needing the real engine to actually
# fail (see tests/test_evolution_advance.py).
_ENGINE_RUNNERS: dict[str, Callable[[str, str, str], tuple[EngineAttempt, Any]]] = {
    "omni_engineer": _run_omni_engineer,
    "claude_code": _run_claude_code,
}

# ---- ENGINEERING ENGINE PRIORITY (Founder-authorized 2026-08-19) ---------
# studio_router.rank()'s generic Studio-wide doctrine (local/free preferred)
# is reused UNMODIFIED above for authority/quota/availability filtering, and
# every OTHER consumer of studio_router (task_router.py, forecast.py,
# organ_discovery.py, external_evolution.py, tests/test_studio_router.py)
# keeps that exact same doctrine — nothing about studio_router.py itself
# changed. This section applies a SEPARATE, engineering-specific priority
# ONLY to the two accepted engine-adapter candidates for THIS pipeline's own
# implementing work: Claude Code is the default primary coding engine (a
# genuine engineering specialist, not merely 'the paid fallback' the old
# locality-first order implied); Omni Engineer is the default fallback —
# reused, never weakened, never deleted, still the only engine ever reached
# when Claude Code is unavailable/quota-limited/rejected/repeatedly failing.
#
# AUTOMATIC FAILOVER ON REPEATED FAILURE (not just registry unavailability):
# studio_router's own accept/reject decision already excludes an engine
# whose registry status/availability/quota_status says it's down — but
# that's a PROCESS-alive signal, not a JOB-outcome signal. That is exactly
# the distinction organ_discovery.duty_check()'s 'running but not doing its
# job' silent_failure_detected exists to catch — the real claude_code PATH
# incident proved the gap: registry status stayed 'active' the entire ~2
# days claude_code was genuinely 0% successful in real production jobs. So
# this ALSO consults duty_check() and swaps Omni Engineer to the front, for
# THIS call only, when Claude Code is currently duty-flagged as silently
# failing and Omni Engineer is not. No persistent state, no circuit-breaker
# to reset — every call re-evaluates fresh from job_ledger's own history, so
# Claude Code automatically returns to the front the moment its own
# duty_check rolling window recovers.
#
# EVIDENCE-BASED, NOT A PERMANENT HARDCODED PREFERENCE: if both engines have
# a high-confidence sample (organ_discovery.DUTY_MIN_SAMPLE_FOR_CONFIDENCE
# reached) and Omni Engineer's real recent_success_rate sustainably exceeds
# Claude Code's by OMNI_PROMOTION_SUCCESS_RATE_MARGIN or more, Omni Engineer
# is preferred instead — a genuine, evidence-based promotion computed fresh
# every call, never a one-time assumption and never model-influenced. Claude
# Code remains the engine actually preferred today (2026-08-19): this branch
# exists so that could change in the future purely on sustained real
# evidence, per explicit Founder instruction.
OMNI_PROMOTION_SUCCESS_RATE_MARGIN = 0.20


def _rank_engineering_engines(accepted_engines: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    """Reorders (never adds/removes) the accepted claude_code/omni_engineer
    candidates per the policy documented above. A no-op (returns the input
    unchanged, with an explanatory reason) whenever fewer than both engines
    are present — e.g. Claude Code already excluded by studio_router for
    quota/availability reasons, in which case Omni Engineer is the only
    candidate anyway and there is nothing to reorder."""
    adapters_present = {adapter for _, adapter in accepted_engines}
    if "claude_code" not in adapters_present or "omni_engineer" not in adapters_present:
        return accepted_engines, ("only one (or neither) of claude_code/omni_engineer is an accepted candidate "
                                   "this call — no engineering-specific reordering needed")

    try:
        duty = organ_discovery.duty_check()["engines"]
    except Exception as e:  # noqa: BLE001 — a duty_check() read failure must never block routing
        # Fall through to the same default-order logic below by treating both
        # engines as having no usable evidence — NOT a bare pass-through of
        # `accepted_engines` as-is, which would silently keep studio_router's
        # raw local-first order (omni_engineer first) instead of the intended
        # Claude Code default.
        duty = {}
        duty_read_error = f"organ_discovery.duty_check() unavailable ({type(e).__name__}: {e}) — "
    else:
        duty_read_error = ""

    claude_stats = duty.get("claude_code", {})
    omni_stats = duty.get("omni_engineer", {})

    claude_broken = bool(claude_stats.get("silent_failure_detected")) and not bool(omni_stats.get("silent_failure_detected"))
    if claude_broken:
        preferred_adapter = "omni_engineer"
        reason = (f"automatic failover: duty_check() flags claude_code as silent_failure_detected "
                   f"({claude_stats.get('reason')}) while omni_engineer is not — Omni Engineer tried first this "
                   f"call; Claude Code returns to primary automatically once its own duty_check window recovers")
    elif (omni_stats.get("capability_confidence") == "high"  # Omni Engineer itself must be genuinely, well-sampled reliable
          and claude_stats.get("sample_size", 0) >= organ_discovery.DUTY_MIN_SAMPLE_FOR_CONFIDENCE  # enough claude_code sample to compare against, whatever tier it's in
          and (omni_stats.get("recent_success_rate", 0.0) - claude_stats.get("recent_success_rate", 0.0))
          >= OMNI_PROMOTION_SUCCESS_RATE_MARGIN):
        preferred_adapter = "omni_engineer"
        reason = (f"evidence-based promotion: omni_engineer has a high-confidence sample "
                   f"(recent_success_rate={omni_stats.get('recent_success_rate')}) that sustainably outperforms "
                   f"claude_code's own real recent_success_rate={claude_stats.get('recent_success_rate')} "
                   f"(sample_size={claude_stats.get('sample_size')}) by >= {OMNI_PROMOTION_SUCCESS_RATE_MARGIN} — "
                   f"preferred this call on real evidence, not assumption")
    else:
        preferred_adapter = "claude_code"
        reason = (f"{duty_read_error}default engineering priority: Claude Code primary, Omni Engineer fallback "
                   f"(claude_code duty={claude_stats.get('capability_confidence', 'unknown')}/"
                   f"{claude_stats.get('recent_success_rate')}, omni_engineer duty="
                   f"{omni_stats.get('capability_confidence', 'unknown')}/{omni_stats.get('recent_success_rate')}) "
                   f"— no evidence yet justifies preferring Omni Engineer")

    reordered = sorted(accepted_engines, key=lambda pair: 0 if pair[1] == preferred_adapter else 1)
    return reordered, reason


def _implementation_router(task_text: str, requested_by: str, proposal_id: str, *, allow_paid: bool = True) -> ImplementationOutcome:
    # allow_paid is a HARD filter applied inside studio_router.rank() itself
    # (same tier as allow_founder_gated) -- a metered/paid capability is
    # simply never in `ranked`'s accepted prefix when False, so
    # accepted_engines below can never contain one, and
    # _rank_engineering_engines() (a REORDER of accepted_engines, never an
    # add) has nothing paid to select even if its duty-cycle scoring would
    # otherwise have preferred it.
    ranked = studio_router.rank("code_edit", allow_founder_gated=False, allow_paid=allow_paid)
    engines_considered = [
        {"capability_id": ev.capability_id, "accepted": ev.accepted, "reason": ev.reason,
         "locality": ev.locality, "cost_class": ev.cost_class, "risk_class": ev.risk_class}
        for ev in ranked
    ]

    quota_state: dict[str, str] = {}
    authority_state: dict[str, str] = {}
    for ev in ranked:
        entry = capability_registry.find(ev.capability_id) or {}
        q = entry.get("quota_status") or {}
        quota_state[ev.capability_id] = (
            f"limited until {q.get('until')} ({q.get('reason')})" if q.get("limited") else "not limited"
        )
        authority_state[ev.capability_id] = "accepted" if ev.accepted else ev.reason

    accepted_engines = [
        (ev.capability_id, studio_router._CAPABILITY_TO_ADAPTER[ev.capability_id])
        for ev in ranked
        if ev.accepted and studio_router._CAPABILITY_TO_ADAPTER.get(ev.capability_id) in _ENGINE_RUNNERS
    ]
    accepted_engines, engineering_routing_reason = _rank_engineering_engines(accepted_engines)

    attempts: list[EngineAttempt] = []
    local_attempt_result: dict[str, Any] | None = None

    for i, (cap_id, adapter_name) in enumerate(accepted_engines):
        runner = _ENGINE_RUNNERS[adapter_name]
        attempt, job = runner(task_text, requested_by, proposal_id)
        attempts.append(attempt)
        if adapter_name == "omni_engineer":
            local_attempt_result = asdict(attempt)

        if attempt.outcome == "rejected_policy":
            return ImplementationOutcome(
                selected_engine=None,
                engine_selection_reason=(
                    f"{adapter_name} rejected the implementing task on policy grounds: {attempt.detail} — "
                    f"this is deterministic (every engine runs the same authority_policy.classify() on the "
                    f"same task text), so no other engine is attempted"
                ),
                engines_considered=engines_considered, quota_state=quota_state, authority_state=authority_state,
                fallback_reason=None, local_attempt_result=local_attempt_result,
                job=job, terminal_reject=True, terminal_reject_detail=attempt.detail,
                engineering_routing_reason=engineering_routing_reason,
            )

        if attempt.outcome == "ran_cleanly":
            reason = f"{cap_id} ({adapter_name}) ranked #{i + 1} of {len(accepted_engines)} and completed cleanly"
            fallback_reason = None
            if i > 0:
                prior = attempts[i - 1]
                fallback_reason = f"{prior.engine} attempt did not complete cleanly ({prior.outcome}: {prior.detail}); fell back to {adapter_name}"
                reason = f"{cap_id} ({adapter_name}) selected after fallback — " + reason
            return ImplementationOutcome(
                selected_engine=adapter_name, engine_selection_reason=reason,
                engines_considered=engines_considered, quota_state=quota_state, authority_state=authority_state,
                fallback_reason=fallback_reason, local_attempt_result=local_attempt_result,
                job=job, terminal_reject=False, terminal_reject_detail=None,
                engineering_routing_reason=engineering_routing_reason,
            )
        # infra_failure -> fall through to the next accepted engine, if any

    detail = "; ".join(f"{a.engine}: {a.outcome} ({a.detail})" for a in attempts) or "no accepted engine for task_type='code_edit'"
    return ImplementationOutcome(
        selected_engine=None,
        engine_selection_reason=f"no engine completed cleanly this attempt. {detail}",
        engines_considered=engines_considered, quota_state=quota_state, authority_state=authority_state,
        fallback_reason=detail if attempts else None, local_attempt_result=local_attempt_result,
        job=None, terminal_reject=False, terminal_reject_detail=None,
        engineering_routing_reason=engineering_routing_reason,
    )


def _escalate_if_unresolved_self_correction(p: proposal_mod.Proposal, *, reason: str, evidence: dict[str, Any]) -> None:
    """CLOSES A REAL GAP found in this milestone's live-architecture audit
    (Founder-authorized 2026-08-19): a duty_self_correction proposal
    (organ_discovery.duty_check()'s own 'this engine is running but not
    doing its job' signal — see autonomous_cycle._self_correct_duty_
    findings) that exhausts its bounded implementation attempts, or whose
    only implementation disagreed with its own independent validator, or
    failed canary, used to just become REJECTED with ZERO Founder-visible
    signal — durable in the proposal's own history, but nothing ever
    surfaced it anywhere a human would see it. This is EXACTLY the gap
    behind "MR. SILENT detected the abnormality but investigation/repair
    still required the Founder-driven session" for the real claude_code
    PATH incident: proposal 7f77e311-aed5-4418-b986-027c4e95e911, created
    and rejected autonomously by the real production cycle on 2026-08-18,
    zero escalation ever created for it. A policy-rejected proposal is
    deliberately NOT routed here — that is an authority gate working
    correctly, not a silent failure, and is already visible via
    CycleRecord.authority_blocks."""
    if p.origin != "observe_engine" or not (p.fingerprint or "").startswith("duty_self_correction:"):
        return
    try:
        from evolution import founder_request
        engine_part = p.fingerprint.split(":")[1] if p.fingerprint and ":" in p.fingerprint else "an engine"
        founder_request.request_founder_decision(
            subject=f"self-correction proposal {p.proposal_id} could not repair {engine_part}",
            finding=p.observed_weakness,
            capability_needed="human_review_of_failed_self_correction",
            reason_required=(
                f"MR. SILENT autonomously detected this engine health degradation and attempted repair via its "
                f"existing engine-routing/implementation pipeline, but {reason} — the current self-correction "
                f"pipeline runs each implementing job in an empty, isolated sandbox with no access to the real "
                f"source tree (a deliberate 'no recursive self-modification' safety boundary), so it can only "
                f"ever produce a disconnected prototype, never a genuine patch to the real affected file(s) — a "
                f"human (or a separately-authorized capability expansion) is needed to actually fix this"
            ),
            recommended_action=f"review proposal {p.proposal_id}'s history and the real affected engine/component directly",
            risk=p.risk_score, affected={"proposal_id": p.proposal_id, "fingerprint": p.fingerprint, **evidence},
            rollback_recovery=None,
        )
    except Exception:  # noqa: BLE001 — an optional escalation must never break the real pipeline
        pass


def advance_one(proposal_id: str, *, requested_by: str = "autonomous_pipeline") -> AdvancementResult:
    p = proposal_mod.load(proposal_id)
    stages: list[StageEvent] = []

    ok, reason = _eligible(p)
    stages.append(StageEvent("risk_classify", "ok" if ok else "blocked",
                              "risk_score=low, status eligible" if ok else reason))
    if not ok:
        return _finish(p, stages, None, reason, None, "n/a")

    # CONCURRENCY CLAIM (#2) — held for the whole call, released in finally.
    # Reuses job_ledger's exact claim/release/lock_status, on a proposal-
    # scoped virtual key (never a real job_id).
    lock_key = _proposal_lock_key(proposal_id)
    claimed, broke_stale_lock = job_ledger.claim_or_break_stale(lock_key, owner=requested_by)
    if not claimed:
        status = job_ledger.lock_status(lock_key)
        stages.append(StageEvent("concurrency_claim", "blocked",
                                  f"another process already holds a live implementation claim for this proposal "
                                  f"(owner={status.get('owner')!r}, pid={status.get('pid')})"))
        return _finish(p, stages, p.implementation_job_id, "concurrent_advancer_blocked", None, "n/a")
    stages.append(StageEvent("concurrency_claim", "ok",
                              f"claimed by {requested_by!r}" +
                              (" (broke a proposal-level lock left behind by a dead prior advancer)" if broke_stale_lock else "")))

    try:
        # PROPOSE: formal transition out of raw OBSERVED, if not already there.
        if p.status == proposal_mod.ProposalStatus.OBSERVED:
            p = proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED,
                                      note="auto-advanced: risk-classified low, proceeding to the implementation router")
        stages.append(StageEvent("propose", "ok", "status -> proposed"))

        # STABLE IMPLEMENTATION IDENTITY (#1) — check the proposal's existing
        # lineage before creating anything new.
        job, note, refuse = _resolve_prior_attempt(p, requested_by=requested_by)
        stages.append(StageEvent("attempt_resolution", "blocked" if refuse else "ok", note))
        if refuse:
            return _finish(p, stages, p.implementation_job_id, "prior_job_still_active", None, "n/a")

        selected_engine_label = None
        impl: ImplementationOutcome | None = None

        if job is not None:
            # Both engines now carry a job_ledger record (bridge.py closed
            # this asymmetry), so a reused/resumed job could be either —
            # derive the label from the job's own ledger record rather than
            # assuming. Falls back to "claude_code" only for a pre-this-
            # milestone historical job that genuinely has no ledger record
            # (result.json-only fallback path in _job_terminal_status).
            job_record = job_ledger.load(job.job_id)
            selected_engine_label = (job_record.selected_engine if job_record and job_record.selected_engine else "claude_code")
        else:
            attempt_count = len(p.implementation_job_ids)
            if attempt_count >= MAX_IMPLEMENTATION_ATTEMPTS:
                p = proposal_mod.advance(
                    p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                    note=f"auto-advance halted: exhausted MAX_IMPLEMENTATION_ATTEMPTS={MAX_IMPLEMENTATION_ATTEMPTS} "
                         f"({attempt_count} job(s) tried: {p.implementation_job_ids}) without a successful implementation",
                )
                proposal_mod.record_lesson(p.proposal_id, "bounded implementation attempts exhausted")
                stages.append(StageEvent("implementation_router", "blocked", "MAX_IMPLEMENTATION_ATTEMPTS exhausted"))
                _escalate_if_unresolved_self_correction(
                    p, reason=f"exhausted MAX_IMPLEMENTATION_ATTEMPTS={MAX_IMPLEMENTATION_ATTEMPTS} without a successful implementation",
                    evidence={"implementation_job_ids": p.implementation_job_ids})
                return _finish(p, stages, p.implementation_job_id, "bounded_attempts_exceeded", None, "n/a")

            task_text = _build_task_text(p)
            impl = _implementation_router(task_text, requested_by, proposal_id,
                                           allow_paid=getattr(p, "paid_resources_allowed", True))
            stages.append(StageEvent("implementation_router", "blocked" if impl.terminal_reject else "ok",
                                      impl.engine_selection_reason))

            if impl.terminal_reject:
                p = proposal_mod.advance(
                    p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                    note=f"auto-advance halted: {impl.terminal_reject_detail} "
                         f"(policy-rejected; no other engine would decide differently on the same task text)",
                )
                return _finish(p, stages, impl.job.job_id if impl.job else p.implementation_job_id,
                                "implementing task was policy-rejected", impl, "n/a")

            if impl.selected_engine is None:
                # CONTROLLED ESCALATION (#4): no engine completed cleanly this
                # attempt (e.g. local unavailable AND Claude quota-limited).
                # Not a permanent rejection unless the attempt budget is now
                # exhausted — the proposal stays PROPOSED, eligible for a
                # later run once conditions change.
                p = proposal_mod.load(p.proposal_id)  # re-load: on_job_created already appended this attempt's job_id(s)
                new_attempt_count = len(p.implementation_job_ids)
                if new_attempt_count >= MAX_IMPLEMENTATION_ATTEMPTS:
                    p = proposal_mod.advance(
                        p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                        note=f"auto-advance halted: no engine completed cleanly and MAX_IMPLEMENTATION_ATTEMPTS="
                             f"{MAX_IMPLEMENTATION_ATTEMPTS} exhausted — {impl.engine_selection_reason}",
                    )
                    proposal_mod.record_lesson(p.proposal_id, impl.engine_selection_reason)
                    _escalate_if_unresolved_self_correction(
                        p, reason=f"no engine completed cleanly and MAX_IMPLEMENTATION_ATTEMPTS={MAX_IMPLEMENTATION_ATTEMPTS} exhausted ({impl.engine_selection_reason})",
                        evidence={"implementation_job_ids": p.implementation_job_ids, "engines_considered": impl.engines_considered})
                    return _finish(p, stages, p.implementation_job_id, "bounded_attempts_exceeded", impl, "n/a")
                stages.append(StageEvent(
                    "controlled_escalation", "ok",
                    f"attempt {new_attempt_count}/{MAX_IMPLEMENTATION_ATTEMPTS} used, no engine ran cleanly this "
                    f"time; proposal remains PROPOSED and eligible for a later attempt — {impl.engine_selection_reason}",
                ))
                return _finish(p, stages, p.implementation_job_id, "no_engine_available_this_attempt", impl, "n/a")

            job = impl.job
            selected_engine_label = impl.selected_engine

        p = proposal_mod.load(p.proposal_id)  # pick up any lineage appended by on_job_created / resume
        attempt_number = len(p.implementation_job_ids) or 1

        stages.append(StageEvent("safe_sandbox_implement", "ok",
                                  f"engine={selected_engine_label}; job_id={job.job_id}; attempt={attempt_number}/{MAX_IMPLEMENTATION_ATTEMPTS}; files_changed={job.files_changed}"))
        p = proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED,
                                  note=f"job {job.job_id} succeeded via {selected_engine_label} (attempt {attempt_number})")

        # AUTOMATIC VALIDATION — already run inside the winning engine's submit_job(); read the result.
        if not job.promotion_eligible:
            # SINGLE_VALIDATOR_DEPENDENCY resilience: a genuine content
            # failure (validation.py or canary said FAIL) and a validator
            # DISAGREEMENT (validation.py+canary both said PASS, but the
            # independent recheck disagreed) are diagnostically very
            # different situations for a human reading this proposal's
            # history — the first means the code is broken, the second
            # means MR. SILENT's own validators disagreed with each other
            # about code neither individually flagged. _run_omni_engineer
            # already recorded the structured experience record for a
            # disagreement; this is the Founder-facing lesson TEXT, which
            # must not be overwritten with the generic message below.
            is_disagreement = getattr(job, "status", "") == "succeeded_validator_disagreement"
            stages.append(StageEvent("automatic_validation", "blocked",
                                      (f"validator disagreement for job {job.job_id} (engine={selected_engine_label}) — "
                                       f"validation.passed=True and canary.passed=True, but the independent recheck disagreed")
                                      if is_disagreement else
                                      f"validation.passed=False for job {job.job_id} (engine={selected_engine_label})"))
            p = proposal_mod.advance(
                p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                note=("auto-advance halted: validation.py+canary both passed, but a genuinely independent recheck "
                      "disagreed — held for Founder review, never silently promoted" if is_disagreement else
                      "auto-advance halted: automatic validation failed; not retried with a different engine either "
                      "(content failure, deterministic — a different engine's output would still need to pass the "
                      "same validators)"),
            )
            proposal_mod.record_lesson(
                p.proposal_id,
                (f"{selected_engine_label} implementation: validation.py+canary both passed, but the independent "
                 f"recheck disagreed — {(getattr(job, 'independent_validation', None) or {}).get('reason', '')}")
                if is_disagreement else
                f"{selected_engine_label} implementation failed automatic validation",
            )
            _escalate_if_unresolved_self_correction(
                p, reason=("validation.py+canary both passed but an independent recheck disagreed" if is_disagreement
                           else "the implementation failed automatic validation"),
                evidence={"implementation_job_id": job.job_id, "selected_engine": selected_engine_label})
            return _finish(p, stages, job.job_id, "validator disagreement" if is_disagreement else "validation failed",
                            impl, selected_engine_label)

        stages.append(StageEvent("automatic_validation", "ok", f"validation passed (engine={selected_engine_label})"))
        p = proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED,
                                  note=f"validation passed for job {job.job_id}")

        # CANARY — independent second pass over the SAME sandbox content, run
        # unconditionally regardless of whether `job` was fresh/resumed/reused.
        canary = validation.validate(Path(job.workdir), job.files_changed, config=None)
        if not canary.passed:
            stages.append(StageEvent("canary", "blocked", "independent re-validation did not reproduce PASS"))
            p = proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                                      note="auto-advance halted: canary re-validation failed (drift/non-determinism)")
            _escalate_if_unresolved_self_correction(
                p, reason="canary (independent re-validation) failed to reproduce the initial PASS",
                evidence={"implementation_job_id": job.job_id, "selected_engine": selected_engine_label})
            return _finish(p, stages, job.job_id, "canary failed", impl, selected_engine_label)

        stages.append(StageEvent("canary", "ok", "independent re-validation confirmed PASS"))
        p = proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note="canary passed")

        # Phase U — bounded local second-opinion review. Only fires on a
        # real trigger (needs_second_opinion), never for every job. Never
        # blocks PROMOTION_CANDIDATE (a human already reviews before any
        # real --founder-approved promotion) — a disagreement is recorded
        # loudly in the proposal's own history/experience so that human
        # review sees it, exactly the "flag, never silently override or
        # silently bypass" contract local_review.py documents.
        try:
            from evolution import local_review
            should_review, review_reason = local_review.needs_second_opinion(job, risk_class=p.risk_score)
            stages.append(StageEvent("local_second_opinion", "triggered" if should_review else "skipped", review_reason))
            if should_review:
                review = local_review.get_second_opinion(
                    summary=f"files_changed={job.files_changed}", objective=p.observed_weakness, requested_by=requested_by)
                resolution = local_review.resolve_disagreement(review, proposal_id=p.proposal_id)
                # Experience learning (mission requirement): record which
                # reviewer/engine combination produced agreement vs
                # disagreement, regardless of outcome — future routing
                # decisions (which engine for which task, which reviewer
                # is useful vs noisy) need BOTH signals, not just failures.
                proposal_mod.record_experience(
                    p.proposal_id,
                    incident_fingerprint={"symptom": f"local second-opinion review triggered ({review_reason})",
                                           "error_signature": f"local_review:{review.get('status')}:{resolution['action']}",
                                           "affected_organ": "local_review", "environment_context": f"engine={selected_engine_label}, reviewer_model={review.get('model')}"},
                    root_cause={"explanation": review_reason, "confidence": "high", "evidence": str(review.get("raw_response", ""))[:300]},
                    remediation={"procedure": "n/a — review only, never auto-applied", "authority_required": "none",
                                 "affected_files_or_services": []},
                    outcome={"success": resolution["action"] == "no_disagreement", "performance": "n/a",
                             "side_effects": "flagged for human review, not blocked" if resolution["action"] == "flagged_for_review" else "none"},
                )
                if resolution["action"] == "flagged_for_review":
                    proposal_mod.record_lesson(
                        p.proposal_id, f"Phase U local second opinion ({review.get('model')}) raised concerns: "
                                       f"{resolution['reason']} — proposal still reaches PROMOTION_CANDIDATE but "
                                       f"human review should weigh this before any --founder-approved promotion")
        except Exception as e:  # noqa: BLE001 — an optional review step must never break the real pipeline
            stages.append(StageEvent("local_second_opinion", "error", f"{type(e).__name__}: {e}"))

        # PROMOTION CANDIDATE (#7) — terminal state of THIS pipeline, reached
        # identically regardless of how `job` was obtained (fresh/resumed/
        # reused). Actual production promotion is a separate, human-run,
        # founder-gated action; recovery never implies promotion authorization.
        p = proposal_mod.advance(
            p.proposal_id, proposal_mod.ProposalStatus.PROMOTION_CANDIDATE,
            note=f"ready for human-directed promotion: implemented via {selected_engine_label}, sandbox at {job.workdir}, "
                 f"run `cli.py promote {job.job_id} --target <path> --founder-approved` to actually promote",
        )
        stages.append(StageEvent("promotion_candidate", "ok",
                                  "awaiting human-chosen target + --founder-approved; nothing promoted"))

        # Founder Communication (Founder-authorized 2026-08-18): a concise,
        # human-readable framing of this exact gate, alongside the
        # machine-readable payload — purely additive, no new notification
        # channel (see evolution/founder_request.py). Deduped by
        # (proposal, "production_promotion") — repeat cycles that revisit
        # the same still-pending promotion candidate update the SAME
        # record rather than spamming a new one.
        try:
            from evolution import founder_request
            founder_request.request_founder_decision(
                subject=f"proposal {p.proposal_id}", finding=p.observed_weakness,
                capability_needed="production_promotion",
                reason_required="writing outside a job sandbox into /opt/pulse5-core is always founder-gated, regardless of risk_class",
                recommended_action=f"promote job {job.job_id} (implemented via {selected_engine_label}) to a real path",
                risk=p.risk_score, affected={"proposal_id": p.proposal_id, "job_id": job.job_id, "files_changed": job.files_changed},
                rollback_recovery="promotion.py backs up any overwritten file before writing; rollback(promotion_id) reverses a granted promotion",
            )
        except Exception as e:  # noqa: BLE001 — an optional communication step must never break the real pipeline
            stages.append(StageEvent("founder_communication", "error", f"{type(e).__name__}: {e}"))

        return _finish(p, stages, job.job_id, None, impl, selected_engine_label)
    finally:
        job_ledger.release(lock_key, owner=requested_by)


def _finish(p: proposal_mod.Proposal, stages: list[StageEvent], job_id: str | None,
            blocked_reason: str | None, impl: ImplementationOutcome | None, selected_engine_label: str) -> AdvancementResult:
    implementation_routing = {
        "proposal_id": p.proposal_id,
        "implementation_job_ids": p.implementation_job_ids,
        "attempt_number": p.implementation_attempts,
        "selected_engine": selected_engine_label if selected_engine_label != "n/a" else (impl.selected_engine if impl else None),
        "previous_attempt": p.implementation_job_ids[-2] if len(p.implementation_job_ids) >= 2 else None,
    }
    if impl is not None:
        implementation_routing.update({
            "engine_selection_reason": impl.engine_selection_reason,
            "engines_considered": impl.engines_considered,
            "quota_state": impl.quota_state,
            "authority_state": impl.authority_state,
            "fallback_reason": impl.fallback_reason,
            "local_attempt_result": impl.local_attempt_result,
            "engineering_routing_reason": impl.engineering_routing_reason,
        })
    audit.record(
        job_id=job_id or f"proposal-{p.proposal_id}",
        requested_by="autonomous_pipeline",
        task_summary=f"auto-advance proposal {p.proposal_id[:8]}: {p.observed_weakness[:120]}",
        # Deliberately NOT the winning engine's own name (e.g. "omni_engineer") —
        # that engine already recorded its own audit entry for this exact job_id
        # inside its own submit_job()/_finalize(). Reusing the engine name here
        # too would double-count this job_id under observe.py's
        # ENGINEERING_AGENT_ADAPTERS-scoped health signals. selected_engine is
        # still fully recorded below, inside implementation_routing.
        tool_agent_selected="proposal_advancement",
        permissions_granted=["sandbox-implement (no source_paths, no gated tools)"],
        files_touched={},
        commands_executed=[],
        test_results={"stages": [asdict(s) for s in stages], "implementation_routing": implementation_routing},
        risk_class=p.risk_score,
        approval_state="not_required",
        final_disposition=p.status,
        lesson=blocked_reason,
    )
    return AdvancementResult(
        proposal_id=p.proposal_id, final_status=p.status,
        stages=[asdict(s) for s in stages],
        implementation_job_id=job_id, blocked_reason=blocked_reason,
        selected_engine=implementation_routing["selected_engine"],
        attempt_number=p.implementation_attempts,
    )


def advance_eligible(limit: int = 3, *, requested_by: str = "autonomous_pipeline",
                      deadline_monotonic: float | None = None) -> list[AdvancementResult]:
    """Human-invoked entrypoint (via cli.py). Processes at most
    min(limit, MAX_PROPOSALS_PER_RUN) proposals per call — never unbounded.

    `deadline_monotonic` (a time.monotonic() timestamp) is PRE-24x7
    CERTIFICATION hardening: autonomous_cycle.py's own MAX_CYCLE_WALLCLOCK_S
    is checked only between phases, and this whole function is one blocking
    call from that caller's point of view — worst case, MAX_PROPOSALS_PER_RUN
    proposals could each spend their own full per-engine timeout budget
    (OMNI_IMPLEMENT_TIMEOUT_S / IMPLEMENT_TIMEOUT_S, across up to
    MAX_IMPLEMENTATION_ATTEMPTS attempts) back-to-back, multiplying the
    intended cycle ceiling several times over. This does NOT preempt a
    proposal that is already mid-implementation (no in-flight subprocess is
    ever killed here — each job's own timeout_s remains the real backstop for
    that); it only stops STARTING new proposals once the budget is already
    spent, so the multiplicative blowup across proposals is bounded to
    roughly one more proposal's worst case, not `limit` of them.

    Phase Q: candidates are ordered by evolution.prioritization.rank()
    before the `limit` slice is taken — WHICH proposals are eligible and HOW
    MANY get processed are unchanged; only the order changes, so a genuine
    Founder priority or a broad, multi-signal root-cause proposal is spent
    on first, ahead of a narrower single-signal one, within the exact same
    per-cycle budget."""
    limit = min(limit, MAX_PROPOSALS_PER_RUN)
    candidates = prioritization.rank([p for p in proposal_mod.list_all() if _eligible(p)[0]])
    results = []
    for p in candidates[:limit]:
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            results.append(_finish(p, [StageEvent("wallclock_budget", "blocked",
                                                    "cycle wallclock budget already exhausted by an earlier proposal "
                                                    "in this same batch; deferred to a later cycle, not started")],
                                    p.implementation_job_id, "cycle_wallclock_budget_exhausted", None, "n/a"))
            continue
        results.append(advance_one(p.proposal_id, requested_by=requested_by))
    return results
