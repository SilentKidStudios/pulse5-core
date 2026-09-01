"""
OmniEngineer Harness — orchestrator for OmniEngineer V0.1's TARGET LOOP, now
wrapped in a durable, crash-safe operational layer:

    TASK -> durable job record -> authority/risk check -> local-model health
    check -> checkpoint -> SANDBOX -> PLAN -> per-iteration checkpoint
    (LIST/SEARCH/READ -> PATCH -> TEST -> INSPECT FAILURE -> BOUNDED RETRY)
    -> VALIDATE -> CANARY -> PROMOTION CANDIDATE -> durable terminal state

Structurally parallel to bridge.py and local_model_bridge.py: every run gets a
UUID job_id and an isolated jobs/<job_id>/workdir, is classified by
authority_policy.classify() before anything happens, has its sandbox
snapshotted before/after, is validated by the same validation.py gate every
other adapter uses, and writes the same result.json shape (status,
workdir, files_changed, promotion_eligible, validation) that bridge.py and
local_model_bridge.py write — so promotion.py, rollback, and the audit trail
all work on an OmniEngineer job with zero changes to any of those modules.
`result.json` is still written exactly once, at the very end, unchanged.

NEW this milestone: `job_ledger.py` is written to INCREMENTALLY, at every
meaningful checkpoint (CREATED/AUTHORIZED/SANDBOX_READY/PLANNING/per-iteration
EDITING-TESTING-REPAIRING/VALIDATING/CANARY/PROMOTION_CANDIDATE/terminal) —
see that module's docstring for the full crash-safety and recovery-policy
reasoning. `_execute()` is the single shared execution body both `submit_job()`
(fresh job) and `resume_job()` (recovering an interrupted one) call, so
recovery reuses the exact same authority/health/validate/canary logic rather
than a second, drifted copy of it.

PLAN and the LIST/SEARCH/READ -> PATCH -> TEST -> INSPECT FAILURE loop itself
live in omniengineer_agent.py; this module owns everything around that loop:
sandbox setup, the best-effort plan-generation pre-step, snapshot diffing,
VALIDATE, CANARY, bounded whole-loop retry, ledger checkpointing, and
audit/result recording.

GOVERNED source_paths / allowed_tools (Founder-authorized 2026-08-20,
OMNI_ENGINEER_REAL_SOURCE_REPAIR_PARITY): submit_job() now optionally
accepts `source_paths` (real files, explicitly authorized by the caller,
copied into THIS job's isolated sandbox before the agent loop starts —
identical mechanism to bridge.py's own copy_source_paths step, same
authority_policy.classify() check, same GATED_PATH_MARKERS enforcement) and
`allowed_tools` (a real, structural restriction on the agent's tool loop —
see omniengineer_agent.py's run_agent_loop(allowed_tools=...) — not merely a
prompt instruction). Without either argument, behavior is byte-for-byte
unchanged from before this milestone: every run still starts from an empty
sandbox and can only create new, self-contained content, exactly like
evolution/advance.py's SAFE SANDBOX IMPLEMENT step still relies on for its
own (unrelated, still-empty-sandbox) implementation attempts. The model
still NEVER receives real filesystem access outside its one sandbox
directory — it edits COPIES of authorized real files, never the real files
themselves; the only path from a sandbox to a real file remains promotion.py's
existing, unmodified, always-founder-gated promote(). This closes the real,
honestly-disclosed gap the prior GOVERNED_AUTONOMOUS_REPAIR_CAPABILITY
milestone found: evolution/repair.py could only use Claude Code for real-
source repair work because this harness had no equivalent capability at all.

Because a local 30B model driving its own tool loop is less proven than
Claude Code or Codex's own agent harnesses, OmniEngineer applies a STRICTER
promotion bar than bridge.py/local_model_bridge.py: promotion_eligible
requires both the first validation pass AND an independent CANARY
re-validation pass to agree — a single lucky validation pass is not enough.

Doctrine carried over unchanged from every other adapter in this project:
no Bash/network/tool authority is ever granted to the model directly (its
sandboxed tool loop is internal to omniengineer_agent.py, not an
authority_policy `tools` grant); GATED_KEYWORDS/GATED_PATH_MARKERS still
apply to the task description and every path the loop touches; nothing here
writes outside jobs/<job_id>/workdir; nothing here promotes anything —
promotion.py's existing founder-gated `promote --founder-approved` is the
only path from a sandbox to a real file, untouched by this module. Recovery
never bypasses authority: resume_job() re-runs authority_policy.classify()
from scratch on every resume, and a job that was FOUNDER_REQUIRED before
interruption stays FOUNDER_REQUIRED after it (see job_ledger.classify()).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import audit
import job_ledger
from engine_identity import (
    OMNI_ENGINEER_ID,
    OMNI_ENGINEER_CANONICAL_NAME,
)
import local_model_health
import validation
from evolution import independent_validation
from authority_policy import classify
from job_ledger import JobState, RecoveryPolicy
from local_model_bridge import DEFAULT_MODEL, OLLAMA_BASE_URL
from omniengineer_agent import MAX_ITERATIONS, run_agent_loop

BRIDGE_ROOT = Path(__file__).resolve().parent
JOBS_ROOT = BRIDGE_ROOT / "jobs"


def _filter_actually_installed(candidates: list[str], installed_models: list[str]) -> tuple[list[str], list[str]]:
    """(really_installed, not_installed). OMNI_GOD_MODE_V1 fallback-truth fix:
    local_model_health.engineering_failover_order() is a static configured
    list -- it does not itself check whether a candidate is actually
    installed (real incident: job 6978adf2 burned a full model-call attempt
    on gpt-oss:20b, which had no local Ollama manifest at all, before
    reporting provider_b_unavailable). installed_models should be the REAL
    list from a fresh local_model_health.check().models (GET /api/tags,
    read-only) -- never advertise a config-only entry as an operational
    fallback. Does not modify local_model_health.py or its static ordering;
    only filters what the caller actually attempts. Matching mirrors
    HealthStatus.default_model_present's own convention (exact tag, or same
    base name before the ':')."""
    really_installed, not_installed = [], []
    for c in candidates:
        base = c.split(":")[0] + ":"
        if any(m == c or m.startswith(base) for m in installed_models):
            really_installed.append(c)
        else:
            not_installed.append(c)
    return really_installed, not_installed
DEFAULT_TIMEOUT_S = 600   # whole-loop wall-clock budget across up to MAX_ITERATIONS model calls
MAX_TIMEOUT_S = 1800
PLAN_TIMEOUT_S = 60

# Infra-looking failures worth exactly one bounded whole-loop retry — never a
# validation failure (deterministic/content, not transient) and never a clean
# "escalate" (the model explicitly said it can't proceed; retrying the exact
# same task is unlikely to help and burns another full iteration budget).
RETRYABLE_FINAL_ACTIONS = frozenset({"iteration_ceiling_reached", "model_unavailable", "error", "timeout"})

# omniengineer_agent tool -> the ledger JobState it most closely represents,
# for per-iteration checkpointing. finish/escalate are deliberately absent —
# the harness sets the real terminal/near-terminal state itself once the
# loop returns, rather than guessing from inside the callback.
_TOOL_TO_STATE = {
    "list_files": JobState.PLANNING, "read_file": JobState.PLANNING,
    "grep": JobState.PLANNING, "inspect_diff": JobState.PLANNING,
    "write_file_sandbox": JobState.EDITING,
    "apply_patch_sandbox": JobState.REPAIRING,
    "run_command": JobState.TESTING, "run_validator": JobState.TESTING,
}

RESUME_NOTE = (
    "\n\nNOTE: this sandbox may already contain partial progress from an "
    "interrupted prior attempt at this exact task. Use list_files and "
    "read_file FIRST to inspect what is already there before writing or "
    "patching anything — do not blindly redo work that may already be done."
)


@dataclass
class JobResult:
    job_id: str
    task: str
    adapter: str
    model: str
    risk_class: str
    approval_state: str
    status: str
    workdir: str
    started_at: str
    ended_at: str | None
    duration_s: float | None
    files_changed: dict[str, list[str]]
    plan_text: str | None
    agent_final_action: str | None
    agent_summary_or_reason: str | None
    retried: bool
    turns: list[dict[str, Any]] = field(default_factory=list)
    commands_executed: list[str] = field(default_factory=list)
    error: str | None = None
    policy_reasons: list[str] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    canary: dict[str, Any] | None = None
    promotion_eligible: bool = False
    engine_name: str = OMNI_ENGINEER_CANONICAL_NAME
    attempted_models: list[str] = field(default_factory=list)  # every model tried, in order (SINGLE_MODEL_DEPENDENCY resilience)
    model_failure_reasons: dict[str, str] = field(default_factory=dict)  # {model: why it was abandoned}
    fallback_reason: str | None = None  # why failover moved past the primary model, if it did
    provider: str = "ollama"  # FINAL provider that actually served this job (OLLAMA_SINGLE_PROVIDER_DEPENDENCY resilience)
    attempted_providers: list[str] = field(default_factory=lambda: ["ollama"])  # every provider tried, in order
    failure_classification: str | None = None  # most recent local_model_health.classify_failure() value, if any failure occurred
    independent_validation: dict[str, Any] | None = None  # SINGLE_VALIDATOR_DEPENDENCY: evolution.independent_validation.recheck() result, if run
    worker_backend: str | None = None


def _snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    return {"added": added, "modified": modified, "removed": removed}


def _generate_plan(task: str, *, model: str, timeout_s: int) -> str | None:
    """A single, best-effort, non-tool model call to produce a short plan before
    the bounded tool loop starts. Never blocks the run: any failure here just
    means the loop proceeds with the raw task text alone (visible in the
    result as plan_text=None), not a fatal error."""
    prompt = (
        "You are planning a small, self-contained sandboxed coding task. "
        "In 3-6 short numbered steps, outline your plan. Do not write any code yet, "
        "just the plan as plain text.\n\nTASK:\n" + task
    )
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode())
            return body.get("response", "").strip() or None
    except Exception:  # noqa: BLE001 — best-effort only, never fatal
        return None


def _duplicate_result(existing: job_ledger.LedgerRecord, task: str, model: str, requested_by: str) -> JobResult:
    audit.record(
        job_id=existing.job_id, requested_by=requested_by,
        task_summary=task[:200], tool_agent_selected=OMNI_ENGINEER_ID,
        permissions_granted=[], files_touched=existing.files_touched, commands_executed=[],
        test_results=None, risk_class=existing.risk_class or "n/a",
        approval_state=existing.approval_state or "n/a",
        final_disposition="duplicate_suppressed",
        lesson=f"suppressed: task already in-flight as job {existing.job_id} (state={existing.state})",
    )
    return JobResult(
        job_id=existing.job_id, task=task, adapter=OMNI_ENGINEER_ID, model=model,
        risk_class=existing.risk_class or "", approval_state=existing.approval_state or "",
        status="duplicate_suppressed", workdir=existing.sandbox_path,
        started_at=existing.created_at, ended_at=None, duration_s=None,
        files_changed=existing.files_touched, plan_text=None,
        agent_final_action=None,
        agent_summary_or_reason=f"identical task already in-flight as job {existing.job_id} (state={existing.state}); this submission was not started",
        retried=False, policy_reasons=[],
    )



def _json_safe_submit_value(value, *, field="submit_params"):
    """Normalize public durable submission values into deterministic JSON-safe forms."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, (list, tuple)):
        return [
            _json_safe_submit_value(item, field=f"{field}[]")
            for item in value
        ]

    if isinstance(value, (set, frozenset)):
        import json as _json

        normalized = [
            _json_safe_submit_value(item, field=f"{field}[]")
            for item in value
        ]

        return sorted(
            normalized,
            key=lambda item: _json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )

    if isinstance(value, dict):
        normalized = {}

        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{field}: mapping keys must be strings, got "
                    f"{type(key).__name__}"
                )

            normalized[key] = _json_safe_submit_value(
                item,
                field=f"{field}.{key}",
            )

        return normalized

    raise TypeError(
        f"{field}: unsupported durable submission value type "
        f"{type(value).__name__}"
    )

def submit_job(
    task: str,
    *,
    requested_by: str = "unspecified",
    timeout_s: int = DEFAULT_TIMEOUT_S,
    founder_approved: bool = False,
    model: str = DEFAULT_MODEL,
    max_iterations: int = MAX_ITERATIONS,
    validation_config: dict[str, Any] | None = None,
    on_job_created: Callable[[str], None] | None = None,
    source_paths: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> JobResult:
    # IDEMPOTENCY (#3): an identical task already actively in-flight is
    # suppressed BEFORE any job_id/sandbox/ledger is even created — nothing
    # to clean up, nothing partially created.
    fp = job_ledger.task_fingerprint(task)
    existing = job_ledger.find_active_by_fingerprint(fp)
    if existing is not None:
        return _duplicate_result(existing, task, model, requested_by)

    job_id = str(uuid.uuid4())
    # Linked to any caller-side durable record (e.g. a self-evolution
    # proposal's implementation lineage) THE INSTANT the job_id exists, not
    # after this whole call returns — closes the "crash after routing,
    # before the attempt finishes" duplicate-implementation gap: even if the
    # process dies one line later, the caller's own record already knows
    # this job_id and can resume/reuse it instead of creating another.
    if on_job_created:
        try:
            on_job_created(job_id)
        except Exception:  # noqa: BLE001 — a caller's linking hook must never break job execution
            pass
    workdir = JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    timeout_s = min(timeout_s, MAX_TIMEOUT_S)
    job_ledger.create(job_id, task=task, requested_by=requested_by, sandbox_path=str(workdir),
                       model=model, max_iterations=max_iterations,
                       submit_params={"source_paths": _json_safe_submit_value(
                                          source_paths or [], field="source_paths"
                                      ),
                                      "allowed_tools": _json_safe_submit_value(
                                          allowed_tools, field="allowed_tools"
                                      ),
                                      "validation_config": validation_config})
    return _execute(
        job_id, workdir, task, requested_by=requested_by, timeout_s=timeout_s,
        founder_approved=founder_approved, model=model, max_iterations=max_iterations,
        validation_config=validation_config,
        source_paths=[Path(p) for p in (source_paths or [])],
        allowed_tools=frozenset(allowed_tools) if allowed_tools is not None else None,
        copy_source_paths=True,
    )


def resume_job(job_id: str, *, requested_by: str = "recovery") -> JobResult:
    """Recovery entrypoint (#8). Never blindly repeats an external/
    destructive action — see job_ledger.py's module docstring for the full
    SAFE_RESUME vs RESTART_FROM_SANDBOX reasoning. Re-derives authority from
    scratch (authority_policy.classify() runs again inside _execute()); a
    job that needed Founder approval before interruption still needs it."""
    record = job_ledger.load(job_id)
    if record is None:
        raise ValueError(f"no ledger record for job {job_id} — nothing to resume")

    policy = job_ledger.classify(record)
    if policy == RecoveryPolicy.TERMINAL_FAILURE:
        return _recovery_refusal(record, policy, "job already reached a terminal state; nothing to resume")
    if policy == RecoveryPolicy.FOUNDER_REQUIRED:
        return _recovery_refusal(record, policy, "job was awaiting Founder approval when interrupted — recovery cannot bypass that; a human must decide")
    if policy == RecoveryPolicy.ESCALATE:
        reason = ("already auto-resumed the maximum allowed number of times" if record.resume_count >= job_ledger.MAX_RESUME_ATTEMPTS
                   else "heartbeat is not yet stale — a live process may still own this job; resume refused to avoid racing it")
        return _recovery_refusal(record, policy, reason)

    if not job_ledger.claim(job_id, owner=requested_by):
        status = job_ledger.lock_status(job_id)
        return _recovery_refusal(record, RecoveryPolicy.ESCALATE,
                                  f"another process already holds the claim on this job (owner={status.get('owner')!r}, pid={status.get('pid')}); refusing to resume concurrently")

    try:
        job_ledger.checkpoint(job_id, record.state, resume_count=record.resume_count + 1,
                               note=f"resume attempt started (policy={policy.value})")
        task = record.task
        if policy == RecoveryPolicy.RESTART_FROM_SANDBOX:
            task = record.task + RESUME_NOTE
        params = record.submit_params or {}
        allowed = params.get("allowed_tools")
        return _execute(
            job_id, Path(record.sandbox_path), task, requested_by=requested_by,
            timeout_s=DEFAULT_TIMEOUT_S, founder_approved=False,
            model=record.model or DEFAULT_MODEL, max_iterations=record.max_iterations or MAX_ITERATIONS,
            validation_config=params.get("validation_config"), is_resume=True,
            source_paths=[Path(p) for p in (params.get("source_paths") or [])],
            allowed_tools=frozenset(allowed) if allowed is not None else None,
            # SAFE_RESUME: nothing has run yet, safe to (re-)copy source_paths.
            # RESTART_FROM_SANDBOX: the model may already have partially
            # edited the copies — never overwrite them, exactly like
            # bridge.py's identical resume-safety rule.
            copy_source_paths=(policy == RecoveryPolicy.SAFE_RESUME),
        )
    finally:
        job_ledger.release(job_id, owner=requested_by)


def _recovery_refusal(record: job_ledger.LedgerRecord, policy: RecoveryPolicy, reason: str) -> JobResult:
    return JobResult(
        job_id=record.job_id, task=record.task, adapter=OMNI_ENGINEER_ID, model=record.model or "",
        risk_class=record.risk_class or "", approval_state=record.approval_state or "",
        status=f"resume_refused_{policy.value}", workdir=record.sandbox_path,
        started_at=record.created_at, ended_at=None, duration_s=None,
        files_changed=record.files_touched, plan_text=None,
        agent_final_action=None, agent_summary_or_reason=reason, retried=False, policy_reasons=[],
    )


# OMNI_REAL_SOURCE_PATH_LINEAGE_V2
def _source_sandbox_destination(workdir: Path, src: Path) -> Path:
    """Return the sandbox destination for an authorized source path.

    Canonical Omni Engineer source living beneath BRIDGE_ROOT keeps its
    project-relative directory lineage. This prevents mature source such as
    evolution/advance.py from being flattened into workdir/advance.py.

    Sources outside this project retain the historical basename behavior;
    callers that need an external tree preserved can provide that directory
    itself as source_paths.
    """
    resolved_src = src.resolve()
    resolved_root = BRIDGE_ROOT.resolve()

    try:
        relative = resolved_src.relative_to(resolved_root)
    except ValueError:
        return workdir / src.name

    return workdir / relative


def _execute(
    job_id: str, workdir: Path, task: str, *, requested_by: str, timeout_s: int,
    founder_approved: bool, model: str, max_iterations: int,
    validation_config: dict[str, Any] | None, is_resume: bool = False,
    source_paths: list[Path] | None = None, allowed_tools: frozenset[str] | None = None,
    copy_source_paths: bool = False,
) -> JobResult:
    """Shared execution body for both a fresh submit_job() and a resume_job()
    — authority check, local-model health check, the agent loop (with
    per-iteration ledger checkpointing), validate, canary, and final
    result/audit recording, all in one place so recovery can never drift
    from a fresh run's behavior."""
    source_paths = source_paths or []
    if not is_resume:
        job_ledger.claim(job_id, owner=requested_by)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    try:
        decision = classify(
            task_description=task,
            requested_tools=set(),   # the model is never granted authority_policy "tools" —
                                      # its sandboxed tool loop is internal to omniengineer_agent.py
            sandbox_root=workdir,
            source_paths=source_paths,  # real GATED_PATH_MARKERS/self-modification-jail check, same as bridge.py
            founder_approved=founder_approved,
            adapter=OMNI_ENGINEER_ID,  # not in GATED_ADAPTERS -> stays LOW unless task text/paths themselves are gated
        )
        job_ledger.checkpoint(job_id, JobState.AUTHORIZED, risk_class=decision.risk_class.value,
                               approval_state=decision.approval_state.value,
                               authority_state="granted" if decision.may_execute else "denied")

        if not decision.may_execute:
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="rejected_policy", error_class="authority")
            result = JobResult(
                job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=model,
                risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                status="rejected_policy", workdir=str(workdir),
                started_at=started_at, ended_at=started_at, duration_s=0.0,
                files_changed={"added": [], "modified": [], "removed": []},
                plan_text=None, agent_final_action=None, agent_summary_or_reason=None, retried=False,
                policy_reasons=decision.reasons,
            )
            _finalize(result, requested_by=requested_by)
            return result

        job_ledger.checkpoint(job_id, JobState.ROUTED, selected_engine=OMNI_ENGINEER_ID)

        # SAME job/proposal lineage across provider/model failover (#resume):
        # a resumed job must not forget which providers/models it already
        # tried before crashing — otherwise "each model attempted at most
        # once per bounded job" could be violated by re-offering an
        # already-failed provider/model as if it were fresh. The ledger
        # record (loaded fresh here, not threaded through every call site)
        # is the durable source of truth for this, exactly like every other
        # resume-safety property in this module.
        prior = job_ledger.load(job_id) if is_resume else None
        attempted_models = list(prior.attempted_models) if prior and prior.attempted_models else []
        model_failure_reasons: dict[str, str] = dict(prior.model_failure_reasons) if prior and prior.model_failure_reasons else {}
        attempted_providers = list(prior.attempted_providers) if prior and getattr(prior, "attempted_providers", None) else []
        fallback_reason: str | None = None
        retried = bool(attempted_models)

        # LOCAL MODEL HEALTH (#7) — fail fast, before the (much slower,
        # timeout-bound) agent loop, if Ollama itself is unreachable. Also
        # consults Ollama's own circuit breaker: a provider already
        # confirmed down across recent jobs is treated as unavailable here
        # too, without a redundant network round-trip (requirement #6, "do
        # not hammer a dead provider every cycle/job"). Never downloads/
        # starts/stops/modifies anything — read-only GET.
        health = local_model_health.check(model=model)
        ollama_available = health.available and not local_model_health.circuit_is_open("ollama")
        if not health.available:
            local_model_health.record_provider_outcome("ollama", success=False)

        current_provider = "ollama"
        current_model = model
        # TASK30B2_COMPUTE_PREFLIGHT
        (
            _compute_decision,
            current_provider,
            current_model,
        ) = _task30b2_compute_preflight(
            current_provider=current_provider,
            current_model=current_model,
            validation_config=validation_config,
        )
        pre_flight_note: str | None = None

        if not ollama_available:
            # CROSS_PROVIDER_FAILOVER at pre-flight (Founder-authorized
            # 2026-08-18): Ollama itself is down/circuit-open before even
            # one attempt this job — route directly to the genuinely
            # independent provider_b (provider_b_bridge.py, a standalone
            # llama-server process NOT dependent on the ollama daemon)
            # instead of failing the whole job outright on an Ollama outage
            # provider_b need not share.
            import provider_b_bridge
            provider_b_artifact_error: str | None = None
            if ("provider_b" not in attempted_providers
                    and not local_model_health.circuit_is_open("provider_b")):
                try:
                    pb_health = provider_b_bridge.ensure_running()
                except provider_b_bridge.ModelArtifactMissing as exc:
                    # Optional Provider B has no usable Pulse-local model
                    # artifact. This is backend unavailability, not an
                    # unhandled Omni Engineer runtime failure.
                    pb_health = None
                    provider_b_artifact_error = str(exc)
                    local_model_health.record_provider_outcome(
                        "provider_b", success=False
                    )
            else:
                pb_health = None

            if (provider_b_artifact_error is None
                    and ((_compute_decision.defer)
                         or (pb_health is not None and pb_health.available))):
                current_provider = "provider_b"
                current_model = provider_b_bridge.DEFAULT_MODEL
                pre_flight_note = (f"ollama unavailable at pre-flight ({health.error}) — "
                                    f"routing directly to independent provider_b ({current_model!r})")
                fallback_reason = pre_flight_note
            else:
                pb_err = (
                    provider_b_artifact_error
                    or (
                        pb_health.error
                        if pb_health is not None
                        else "circuit open or already attempted this job"
                    )
                )
                if pb_health is not None:
                    local_model_health.record_provider_outcome("provider_b", success=False)
                job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="local_model_unavailable", error_class="infra")
                result = JobResult(
                    job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=model,
                    risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                    status="local_model_unavailable", workdir=str(workdir),
                    started_at=started_at, ended_at=datetime.now(timezone.utc).isoformat(), duration_s=time.monotonic() - t0,
                    files_changed={"added": [], "modified": [], "removed": []},
                    plan_text=None, agent_final_action=None,
                    agent_summary_or_reason=f"Ollama health check failed ({health.error}); provider_b also unavailable ({pb_err})",
                    retried=retried, policy_reasons=decision.reasons,
                    attempted_models=attempted_models,
                    model_failure_reasons=model_failure_reasons,
                    fallback_reason=fallback_reason,
                    provider="none", attempted_providers=(attempted_providers + ["ollama", "provider_b"]),
                    failure_classification=local_model_health.classify_failure(final_action="model_unavailable", provider_health=health),
                )
                _finalize(result, requested_by=requested_by, final_disposition="local_model_unavailable")
                return result

        if current_model not in attempted_models:
            attempted_models.append(current_model)
        if current_provider not in attempted_providers:
            attempted_providers.append(current_provider)

        # Copy any explicitly-authorized source_paths into the sandbox for
        # the agent to work against — only on a fresh run or SAFE_RESUME;
        # RESTART_FROM_SANDBOX must never overwrite whatever the model
        # already partially edited. Identical mechanism/placement to
        # bridge.py's own copy_source_paths step: this happens BEFORE the
        # before-snapshot, so the copied files are never reported as
        # "added" by the agent — only what it actually changes shows up in
        # files_changed.
        if copy_source_paths:
            import shutil as _shutil
            for src in source_paths:
                dest = _source_sandbox_destination(workdir, src)
                if src.is_dir():
                    _shutil.copytree(src, dest, dirs_exist_ok=True)
                elif src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(src, dest)

        job_ledger.checkpoint(job_id, JobState.SANDBOX_READY, model=current_model,
                               attempted_models=attempted_models, attempted_providers=attempted_providers,
                               note=pre_flight_note)

        before = _snapshot(workdir)
        job_ledger.checkpoint(job_id, JobState.PLANNING)
        plan_text = _generate_plan(task, model=(model if current_provider == "ollama" else current_model), timeout_s=PLAN_TIMEOUT_S)

        def on_checkpoint(info: dict[str, Any]) -> None:
            state = _TOOL_TO_STATE.get(info["tool"])
            if state is None:
                return
            job_ledger.checkpoint(
                job_id, state, note=f"iteration {info['iteration']}: {info['tool']}",
                iteration=info["iteration"], model_calls=info["model_calls"],
                files_touched={"added": sorted(info["files_touched"]), "modified": [], "removed": []},
                commands_executed=info["commands_executed"],
            )

        run = run_agent_loop(task, workdir, model=current_model, provider=current_provider, max_iterations=max_iterations,
                              plan_text=plan_text, timeout_s=timeout_s, on_checkpoint=on_checkpoint,
                              allowed_tools=allowed_tools)

        # SINGLE_MODEL_DEPENDENCY resilience: on a retryable failure, prefer
        # a DIFFERENT suitable installed model over blindly repeating the
        # same one — but only if this is a genuine MODEL_FAILURE, never a
        # PROVIDER_FAILURE (an Ollama outage would fail every model
        # identically; cycling through them would waste the job's whole
        # time budget for zero benefit and must fall through to the
        # existing infra_failure -> Claude path immediately instead).
        # Bounded by local_model_health.engineering_failover_order(), which
        # can never return more than MAX_MODEL_FAILOVER_ATTEMPTS entries and
        # never a VISION_SPECIALIST model — this loop terminates once that
        # list (minus already-attempted models) is exhausted, once the time
        # budget runs out, or once the provider itself is confirmed down.
        # Only meaningful while still on the "ollama" provider — if
        # pre-flight already routed straight to provider_b, there is no
        # same-provider chain left to walk.
        while current_provider == "ollama" and run.final_action in RETRYABLE_FINAL_ACTIONS:
            model_failure_reasons[current_model] = run.summary_or_reason or run.final_action
            health_now = local_model_health.check()
            if not health_now.available:
                local_model_health.record_provider_outcome("ollama", success=False)
                fallback_reason = (f"provider_failure: Ollama itself is unreachable right now "
                                    f"({health_now.error}) — model failover would not help, stopping here")
                job_ledger.checkpoint(job_id, JobState.EDITING, attempted_models=attempted_models,
                                       model_failure_reasons=model_failure_reasons, note=fallback_reason)
                break

            remaining = max(30, timeout_s - int(time.monotonic() - t0))
            if remaining <= 30:
                fallback_reason = "time budget exhausted — no remaining window for another model attempt"
                break

            candidates = local_model_health.engineering_failover_order(exclude=attempted_models)
            candidates, not_installed = _filter_actually_installed(candidates, health_now.models)
            if not_installed:
                for c in not_installed:
                    model_failure_reasons.setdefault(
                        c, f"not_actually_installed: {c!r} is configured in engineering_failover_order "
                           f"but has no matching entry in the real Ollama model list ({health_now.models}) -- skipped without spending a model-call attempt",
                    )
            if not candidates:
                fallback_reason = f"model failover exhausted — every suitable installed model was tried or confirmed not installed: {attempted_models + not_installed}"
                break

            next_model = candidates[0]
            fallback_reason = (f"model_failure on {current_model!r} ({model_failure_reasons[current_model]}) "
                                f"— failing over to {next_model!r}")
            attempted_models.append(next_model)
            current_model = next_model
            retried = True
            job_ledger.checkpoint(job_id, JobState.EDITING, model=current_model, attempted_models=attempted_models,
                                   model_failure_reasons=model_failure_reasons, note=fallback_reason)
            run = run_agent_loop(task, workdir, model=current_model, provider="ollama", max_iterations=max_iterations,
                                  plan_text=plan_text, timeout_s=remaining, on_checkpoint=on_checkpoint,
                                  allowed_tools=allowed_tools)

        if run.final_action in RETRYABLE_FINAL_ACTIONS:
            model_failure_reasons[current_model] = run.summary_or_reason or run.final_action
        if current_provider == "ollama":
            local_model_health.record_provider_outcome("ollama", success=(run.final_action not in RETRYABLE_FINAL_ACTIONS))

        # CROSS_PROVIDER_FAILOVER (Founder-authorized 2026-08-18): same-
        # provider (Ollama) model failover above is now exhausted — either
        # every suitable installed model was tried, Ollama's own provider
        # health failed mid-job, or both — and we haven't already routed to
        # provider_b at pre-flight. Before falling through to the existing
        # Claude/infra_failure path, try the genuinely independent
        # provider_b (provider_b_bridge.py — a standalone llama-server
        # process that does NOT depend on the ollama daemon), exactly once,
        # SAME job_id/sandbox/ledger record. Skipped outright if its
        # circuit breaker is open (a provider already known to be down
        # right now must not be hammered every job) or if there's no
        # remaining time budget. A provider that was never even reachable
        # this attempt records a circuit-breaker failure and falls through
        # unchanged to the pre-existing Claude fallback below — no new
        # terminal-status handling needed there.
        if (current_provider == "ollama"
                and run.final_action in RETRYABLE_FINAL_ACTIONS
                and "provider_b" not in attempted_providers
                and not local_model_health.circuit_is_open("provider_b")):
            remaining = max(30, timeout_s - int(time.monotonic() - t0))
            if remaining > 30:
                import provider_b_bridge
                try:
                    pb_health = provider_b_bridge.ensure_running()
                except provider_b_bridge.ModelArtifactMissing as exc:
                    # Provider B is an optional cross-provider fallback.
                    # A missing local model artifact means this backend is
                    # unavailable; it must not terminate the parent Omni
                    # Engineer job. Record the evidence and continue through
                    # the pre-existing governed fallback path below.
                    pb_health = None
                    model_failure_reasons[provider_b_bridge.DEFAULT_MODEL] = (
                        f"provider_b_unavailable: {exc}"
                    )
                if pb_health is not None and pb_health.available:
                    pb_model = provider_b_bridge.DEFAULT_MODEL
                    fallback_reason = (
                        f"cross_provider_failover: same-provider (ollama) failover exhausted/unavailable "
                        f"({model_failure_reasons.get(current_model, run.final_action)}) — trying independent "
                        f"provider_b ({pb_model!r}, standalone llama-server, not dependent on the ollama daemon)"
                    )
                    attempted_providers.append("provider_b")
                    current_provider = "provider_b"
                    current_model = pb_model
                    if pb_model not in attempted_models:
                        attempted_models.append(pb_model)
                    retried = True
                    job_ledger.checkpoint(job_id, JobState.EDITING, model=current_model,
                                           attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                           note=fallback_reason)
                    run = run_agent_loop(task, workdir, model=pb_model, provider="provider_b",
                                          max_iterations=max_iterations, plan_text=plan_text,
                                          timeout_s=remaining, on_checkpoint=on_checkpoint,
                                          allowed_tools=allowed_tools)
                    local_model_health.record_provider_outcome(
                        "provider_b", success=(run.final_action not in RETRYABLE_FINAL_ACTIONS))
                    if run.final_action in RETRYABLE_FINAL_ACTIONS:
                        model_failure_reasons[pb_model] = run.summary_or_reason or run.final_action
                else:
                    local_model_health.record_provider_outcome("provider_b", success=False)
                    fallback_reason = (f"{fallback_reason}; provider_b also unavailable ({getattr(pb_health, 'error', None) or model_failure_reasons.get(provider_b_bridge.DEFAULT_MODEL) or 'provider_b unavailable'}) — "
                                        f"falling through to Claude specialist fallback")

        failure_classification: str | None = None
        if run.final_action in RETRYABLE_FINAL_ACTIONS:
            failure_classification = local_model_health.classify_failure(
                final_action=run.final_action, provider_health=None)

        provider = current_provider  # JobResult.provider reflects the FINAL provider actually used
        model = current_model  # JobResult.model reflects the FINAL model actually used

        after = _snapshot(workdir)
        files_changed = _diff_snapshots(before, after)
        ended_at = datetime.now(timezone.utc).isoformat()
        agent_ran_cleanly = run.final_action == "finish"

        validation_result: dict[str, Any] | None = None
        canary_result: dict[str, Any] | None = None
        independent_validation_result: dict[str, Any] | None = None
        promotion_eligible = False
        if agent_ran_cleanly:
            # OMNI_NOOP_PROMOTION_TRUTH_V1
            # A read-only/no-op job may legitimately succeed, but an empty
            # change-set can never be a promotion candidate.
            has_changes = any(
                files_changed.get(kind)
                for kind in ("added", "modified", "removed")
            )

            job_ledger.checkpoint(job_id, JobState.VALIDATING, iteration=len(run.turns))
            vres = validation.validate(workdir, files_changed, config=validation_config)
            validation_result = vres.to_json()
            if vres.passed:
                job_ledger.checkpoint(job_id, JobState.CANARY, validation_result=validation_result)
                cres = validation.validate(workdir, files_changed, config=validation_config)
                canary_result = cres.to_json()
                promotion_eligible = bool(cres.passed and has_changes)

                # SINGLE_VALIDATOR_DEPENDENCY resilience (Founder-authorized
                # 2026-08-18): validation.py + canary are the SAME code path
                # run twice — a bug/blind spot in that one implementation
                # would pass both times identically. This is a genuinely
                # separate re-check (different subprocess mechanism, not a
                # second opinion from the same code) of the sandbox's most
                # objectively-checkable facts. A disagreement is NEVER
                # silently overridden — it blocks promotion_eligible even
                # though validation.py+canary both said PASS, and is
                # recorded loudly for Founder visibility and experience
                # learning (see evolution/advance.py).
                if promotion_eligible:
                    ivres = independent_validation.recheck(workdir, files_changed, primary_passed=True)
                    independent_validation_result = ivres.to_json()
                    if ivres.ran and ivres.agrees_with_primary is False:
                        promotion_eligible = False

        if not agent_ran_cleanly:
            status = "escalated" if run.final_action == "escalate" else run.final_action
            ledger_state = JobState.ESCALATED if run.final_action == "escalate" else JobState.FAILED
            job_ledger.checkpoint(job_id, ledger_state, terminal_result=status, model=model,
                                   attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                   attempted_providers=attempted_providers,
                                   error_class="model_escalate" if run.final_action == "escalate" else "infra")
        elif not validation_result or not validation_result.get("passed"):
            status = "succeeded_validation_failed"
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result=status, model=model,
                                   attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                   attempted_providers=attempted_providers,
                                   error_class="validation", validation_result=validation_result)
        elif not canary_result or not canary_result.get("passed"):
            status = "succeeded_canary_failed"
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result=status, model=model,
                                   attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                   attempted_providers=attempted_providers,
                                   error_class="validation", validation_result=validation_result, canary_result=canary_result)
        elif not has_changes:
            # Validation/canary passed, but there is literally nothing to
            # promote. This is a legitimate succeeded read-only/no-op result,
            # NOT a validator disagreement and NOT a promotion candidate.
            status = "succeeded"
            job_ledger.checkpoint(
                job_id,
                JobState.COMPLETED,
                terminal_result="succeeded",
                model=model,
                attempted_models=attempted_models,
                model_failure_reasons=model_failure_reasons,
                attempted_providers=attempted_providers,
                promotion_eligible=False,
                files_touched=files_changed,
                validation_result=validation_result,
                canary_result=canary_result,
                independent_validation_result=independent_validation_result,
            )
        elif not promotion_eligible:
            # validation.py + canary BOTH passed, but the genuinely
            # independent recheck disagreed — never silently promoted.
            status = "succeeded_validator_disagreement"
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result=status, model=model,
                                   attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                   attempted_providers=attempted_providers,
                                   error_class="validator_disagreement", validation_result=validation_result,
                                   canary_result=canary_result, independent_validation_result=independent_validation_result)
        else:
            status = "succeeded"
            job_ledger.checkpoint(job_id, JobState.PROMOTION_CANDIDATE,
                                   validation_result=validation_result, canary_result=canary_result,
                                   independent_validation_result=independent_validation_result,
                                   promotion_eligible=True, files_touched=files_changed, model=model,
                                   attempted_models=attempted_models, model_failure_reasons=model_failure_reasons,
                                   attempted_providers=attempted_providers)
            job_ledger.checkpoint(job_id, JobState.COMPLETED, terminal_result="succeeded")

        if validation_result:
            (JOBS_ROOT / job_id / "validation.json").write_text(json.dumps(validation_result, indent=2))

        result = JobResult(
            job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=model,
            risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
            status=status, workdir=str(workdir),
            started_at=started_at, ended_at=ended_at, duration_s=time.monotonic() - t0,
            files_changed=files_changed,
            plan_text=plan_text,
            agent_final_action=run.final_action, agent_summary_or_reason=run.summary_or_reason, retried=retried,
            turns=run.turns, commands_executed=run.commands_executed,
            policy_reasons=decision.reasons,
            validation=validation_result, canary=canary_result, promotion_eligible=promotion_eligible,
            attempted_models=attempted_models, model_failure_reasons=model_failure_reasons, fallback_reason=fallback_reason,
            provider=provider, attempted_providers=attempted_providers, failure_classification=failure_classification,
            independent_validation=independent_validation_result,
        )
        _finalize(result, requested_by=requested_by, final_disposition=status)
        return result
    finally:
        if not is_resume:
            job_ledger.release(job_id, owner=requested_by)


def _finalize(result: JobResult, *, requested_by: str, final_disposition: str | None = None) -> None:
    # H5-P0: worker identity is distinct from provider/model.
    # Current Ollama/provider_b execution is the local-model worker.
    # Explicit future worker identities (Claude/Codex) are preserved.
    if (
        result.worker_backend is None
        and result.status != "rejected_policy"
        and result.provider in {"ollama", "provider_b"}
    ):
        result.worker_backend = "local_model"

    result_path = JOBS_ROOT / result.job_id / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(asdict(result), indent=2))

    job_ledger.record_execution_metadata(
        result.job_id,
        provider=result.provider,
        fallback_reason=result.fallback_reason,
        worker_backend=result.worker_backend,
    )

    audit.record(
        job_id=result.job_id, requested_by=requested_by,
        task_summary=result.task[:200],
        tool_agent_selected=OMNI_ENGINEER_ID,
        permissions_granted=[] if result.status == "rejected_policy" else [
            "sandboxed-tool-loop(list_files/read_file/grep/inspect_diff/"
            "write_file_sandbox/apply_patch_sandbox/run_validator/run_command)"
        ],
        files_touched=result.files_changed,
        commands_executed=result.commands_executed,
        test_results=result.validation,
        risk_class=result.risk_class,
        approval_state=result.approval_state,
        final_disposition=final_disposition or result.status,
        lesson=result.agent_summary_or_reason if result.agent_final_action == "escalate" else None,
    )


# ============================================================
# H5 — Operational Shared Engineering Memory
#
# The harness owns the imported run_agent_loop binding.
# This preserves omniengineer_agent.py unchanged.
# ============================================================

import functools as _h5mem_functools
import inspect as _h5mem_inspect
import engineering_memory_runtime as _h5mem_runtime


# ------------------------------------------------------------
# Pre-planning retrieval
# ------------------------------------------------------------

_h5mem_original_generate_plan = _generate_plan
_h5mem_generate_plan_signature = (
    _h5mem_inspect.signature(
        _h5mem_original_generate_plan
    )
)


@_h5mem_functools.wraps(
    _h5mem_original_generate_plan
)
def _h5mem_generate_plan_wrapper(
    *args,
    **kwargs,
):
    return (
        _h5mem_runtime
        .invoke_with_memory_context(
            _h5mem_original_generate_plan,
            args,
            kwargs,
        )
    )


_h5mem_generate_plan_wrapper.__signature__ = (
    _h5mem_generate_plan_signature
)

_h5mem_generate_plan_wrapper._h5_operational_memory_wrapped = True

_generate_plan = (
    _h5mem_generate_plan_wrapper
)


# ------------------------------------------------------------
# Pre-investigation retrieval
# ------------------------------------------------------------

_h5mem_original_run_agent_loop = (
    run_agent_loop
)

_h5mem_run_agent_loop_signature = (
    _h5mem_inspect.signature(
        _h5mem_original_run_agent_loop
    )
)


@_h5mem_functools.wraps(
    _h5mem_original_run_agent_loop
)
def _h5mem_run_agent_loop_wrapper(
    *args,
    **kwargs,
):
    return (
        _h5mem_runtime
        .invoke_with_memory_context(
            _h5mem_original_run_agent_loop,
            args,
            kwargs,
        )
    )


_h5mem_run_agent_loop_wrapper.__signature__ = (
    _h5mem_run_agent_loop_signature
)

_h5mem_run_agent_loop_wrapper._h5_operational_memory_wrapped = True

run_agent_loop = (
    _h5mem_run_agent_loop_wrapper
)


# ------------------------------------------------------------
# Whole-job memory query scope
# ------------------------------------------------------------

_h5mem_original_submit_job = (
    submit_job
)

_h5mem_submit_signature = (
    _h5mem_inspect.signature(
        _h5mem_original_submit_job
    )
)


@_h5mem_functools.wraps(
    _h5mem_original_submit_job
)
def _h5mem_submit_job_wrapper(
    *args,
    **kwargs,
):
    bound = (
        _h5mem_submit_signature
        .bind_partial(
            *args,
            **kwargs,
        )
    )

    task = bound.arguments.get(
        "task"
    )

    with (
        _h5mem_runtime
        .job_memory_scope(
            task
        )
    ):
        return (
            _h5mem_original_submit_job(
                *args,
                **kwargs,
            )
        )


_h5mem_submit_job_wrapper.__signature__ = (
    _h5mem_submit_signature
)

_h5mem_submit_job_wrapper._h5_operational_memory_wrapped = True

submit_job = (
    _h5mem_submit_job_wrapper
)


# ------------------------------------------------------------
# Post-terminal writeback
# ------------------------------------------------------------

_h5mem_original_finalize = (
    _finalize
)

_h5mem_finalize_signature = (
    _h5mem_inspect.signature(
        _h5mem_original_finalize
    )
)


@_h5mem_functools.wraps(
    _h5mem_original_finalize
)
def _h5mem_finalize_wrapper(
    *args,
    **kwargs,
):
    bound = (
        _h5mem_finalize_signature
        .bind_partial(
            *args,
            **kwargs,
        )
    )

    result = bound.arguments.get(
        "result"
    )

    output = (
        _h5mem_original_finalize(
            *args,
            **kwargs,
        )
    )

    if result is not None:
        (
            _h5mem_runtime
            .writeback_omni_result(
                result
            )
        )

    return output


_h5mem_finalize_wrapper.__signature__ = (
    _h5mem_finalize_signature
)

_h5mem_finalize_wrapper._h5_operational_memory_wrapped = True

_finalize = (
    _h5mem_finalize_wrapper
)



# TASK30B2_COMPUTE_PREFLIGHT_HELPER
def _task30b2_compute_preflight(
    *,
    current_provider,
    current_model,
    validation_config,
):
    import compute_capability as _task30_compute

    cfg = {}

    if isinstance(
        validation_config,
        dict,
    ):
        raw = validation_config.get(
            "compute_capability",
            {},
        )

        if isinstance(raw, dict):
            cfg = dict(raw)

    fallback_available = bool(
        cfg.get(
            "fallback_available",
            False,
        )
    )

    snapshot = (
        _task30_compute
        .detect_compute_capability(
            fallback_provider_available=(
                fallback_available
            ),
        )
    )

    decision = (
        _task30_compute
        .select_compute_route(
            snapshot,
            requires_gpu=bool(
                cfg.get(
                    "requires_gpu",
                    False,
                )
            ),
            fallback_authorized=bool(
                cfg.get(
                    "fallback_authorized",
                    False,
                )
            ),
            cpu_degraded_allowed=bool(
                cfg.get(
                    "cpu_degraded_allowed",
                    True,
                )
            ),
        )
    )

    # Default CPU-degraded path:
    # preserve the existing Ollama/local route.
    # Ollama may execute on CPU when GPU is absent.
    if (
        decision.selected_route
        == "ollama"
    ):
        return (
            decision,
            "ollama",
            current_model,
        )

    # Provider B is selected only when the
    # caller explicitly marked the fallback
    # both available and authorized.
    if (
        decision.selected_route
        == "provider_b"
    ):
        return (
            decision,
            "provider_b",
            str(
                cfg.get(
                    "fallback_model",
                    "gpt-oss:20b",
                )
            ),
        )

    # Defer is deliberately non-destructive.
    # Existing execution/failure taxonomy is
    # preserved until Task30B3 binds the
    # decision to the durable defer/escalation
    # lifecycle after certification.
    return (
        decision,
        current_provider,
        current_model,
    )


# ============================================================
# OMNI_GOD_MODE_V1 PHASE 2 — bounded task decomposition
#
# Real regression target: job 6978adf2, where qwen3-coder:30b used all 18
# iterations of ONE undecomposed ReAct loop on a large, multi-file task
# without ever calling finish, run_validator, or run_command.
#
# Design constraints this satisfies: ONE canonical parent job_id/sandbox/
# ledger record throughout (no second job-tracking system); a FIXED, small,
# bounded phase sequence (no dynamic/unbounded decomposition); each phase is
# just another run_agent_loop() call in the SAME sandbox with a smaller
# max_iterations and a phase-scoped allowed_tools set (reusing the
# EXISTING, structurally-enforced allowed_tools gate -- not a new
# capability); durable phase state lives on the SAME LedgerRecord
# (LedgerRecord.phases); final success is decided by the EXISTING
# validation.py + canary + independent_validation pipeline, unconditionally
# -- a phase merely calling finish is never sufficient by itself. Defined
# after the H5 engineering-memory wrapping above, so run_agent_loop/
# _generate_plan/_finalize calls below automatically get memory-context
# behavior identically to a normal submit_job() run -- nothing new to wire.
# ============================================================

DECOMPOSED_MAX_ITERATIONS_PER_PHASE = 6
DECOMPOSED_MAX_TOTAL_PHASES = 8  # hard ceiling on phases actually run, including repair cycles -- "no infinite decomposition"
DECOMPOSED_MAX_REPAIR_CYCLES = 2

# finish/escalate are always implicitly allowed by run_agent_loop regardless
# of allowed_tools, so they're intentionally omitted here.
_PHASE_TOOLS: dict[str, frozenset[str]] = {
    "inspect": frozenset({"list_files", "read_file", "grep"}),
    "implement": frozenset({"list_files", "read_file", "grep", "inspect_diff", "write_file_sandbox", "apply_patch_sandbox"}),
    "test": frozenset({"list_files", "read_file", "grep", "inspect_diff", "write_file_sandbox", "apply_patch_sandbox", "run_command"}),
    "repair": frozenset({"list_files", "read_file", "grep", "inspect_diff", "write_file_sandbox", "apply_patch_sandbox", "run_command"}),
}


def _phase_task_text(parent_task: str, phase_name: str, phase_objective: str, prior_summary: str, allowed_tools: frozenset[str] | None) -> str:
    parts = [
        "You are working on ONE BOUNDED PHASE of a larger, already-authorized engineering "
        "objective. Complete ONLY this phase's objective, then call finish with a short summary "
        "of exactly what you did/found. Do not attempt work that belongs to a later phase.",
        f"\nOVERALL OBJECTIVE (context only -- do not exceed this phase's scope):\n{parent_task}",
        f"\nCURRENT PHASE: {phase_name.upper()}",
        f"\nPHASE OBJECTIVE:\n{phase_objective}",
    ]
    # Real incident this addresses: an inspect-phase run tried something
    # needing write access, got a correct structural refusal (inspect is
    # deliberately read-only), and escalated with "write permissions are
    # not available" instead of understanding this is expected phase
    # scoping -- not a malfunction, and not something to ask a human about.
    if allowed_tools is not None:
        parts.append(
            f"\nTOOL SCOPE FOR THIS PHASE: only {', '.join(sorted(allowed_tools))} (plus finish/escalate) "
            f"are available. This is DELIBERATE, EXPECTED, and by design for this phase -- not a malfunction. "
            f"If a tool you'd want isn't listed, that work belongs to a LATER phase of this same job, which "
            f"will run automatically after this one. Do not escalate just because a tool is unavailable this "
            f"phase; simply do what this phase's objective actually asks for with the tools you do have, then "
            f"call finish. Only escalate if the objective genuinely cannot be satisfied even within this phase's "
            f"real scope and tools."
        )
    if prior_summary:
        parts.append(f"\nVERIFIED PROGRESS SO FAR (from already-completed phases):\n{prior_summary}")
    parts.append("\nWhen this phase's objective is satisfied, call finish now.")
    return "\n".join(parts)


def _run_phase(
    phase_name: str, phase_objective: str, workdir: Path, *, parent_task: str,
    prior_summary: str, model: str, max_iterations: int, timeout_s: int,
) -> tuple[Any, str, list[str]]:
    """Runs ONE bounded phase in the SAME sandbox the parent job already
    owns. Bounded model failover on a retryable outcome (iteration_ceiling_
    reached/timeout/model_unavailable/error): try the next REAL installed
    model (_filter_actually_installed(), same real-time check as the
    whole-job path), never a phantom configured-only one. finish/escalate
    are always terminal for the phase -- never retried with a different
    model. Returns (AgentRunResult, model_actually_used, attempted_models)."""
    allowed = _PHASE_TOOLS.get(phase_name)
    task_text = _phase_task_text(parent_task, phase_name, phase_objective, prior_summary, allowed)
    attempted: list[str] = []
    current_model = model
    t0 = time.monotonic()
    while True:
        attempted.append(current_model)
        remaining = max(30, timeout_s - int(time.monotonic() - t0))
        run = run_agent_loop(
            task_text, workdir, model=current_model, provider="ollama",
            max_iterations=max_iterations, timeout_s=remaining, allowed_tools=allowed,
        )
        if run.final_action in ("finish", "escalate"):
            return run, current_model, attempted
        health = local_model_health.check(model=current_model)
        candidates = local_model_health.engineering_failover_order(exclude=attempted)
        candidates, _not_installed = _filter_actually_installed(candidates, health.models if health.available else [])
        if not candidates:
            return run, current_model, attempted
        current_model = candidates[0]


def submit_job_decomposed(
    task: str, *, requested_by: str = "unspecified", timeout_s: int = DEFAULT_TIMEOUT_S,
    founder_approved: bool = False, model: str = DEFAULT_MODEL,
    max_iterations_per_phase: int = DECOMPOSED_MAX_ITERATIONS_PER_PHASE,
    validation_config: dict[str, Any] | None = None,
    on_job_created: Callable[[str], None] | None = None,
) -> JobResult:
    """Bounded, phase-decomposed alternative to submit_job() for large/
    complex objectives. One canonical job_id/sandbox/ledger throughout.
    Fixed phase sequence: INSPECT -> IMPLEMENT -> TEST -> (deterministic
    VALIDATE, reusing validation.py exactly as submit_job()'s single-loop
    path does) -> up to DECOMPOSED_MAX_REPAIR_CYCLES bounded REPAIR phases
    on a validation failure, re-validating after each. Does NOT support
    source_paths in this version -- a disclosed, real scope limit, not a
    silent gap. Never marks a job successful on a phase's own 'finish' call
    alone; final success is always the deterministic validation+canary+
    independent_validation pipeline, identically to submit_job()."""
    fp = job_ledger.task_fingerprint(task)
    existing = job_ledger.find_active_by_fingerprint(fp)
    if existing is not None:
        return _duplicate_result(existing, task, model, requested_by)

    job_id = str(uuid.uuid4())
    if on_job_created:
        try:
            on_job_created(job_id)
        except Exception:  # noqa: BLE001 — a caller's linking hook must never break job execution
            pass
    workdir = JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    timeout_s = min(timeout_s, MAX_TIMEOUT_S)
    job_ledger.create(
        job_id, task=task, requested_by=requested_by, sandbox_path=str(workdir),
        model=model, max_iterations=max_iterations_per_phase,
        submit_params={"decomposed": True, "validation_config": validation_config},
    )
    job_ledger.claim(job_id, owner=requested_by)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    try:
        decision = classify(
            task_description=task, requested_tools=set(), sandbox_root=workdir,
            source_paths=[], founder_approved=founder_approved, adapter=OMNI_ENGINEER_ID,
        )
        job_ledger.checkpoint(job_id, JobState.AUTHORIZED, risk_class=decision.risk_class.value,
                               approval_state=decision.approval_state.value,
                               authority_state="granted" if decision.may_execute else "denied")
        if not decision.may_execute:
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="rejected_policy", error_class="authority")
            result = JobResult(
                job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=model,
                risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                status="rejected_policy", workdir=str(workdir), started_at=started_at,
                ended_at=started_at, duration_s=0.0,
                files_changed={"added": [], "modified": [], "removed": []},
                plan_text=None, agent_final_action=None, agent_summary_or_reason=None,
                retried=False, policy_reasons=decision.reasons,
            )
            _finalize(result, requested_by=requested_by)
            return result

        job_ledger.checkpoint(job_id, JobState.ROUTED, selected_engine=OMNI_ENGINEER_ID)

        before_all = _snapshot(workdir)
        phases_state: list[dict[str, Any]] = []
        all_commands: list[str] = []
        attempted_models_all: list[str] = []
        prior_summary = ""

        def _record_phase(name: str, objective: str, run, model_used: str, attempted: list[str]) -> dict[str, Any]:
            entry = {
                "name": name, "objective": objective[:500],
                "final_action": run.final_action, "summary": (run.summary_or_reason or "")[:800],
                "model": model_used, "attempted_models": attempted,
                "commands_executed": list(run.commands_executed), "ended_at": datetime.now(timezone.utc).isoformat(),
            }
            phases_state.append(entry)
            all_commands.extend(run.commands_executed)
            for m in attempted:
                if m not in attempted_models_all:
                    attempted_models_all.append(m)
            job_ledger.checkpoint(job_id, JobState.EDITING, phases=list(phases_state),
                                   note=f"phase {name!r} finished: {run.final_action}")
            return entry

        escalated = False
        for phase_name, phase_objective in (
            ("inspect", "Inspect the existing sandbox and any relevant files. Understand exactly what "
                        "needs to change to satisfy the overall objective. Call finish with a concise "
                        "written summary of what you found and what you plan to change."),
            ("implement", "Make the bounded code changes needed to satisfy the overall objective, based "
                           "on your inspection. Call finish with a summary of the files you added/changed."),
            ("test", "Write focused tests for the change you just implemented, then run them with "
                     "run_command. Call finish with a summary of the tests and whether they passed."),
        ):
            if len(phases_state) >= DECOMPOSED_MAX_TOTAL_PHASES:
                break
            run, mdl, attempted = _run_phase(
                phase_name, phase_objective, workdir, parent_task=task, prior_summary=prior_summary,
                model=model, max_iterations=max_iterations_per_phase,
                timeout_s=max(30, timeout_s - int(time.monotonic() - t0)),
            )
            entry = _record_phase(phase_name, phase_objective, run, mdl, attempted)
            if run.final_action == "escalate":
                escalated = True
                break
            prior_summary = (prior_summary + f"\n[{phase_name}] {entry['summary']}").strip()

        if escalated:
            escalated_files_changed = _diff_snapshots(before_all, _snapshot(workdir))
            job_ledger.checkpoint(job_id, JobState.ESCALATED, terminal_result="escalated", phases=phases_state,
                                   attempted_models=attempted_models_all, error_class="model_escalate",
                                   files_touched=escalated_files_changed)
            result = JobResult(
                job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=attempted_models_all[-1] if attempted_models_all else model,
                risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                status="escalated", workdir=str(workdir), started_at=started_at,
                ended_at=datetime.now(timezone.utc).isoformat(), duration_s=time.monotonic() - t0,
                files_changed=escalated_files_changed, plan_text=None,
                agent_final_action="escalate", agent_summary_or_reason=phases_state[-1]["summary"] if phases_state else None,
                retried=len(attempted_models_all) > 1, commands_executed=all_commands,
                attempted_models=attempted_models_all,
            )
            _finalize(result, requested_by=requested_by)
            return result

        # Deterministic VALIDATE, identical pipeline to submit_job()'s single-loop path.
        after = _snapshot(workdir)
        files_changed = _diff_snapshots(before_all, after)
        job_ledger.checkpoint(job_id, JobState.VALIDATING, phases=phases_state)
        vres = validation.validate(workdir, files_changed, config=validation_config)

        repair_cycles = 0
        while not vres.passed and repair_cycles < DECOMPOSED_MAX_REPAIR_CYCLES and len(phases_state) < DECOMPOSED_MAX_TOTAL_PHASES:
            repair_cycles += 1
            repair_objective = (
                f"Validation FAILED (repair attempt {repair_cycles}/{DECOMPOSED_MAX_REPAIR_CYCLES}). "
                f"Fix the specific issue(s) below, then call finish.\n{json.dumps(vres.to_json())[:1500]}"
            )
            run, mdl, attempted = _run_phase(
                "repair", repair_objective, workdir, parent_task=task, prior_summary=prior_summary,
                model=model, max_iterations=max_iterations_per_phase,
                timeout_s=max(30, timeout_s - int(time.monotonic() - t0)),
            )
            entry = _record_phase(f"repair_{repair_cycles}", repair_objective, run, mdl, attempted)
            if run.final_action == "escalate":
                job_ledger.checkpoint(job_id, JobState.ESCALATED, terminal_result="escalated", phases=phases_state,
                                       attempted_models=attempted_models_all, error_class="model_escalate",
                                       files_touched=files_changed, validation_result=vres.to_json())
                result = JobResult(
                    job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=mdl,
                    risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                    status="escalated", workdir=str(workdir), started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(), duration_s=time.monotonic() - t0,
                    files_changed=files_changed, plan_text=None, agent_final_action="escalate",
                    agent_summary_or_reason=entry["summary"], retried=len(attempted_models_all) > 1,
                    commands_executed=all_commands, attempted_models=attempted_models_all,
                    validation=vres.to_json(),
                )
                _finalize(result, requested_by=requested_by)
                return result
            prior_summary = (prior_summary + f"\n[repair_{repair_cycles}] {entry['summary']}").strip()
            after = _snapshot(workdir)
            files_changed = _diff_snapshots(before_all, after)
            vres = validation.validate(workdir, files_changed, config=validation_config)

        canary_result = None
        independent_validation_result = None
        promotion_eligible = False
        has_changes = any(files_changed.get(k) for k in ("added", "modified", "removed"))
        if vres.passed:
            job_ledger.checkpoint(job_id, JobState.CANARY, validation_result=vres.to_json(), phases=phases_state)
            cres = validation.validate(workdir, files_changed, config=validation_config)
            canary_result = cres.to_json()
            promotion_eligible = bool(cres.passed and has_changes)
            if promotion_eligible:
                ivres = independent_validation.recheck(workdir, files_changed, primary_passed=True)
                independent_validation_result = ivres.to_json()
                if ivres.ran and ivres.agrees_with_primary is False:
                    promotion_eligible = False

        ended_at = datetime.now(timezone.utc).isoformat()
        if not vres.passed:
            status = "succeeded_validation_failed"
        elif not canary_result or not canary_result.get("passed"):
            status = "succeeded_canary_failed"
        else:
            status = "succeeded"
        ledger_state = JobState.COMPLETED if status == "succeeded" else JobState.FAILED
        job_ledger.checkpoint(
            job_id, ledger_state, terminal_result=status, phases=phases_state,
            attempted_models=attempted_models_all, validation_result=vres.to_json(),
            files_touched=files_changed, promotion_eligible=promotion_eligible,
            error_class=None if status == "succeeded" else "validation",
        )
        result = JobResult(
            job_id=job_id, task=task, adapter=OMNI_ENGINEER_ID, model=attempted_models_all[-1] if attempted_models_all else model,
            risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
            status=status, workdir=str(workdir), started_at=started_at, ended_at=ended_at,
            duration_s=time.monotonic() - t0, files_changed=files_changed, plan_text=None,
            agent_final_action="finish", agent_summary_or_reason=phases_state[-1]["summary"] if phases_state else None,
            retried=len(attempted_models_all) > 1, commands_executed=all_commands,
            attempted_models=attempted_models_all, validation=vres.to_json(), canary=canary_result,
            promotion_eligible=promotion_eligible, independent_validation=independent_validation_result,
        )
        _finalize(result, requested_by=requested_by)
        return result
    finally:
        job_ledger.release(job_id, owner=requested_by)


def phases_from_ledger(job_id: str) -> list[dict[str, Any]]:
    """Read-only: the durable phase history of a decomposed job, straight
    from its ledger record -- used by resume/inspection, never a second
    source of truth."""
    record = job_ledger.load(job_id)
    return list(record.phases) if record and record.phases else []
