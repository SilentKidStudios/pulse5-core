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
import re
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

# ---- COMPLEXITY CLASSIFICATION (OMNI_GOD_MODE_V1 Phase 3) -----------------
# Deterministic, bounded, fail-conservative: a task is only decomposition-
# eligible when at least two INDEPENDENT structural signal categories fire.
# Length alone, or a single stray keyword, is never enough — a one-file edit
# must not be unnecessarily decomposed. This lives inside the Omni capability
# boundary (this module), not in task_router.py or evolution/advance.py, so
# every caller of submit_job_auto() gets the identical decision with zero
# duplicated logic and no new router.
_COMPLEXITY_STAGE_KEYWORDS = re.compile(
    r"\b(inspect|design|implement|test|repair|validate|refactor|migrate)\b", re.IGNORECASE)
_COMPLEXITY_FILE_MENTION = re.compile(
    r"\b[\w][\w\-./]*\.(?:py|js|ts|tsx|jsx|json|md|ya?ml|sh)\b")
_COMPLEXITY_PHASE_MARKER = re.compile(r"\bPHASE\s+\d+\b", re.IGNORECASE)

COMPLEXITY_LENGTH_THRESHOLD = 1200  # chars — a weak signal, only counts combined with a real structural one
COMPLEXITY_MIN_STAGE_KEYWORDS = 3
COMPLEXITY_MIN_FILE_MENTIONS = 2
COMPLEXITY_MIN_PHASE_MARKERS = 2
COMPLEXITY_MIN_SIGNAL_CATEGORIES = 2  # fail conservative: need >=2 independent categories, not just one


@dataclass
class ComplexityDecision:
    decomposition_eligible: bool
    reasons: list[str]
    signals: dict[str, Any]


def classify_complexity(task: str) -> ComplexityDecision:
    """Bounded structural classifier — no model call, no vague 'model
    preference', pure deterministic text analysis of the task description
    itself. See module-level comment above for the fail-conservative rule."""
    stage_hits = sorted({m.group(1).lower() for m in _COMPLEXITY_STAGE_KEYWORDS.finditer(task)})
    file_hits = sorted({m.group(0) for m in _COMPLEXITY_FILE_MENTION.finditer(task)})
    phase_markers = len(_COMPLEXITY_PHASE_MARKER.findall(task))
    length = len(task)

    signals: dict[str, Any] = {
        "length": length,
        "distinct_stage_keywords": stage_hits,
        "distinct_file_mentions": file_hits,
        "explicit_phase_markers": phase_markers,
    }

    categories = 0
    reasons: list[str] = []
    if len(stage_hits) >= COMPLEXITY_MIN_STAGE_KEYWORDS:
        categories += 1
        reasons.append(f"{len(stage_hits)} distinct stage keywords {stage_hits} (>= {COMPLEXITY_MIN_STAGE_KEYWORDS})")
    if len(file_hits) >= COMPLEXITY_MIN_FILE_MENTIONS:
        categories += 1
        reasons.append(f"{len(file_hits)} distinct file/module mentions (>= {COMPLEXITY_MIN_FILE_MENTIONS})")
    if phase_markers >= COMPLEXITY_MIN_PHASE_MARKERS:
        categories += 1
        reasons.append(f"{phase_markers} explicit multi-stage phase markers (>= {COMPLEXITY_MIN_PHASE_MARKERS})")
    if length >= COMPLEXITY_LENGTH_THRESHOLD and (stage_hits or file_hits):
        categories += 1
        reasons.append(f"task length {length} chars (>= {COMPLEXITY_LENGTH_THRESHOLD}) combined with a real structural signal")

    eligible = categories >= COMPLEXITY_MIN_SIGNAL_CATEGORIES
    if not eligible:
        reasons.append(f"only {categories}/{COMPLEXITY_MIN_SIGNAL_CATEGORIES} independent signal categor(y/ies) fired "
                        f"— classified simple by default (fail-conservative)")
    return ComplexityDecision(decomposition_eligible=eligible, reasons=reasons, signals=signals)


# ---- ADAPTIVE RETRY MATRIX (OMNI_GOD_MODE_V1 Phase 3, all 9 classes) ------
# A documented, testable ledger of every failure class's bounded strategy.
# The actual mechanisms live in _run_phase()/_execute()/submit_job_decomposed()
# below (RETRYABLE_FINAL_ACTIONS, model/provider failover, repair cycles,
# context narrowing, no-progress detection) — this dict is the single place
# that names all 9 explicitly so none can silently go undocumented. Kept in
# sync by test_omniengineer.py::test_adaptive_retry_matrix_covers_all_nine.
ADAPTIVE_RETRY_MATRIX: dict[str, dict[str, str]] = {
    "TOOL_SCHEMA_FAILURE": {
        "DETECTION": "omniengineer_agent's tool-call parser rejects a malformed/unknown tool call from the model",
        "RECOVERY_STRATEGY": "Phase 1 repair layer: the malformed call is fed back to the SAME model as a structured correction prompt on the next iteration, never silently dropped",
        "MAX_RETRIES": "bounded by the phase/job's own max_iterations ceiling — no separate unbounded retry loop",
        "FAIL_CLOSED_CONDITION": "iteration ceiling reached with the schema still malformed -> iteration_ceiling_reached (RETRYABLE_FINAL_ACTIONS), never silently treated as success",
        "ESCALATION_CONDITION": "iteration_ceiling_reached after schema-repair attempts triggers model failover exactly like any other retryable outcome",
    },
    "ITERATION_STALL": {
        "DETECTION": "a phase/job hits its max_iterations ceiling without calling finish or escalate",
        "RECOVERY_STRATEGY": "bounded per-phase max_iterations (default 6, vs the historical 18-iteration monolith) + failover to the next REAL installed model rather than re-prompting the same one",
        "MAX_RETRIES": "len(local_model_health.engineering_failover_order()) real installed candidates, minus already-attempted",
        "FAIL_CLOSED_CONDITION": "candidate list exhausted with no finish -> phase/job outcome recorded as its last attempt's final_action, never upgraded to success",
        "ESCALATION_CONDITION": "no candidates left and no time budget remaining -> falls through to provider/engine escalation",
    },
    "VALIDATION_FAILURE": {
        "DETECTION": "validation.validate() (deterministic, not model-judged) returns passed=False after the base inspect/implement/test phases",
        "RECOVERY_STRATEGY": "up to DECOMPOSED_MAX_REPAIR_CYCLES bounded REPAIR phases, each given the exact validation failure JSON as its objective, re-validating after each",
        "MAX_RETRIES": "DECOMPOSED_MAX_REPAIR_CYCLES (2)",
        "FAIL_CLOSED_CONDITION": "still failing after all repair cycles -> status=succeeded_validation_failed, promotion_eligible=False, never auto-promoted",
        "ESCALATION_CONDITION": "a repair phase itself calls escalate -> job terminates ESCALATED with the validation failure preserved in the ledger",
    },
    "TEST_FAILURE": {
        "DETECTION": "_classify_validation_failure() inspects vres.to_json()['checks'] for a failing check whose name signals it is a test-execution check (distinct from a static/lint check)",
        "RECOVERY_STRATEGY": "the repair objective explicitly names the failing test check(s) and their output, not just 'validation failed' — a targeted repair, not a blind retry",
        "MAX_RETRIES": "DECOMPOSED_MAX_REPAIR_CYCLES (2), shared budget with VALIDATION_FAILURE (same repair loop, distinguished only in objective wording/telemetry)",
        "FAIL_CLOSED_CONDITION": "same as VALIDATION_FAILURE — deterministic validate() is the only ground truth, a phase's own 'finish' claim never overrides it",
        "ESCALATION_CONDITION": "identical failing check name on two consecutive repair cycles -> flagged as NO_PROGRESS (see below), repair loop does not blindly repeat a third identical attempt",
    },
    "MODEL_FAILURE": {
        "DETECTION": "run.final_action in RETRYABLE_FINAL_ACTIONS for the CURRENT model specifically (not a whole-provider outage)",
        "RECOVERY_STRATEGY": "fail over to the next REAL installed model via _filter_actually_installed(), same-provider, never a phantom configured-only model",
        "MAX_RETRIES": "bounded by real installed-model count minus already-attempted (engineering_failover_order())",
        "FAIL_CLOSED_CONDITION": "no installed candidates remain -> falls through to PROVIDER_FAILURE handling",
        "ESCALATION_CONDITION": "same-provider chain exhausted -> provider failover attempted next",
    },
    "PROVIDER_FAILURE": {
        "DETECTION": "the current provider (ollama) itself is unreachable (local_model_health.check().available is False), or its model failover chain is exhausted mid-phase",
        "RECOVERY_STRATEGY": "select another actually-eligible provider under canonical policy — provider_b (standalone llama-server, independent of the ollama daemon) — exactly once per phase, skipped outright if its circuit breaker is open",
        "MAX_RETRIES": "1 provider switch per phase (bounded — never cycles providers back and forth)",
        "FAIL_CLOSED_CONDITION": "provider_b also unavailable/circuit-open -> phase returns its last real outcome, never fabricated success",
        "ESCALATION_CONDITION": "both providers exhausted -> phase/job outcome stands, whole-job level may still reach Claude/human escalation via evolution/advance.py's engine router",
    },
    "CONTEXT_PRESSURE": {
        "DETECTION": "accumulated prior_summary text passed into the next phase exceeds CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT",
        "RECOVERY_STRATEGY": "checkpoint the verified state already recorded in phases_state (unaffected — full history stays in the durable ledger), summarize/narrow ONLY the prompt-facing prior_summary via _narrow_prior_summary(), then continue with the next bounded phase",
        "MAX_RETRIES": "n/a — a one-time deterministic truncation applied before each phase call, not a retry loop",
        "FAIL_CLOSED_CONDITION": "narrowing never drops the durable ledger record, only the prompt text — validation still reads real sandbox state, never the summary",
        "ESCALATION_CONDITION": "not an escalation trigger by itself — narrowing is silent, bounded housekeeping, recorded in the phase entry for observability",
    },
    "PARTIAL_IMPLEMENTATION": {
        "DETECTION": "a phase escalates or the job is interrupted after >=1 phase already completed with real files_touched",
        "RECOVERY_STRATEGY": "preserve every valid change already snapshotted (files_touched diff is computed and recorded regardless of the terminal outcome); the ESCALATED/FAILED checkpoint's phases_state + files_touched IS the exact record of what remains unfinished for a targeted follow-up",
        "MAX_RETRIES": "n/a — this is a record-preservation guarantee, not a retry",
        "FAIL_CLOSED_CONDITION": "partial files are never presented as promotion_eligible — only a full validation+canary pass on the complete change-set can set that",
        "ESCALATION_CONDITION": "same as the underlying phase's own escalation (VALIDATION_FAILURE / MODEL_FAILURE / etc.) — PARTIAL_IMPLEMENTATION only changes what gets RECORDED, never the escalation trigger itself",
    },
    "NO_PROGRESS": {
        "DETECTION": "within a single phase's model-failover loop: two consecutive attempts produce an IDENTICAL sandbox snapshot diff (no files added/modified/removed) despite a retryable outcome. Across repair cycles: the deterministic validation failure signature (failing check names) repeats unchanged after a full repair attempt (repeated_failure_signature, recorded but not solely trusted -- see MAX_RETRIES)",
        "RECOVERY_STRATEGY": "within a phase: stop model failover immediately (no_progress_stop) rather than exhausting the full candidate list on further zero-diff attempts. Across repair cycles: recorded for observability only -- a validator can legitimately report a generic/empty checks list between attempts even when real progress differs, so signature-repetition alone never truncates the repair budget",
        "MAX_RETRIES": "within a phase: 1 repeated zero-diff attempt is the hard ceiling (bounded, tested). Across repair cycles: unchanged from VALIDATION_FAILURE's own bound, DECOMPOSED_MAX_REPAIR_CYCLES -- NO_PROGRESS detection there is telemetry, not an additional retry limit",
        "FAIL_CLOSED_CONDITION": "a no-progress phase or repair cycle is never marked finished/successful — its real final_action (error/escalate/iteration_ceiling_reached) and the deterministic validation outcome are preserved untouched",
        "ESCALATION_CONDITION": "no_progress_stop=True is recorded on the specific attempt that triggered it; repeated_failure_signature=True is recorded on a repair phase entry for Founder/telemetry visibility, without itself forcing escalation",
    },
}

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
    partial_progress: dict[str, Any] | None = None  # PARTIAL_IMPLEMENTATION: which phases completed / which files were validly changed before an escalate, if any
    decomposed: bool = False  # True when this JobResult came from submit_job_decomposed() (directly or via submit_job_auto())


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


def submit_job_auto(
    task: str, *, requested_by: str = "unspecified", timeout_s: int = DEFAULT_TIMEOUT_S,
    founder_approved: bool = False, model: str = DEFAULT_MODEL,
    max_iterations: int = MAX_ITERATIONS,
    validation_config: dict[str, Any] | None = None,
    on_job_created: Callable[[str], None] | None = None,
    source_paths: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> JobResult:
    """ROUTER_INTEGRATION (OMNI_GOD_MODE_V1 Phase 3): the single entry point
    every caller of Omni Engineer should use going forward -- task_router.py
    (via omni_engineer_adapter.py) and evolution/advance.py's
    _run_omni_engineer() both call THIS, not submit_job()/submit_job_decomposed()
    directly, so complexity selection lives in exactly one place inside the
    Omni capability boundary instead of being duplicated at every call site.
    classify_complexity() decides simple vs. decomposed; everything else
    (authority, source_paths, validation) behaves identically to calling the
    chosen function directly -- this is a thin dispatcher, not a new engine
    or a new router."""
    complexity = classify_complexity(task)
    if complexity.decomposition_eligible:
        return submit_job_decomposed(
            task, requested_by=requested_by, timeout_s=timeout_s,
            founder_approved=founder_approved, model=model,
            max_iterations_per_phase=DECOMPOSED_MAX_ITERATIONS_PER_PHASE,
            validation_config=validation_config, on_job_created=on_job_created,
            source_paths=source_paths,
        )
    return submit_job(
        task, requested_by=requested_by, timeout_s=timeout_s,
        founder_approved=founder_approved, model=model, max_iterations=max_iterations,
        validation_config=validation_config, on_job_created=on_job_created,
        source_paths=source_paths, allowed_tools=allowed_tools,
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
                except (provider_b_bridge.ModelArtifactMissing, FileNotFoundError) as exc:
                    # GOD_MODE_V1 FINAL PROVIDER_B CLOSURE: Provider B is
                    # unavailable either because it has no usable local
                    # model artifact (ModelArtifactMissing) OR because its
                    # llama-server binary itself is missing on this machine
                    # (FileNotFoundError from subprocess.Popen) -- both are
                    # honest backend unavailability, never an unhandled
                    # Omni Engineer runtime failure/crash.
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
                except (provider_b_bridge.ModelArtifactMissing, FileNotFoundError) as exc:
                    # Provider B is an optional cross-provider fallback.
                    # A missing local model artifact OR a missing
                    # llama-server binary (FileNotFoundError) both mean
                    # this backend is unavailable; neither may terminate
                    # the parent Omni Engineer job. Record the evidence and
                    # continue through the pre-existing governed fallback
                    # path below.
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


def _phase_task_text(parent_task: str, phase_name: str, phase_objective: str, prior_summary: str,
                      allowed_tools: frozenset[str] | None,
                      staged_source_summary: list[str] | None = None) -> str:
    parts = [
        "You are working on ONE BOUNDED PHASE of a larger, already-authorized engineering "
        "objective. Complete ONLY this phase's objective, then call finish with a short summary "
        "of exactly what you did/found. Do not attempt work that belongs to a later phase.",
        f"\nOVERALL OBJECTIVE (context only -- do not exceed this phase's scope):\n{parent_task}",
        f"\nCURRENT PHASE: {phase_name.upper()}",
        f"\nPHASE OBJECTIVE:\n{phase_objective}",
    ]
    # PHASE_SOURCE_PATHS_NARROWING (GOD_MODE_V1 FINAL GAP CLOSURE): the raw
    # staged-file list is only restated to the INSPECT phase (whose job is
    # to survey everything authorized). Later phases receive the SAME files
    # in their sandbox (already copied once, before inspect -- never
    # re-copied, never broadened) but are NOT re-told the full list; they
    # rely on prior_summary (the inspect phase's own distilled findings,
    # itself bounded by CONTEXT_PRESSURE narrowing) instead. This is
    # deliberately narrower prompt context per phase, not merely "the same
    # thing every time."
    if staged_source_summary:
        parts.append(
            "\nPRE-STAGED AUTHORIZED SOURCE FILE(S) in this sandbox (inspect these; nothing else "
            "exists outside this sandbox regardless):\n- " + "\n- ".join(staged_source_summary)
        )
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


CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT = 4000  # prompt-facing prior_summary narrowing threshold (CONTEXT_PRESSURE)


def _narrow_prior_summary(prior_summary: str) -> tuple[str, bool]:
    """CONTEXT_PRESSURE recovery: narrow the PROMPT-FACING prior_summary text
    once it grows past a bounded limit, keeping the most recent (most
    relevant to the next phase) content. Never touches the durable
    phases_state ledger record — only what gets fed back into the next
    model prompt. Returns (possibly-narrowed text, was_narrowed)."""
    if len(prior_summary) <= CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT:
        return prior_summary, False
    head = "[earlier phase summaries truncated for context pressure -- see the durable phase ledger for full history]\n"
    tail = prior_summary[-(CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT - len(head)):]
    return head + tail, True


def _classify_validation_failure(vres: Any) -> str:
    """TEST_FAILURE vs generic VALIDATION_FAILURE: a failing check whose own
    name signals it ran tests (vs. a static/lint/schema check) gets the more
    specific class so the repair objective can name it precisely."""
    try:
        checks = vres.to_json().get("checks", [])
    except Exception:  # noqa: BLE001 — classification must never break the repair loop itself
        return "validation_failure"
    for c in checks:
        if not c.get("passed", True) and "test" in str(c.get("name", "")).lower():
            return "test_failure"
    return "validation_failure"


def _run_phase(
    phase_name: str, phase_objective: str, workdir: Path, *, parent_task: str,
    prior_summary: str, model: str, max_iterations: int, timeout_s: int,
    staged_source_summary: list[str] | None = None,
) -> tuple[Any, str, list[str], list[dict[str, Any]]]:
    """Runs ONE bounded phase in the SAME sandbox the parent job already
    owns. Bounded model failover on a retryable outcome (iteration_ceiling_
    reached/timeout/model_unavailable/error): try the next REAL installed
    model (_filter_actually_installed(), same real-time check as the
    whole-job path), never a phantom configured-only one; once same-provider
    (ollama) candidates are exhausted, tries independent provider_b exactly
    once (PROVIDER_FAILURE), skipped if its circuit breaker is open. finish/
    escalate are always terminal for the phase -- never retried with a
    different model. NO_PROGRESS: two consecutive attempts producing an
    IDENTICAL sandbox diff stop the failover loop early rather than
    exhausting every remaining candidate on a strategy that isn't working.

    PHASE_NARRATIVE_ACCURACY: every attempt (not just the last) is recorded
    in the returned attempt_history, each with its OWN files-touched diff --
    so an earlier model's real completed work is never erased from the
    record just because a later model in the same phase's failover chain
    then failed. Returns (AgentRunResult, model_actually_used,
    attempted_models, attempt_history)."""
    allowed = _PHASE_TOOLS.get(phase_name)
    task_text = _phase_task_text(parent_task, phase_name, phase_objective, prior_summary, allowed,
                                  staged_source_summary=staged_source_summary)
    attempted: list[str] = []
    attempt_history: list[dict[str, Any]] = []
    current_model = model
    current_provider = "ollama"
    t0 = time.monotonic()
    phase_start_snapshot = _snapshot(workdir)
    prev_attempt_diff: dict[str, list[str]] | None = None
    consecutive_no_progress = 0

    while True:
        attempted.append(current_model)
        before_attempt = _snapshot(workdir)
        remaining = max(30, timeout_s - int(time.monotonic() - t0))
        run = run_agent_loop(
            task_text, workdir, model=current_model, provider=current_provider,
            max_iterations=max_iterations, timeout_s=remaining, allowed_tools=allowed,
        )
        after_attempt = _snapshot(workdir)
        attempt_diff = _diff_snapshots(before_attempt, after_attempt)
        attempt_has_progress = any(attempt_diff.get(k) for k in ("added", "modified", "removed"))
        no_progress_this_attempt = (not attempt_has_progress) and run.final_action in RETRYABLE_FINAL_ACTIONS
        attempt_history.append({
            "model": current_model, "provider": current_provider, "final_action": run.final_action,
            "summary": run.summary_or_reason or "", "commands_executed": list(run.commands_executed),
            "files_touched": attempt_diff, "no_progress": no_progress_this_attempt,
        })

        if run.final_action in ("finish", "escalate"):
            return run, current_model, attempted, attempt_history

        if no_progress_this_attempt and prev_attempt_diff == attempt_diff:
            consecutive_no_progress += 1
        else:
            consecutive_no_progress = 0
        prev_attempt_diff = attempt_diff
        if consecutive_no_progress >= 1:  # NO_PROGRESS: 1 repeated zero-diff attempt is the hard ceiling
            attempt_history[-1]["no_progress_stop"] = True
            return run, current_model, attempted, attempt_history

        # MODEL_FAILURE: try the next real installed model, same provider.
        if current_provider == "ollama":
            health = local_model_health.check(model=current_model)
            candidates = local_model_health.engineering_failover_order(exclude=attempted)
            candidates, _not_installed = _filter_actually_installed(candidates, health.models if health.available else [])
            if candidates:
                current_model = candidates[0]
                continue

        # PROVIDER_FAILURE: same-provider (ollama) chain exhausted -- try
        # the independent provider_b exactly once per phase.
        provider_b_already_tried = any(a["provider"] == "provider_b" for a in attempt_history)
        if current_provider == "ollama" and not provider_b_already_tried:
            if not local_model_health.circuit_is_open("provider_b"):
                remaining = max(30, timeout_s - int(time.monotonic() - t0))
                if remaining > 30:
                    import provider_b_bridge
                    try:
                        pb_health = provider_b_bridge.ensure_running()
                    except (provider_b_bridge.ModelArtifactMissing, FileNotFoundError):
                        pb_health = None
                    if pb_health is not None and pb_health.available:
                        current_model = provider_b_bridge.DEFAULT_MODEL
                        current_provider = "provider_b"
                        continue
        return run, current_model, attempted, attempt_history


# ---- SOURCE_PATH / CONTEXT STAGING for decomposed jobs (Phase 3) ----------
# GATED_PATH_MARKERS (authority_policy.py) already blocks a job from ever
# reaching credential/protected paths at all (FOUNDER_GATED). This is a
# SEPARATE, complementary hygiene filter: even an authority-safe path can be
# noisy, historical, or irrelevant context that should never be silently
# staged into a bounded phase's prompt just because a caller's source_paths
# list happened to include it. Default-exclude; live canonical source wins
# over historical copies.
CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS = (
    "manual_candidates",
    "continuity_backup",
    "/backups/",
    "_backup/",
    "archived_jobs",
    "archive/",  # generic archive trees, distinct from archived_jobs above
    "founder_receipts",
    "content_packet",
    "/logs/",
    ".log",
    "/jobs/",  # another job's own historical workdir tree
    # GOD_MODE_V1 FINAL GAP CLOSURE: defense-in-depth secret/key exclusion.
    # authority_policy.GATED_PATH_MARKERS already REJECTS the whole job
    # outright (a stronger guarantee) if a source_path touches one of these
    # -- this list ensures the STAGING layer itself never copies such a
    # path either, in case a future caller reaches this filter through a
    # path that does not also run classify() first.
    "secrets",
    "credentials",
    ".env",
    "/.ssh",
    "/.aws",
    "/.config/gcloud",
)


def _stage_context_source_paths(source_paths: list[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    """Splits caller-provided source_paths into (allowed, excluded) per the
    default-exclusion markers above. Returns the excluded list with its
    matched marker for durable, honest recording -- callers never silently
    lose a path without a reason on record."""
    allowed: list[Path] = []
    excluded: list[dict[str, str]] = []
    for p in source_paths:
        p_str = str(p)
        marker = next((m for m in CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS if m.lower() in p_str.lower()), None)
        if marker:
            excluded.append({"path": p_str, "excluded_marker": marker})
        else:
            allowed.append(p)
    return allowed, excluded


def submit_job_decomposed(
    task: str, *, requested_by: str = "unspecified", timeout_s: int = DEFAULT_TIMEOUT_S,
    founder_approved: bool = False, model: str = DEFAULT_MODEL,
    max_iterations_per_phase: int = DECOMPOSED_MAX_ITERATIONS_PER_PHASE,
    validation_config: dict[str, Any] | None = None,
    on_job_created: Callable[[str], None] | None = None,
    source_paths: list[str] | None = None,
) -> JobResult:
    """Bounded, phase-decomposed alternative to submit_job() for large/
    complex objectives. One canonical job_id/sandbox/ledger throughout.
    Fixed phase sequence: INSPECT -> IMPLEMENT -> TEST -> (deterministic
    VALIDATE, reusing validation.py exactly as submit_job()'s single-loop
    path does) -> up to DECOMPOSED_MAX_REPAIR_CYCLES bounded REPAIR phases
    on a validation failure, re-validating after each. Never marks a job
    successful on a phase's own 'finish' call alone; final success is always
    the deterministic validation+canary+independent_validation pipeline,
    identically to submit_job().

    source_paths (Phase 3): GOVERNED, same mechanism as submit_job() --
    explicitly authorized real files/dirs, checked against
    authority_policy.GATED_PATH_MARKERS, copied ONCE into the sandbox before
    the inspect phase starts (never re-copied mid-job, so a later phase can
    never silently broaden its own context). Additionally passed through
    _stage_context_source_paths(), which default-excludes noisy/historical
    trees (manual_candidates/, backups/, other jobs' workdirs, receipts,
    logs) even when authority-safe -- a context-hygiene filter, distinct
    from and in addition to the authority gate. The fully-automatic
    evolution/advance.py proposal pipeline deliberately never passes
    source_paths here (same "no recursive self-modification" doctrine it
    already applies to submit_job()) -- this parameter is for explicitly
    authorized, non-automatic callers only."""
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
    all_source_paths = [Path(p) for p in (source_paths or [])]
    staged_source_paths, excluded_source_paths = _stage_context_source_paths(all_source_paths)
    job_ledger.create(
        job_id, task=task, requested_by=requested_by, sandbox_path=str(workdir),
        model=model, max_iterations=max_iterations_per_phase,
        submit_params={"decomposed": True, "validation_config": validation_config,
                       "source_paths": _json_safe_submit_value(source_paths or [], field="source_paths"),
                       "context_staging_excluded": excluded_source_paths},
    )
    job_ledger.claim(job_id, owner=requested_by)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    try:
        decision = classify(
            task_description=task, requested_tools=set(), sandbox_root=workdir,
            source_paths=all_source_paths, founder_approved=founder_approved, adapter=OMNI_ENGINEER_ID,
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

        # Copy staged (authority-safe AND context-staging-safe) source_paths
        # into the sandbox ONCE, before the inspect phase starts -- identical
        # mechanism to submit_job()'s copy_source_paths step. This happens
        # BEFORE before_all is snapshotted, so copied files are never
        # reported as "added" by any phase -- only what a phase actually
        # changes shows up in files_changed. No later phase re-copies or
        # extends this set -- source_paths cannot silently broaden mid-job.
        staged_relative_names: list[str] = []
        if staged_source_paths:
            import shutil as _shutil
            for src in staged_source_paths:
                dest = _source_sandbox_destination(workdir, src)
                if src.is_dir():
                    _shutil.copytree(src, dest, dirs_exist_ok=True)
                    # List individual staged files (bounded), not just the
                    # directory name -- the model needs real filenames to
                    # act on, not merely "a directory exists".
                    staged_relative_names.extend(
                        str(f.relative_to(workdir)) for f in sorted(dest.rglob("*"))[:50] if f.is_file()
                    )
                elif src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(src, dest)
                    staged_relative_names.append(str(dest.relative_to(workdir)))

        before_all = _snapshot(workdir)
        phases_state: list[dict[str, Any]] = []
        all_commands: list[str] = []
        attempted_models_all: list[str] = []
        prior_summary = ""

        def _record_phase(name: str, objective: str, run, model_used: str, attempted: list[str],
                           attempt_history: list[dict[str, Any]]) -> dict[str, Any]:
            # PHASE_NARRATIVE_ACCURACY: `final_action`/`summary` still record
            # the LAST attempt's own outcome (the decision that ended the
            # phase) -- but `attempts` preserves every attempt's own
            # files_touched, so an earlier model's real completed work is
            # never erased just because a later model in this phase's
            # failover chain then failed. `progress_preserved` is a cheap,
            # honest at-a-glance signal: True whenever ANY attempt in this
            # phase produced a real sandbox diff, regardless of which
            # attempt's final_action reads as success or failure.
            progress_preserved = any(
                any(a["files_touched"].get(k) for k in ("added", "modified", "removed"))
                for a in attempt_history
            )
            entry = {
                "name": name, "objective": objective[:500],
                "final_action": run.final_action, "summary": (run.summary_or_reason or "")[:800],
                "model": model_used, "attempted_models": attempted,
                "commands_executed": list(run.commands_executed), "ended_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempt_history, "progress_preserved": progress_preserved,
            }
            phases_state.append(entry)
            all_commands.extend(run.commands_executed)
            for m in attempted:
                if m not in attempted_models_all:
                    attempted_models_all.append(m)
            job_ledger.checkpoint(job_id, JobState.EDITING, phases=list(phases_state),
                                   note=f"phase {name!r} finished: {run.final_action} "
                                        f"(progress_preserved={progress_preserved}, {len(attempt_history)} attempt(s))")
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
            prior_summary, _narrowed = _narrow_prior_summary(prior_summary)  # CONTEXT_PRESSURE
            # PHASE_SOURCE_PATHS_NARROWING: only 'inspect' is (re-)told the
            # staged file list -- later phases rely on prior_summary alone.
            run, mdl, attempted, attempt_history = _run_phase(
                phase_name, phase_objective, workdir, parent_task=task, prior_summary=prior_summary,
                model=model, max_iterations=max_iterations_per_phase,
                timeout_s=max(30, timeout_s - int(time.monotonic() - t0)),
                staged_source_summary=staged_relative_names if phase_name == "inspect" else None,
            )
            entry = _record_phase(phase_name, phase_objective, run, mdl, attempted, attempt_history)
            if run.final_action == "escalate":
                escalated = True
                break
            prior_summary = (prior_summary + f"\n[{phase_name}] {entry['summary']}").strip()

        if escalated:
            escalated_files_changed = _diff_snapshots(before_all, _snapshot(workdir))
            # PARTIAL_IMPLEMENTATION: preserve exactly what completed and
            # what did not -- never erased just because the LAST phase
            # escalated.
            partial_progress = {
                "phases_completed": [p["name"] for p in phases_state[:-1]],
                "phase_that_escalated": phases_state[-1]["name"] if phases_state else None,
                "any_progress_preserved": any(p.get("progress_preserved") for p in phases_state),
                "files_touched_before_escalation": escalated_files_changed,
            }
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
                attempted_models=attempted_models_all, partial_progress=partial_progress, decomposed=True,
            )
            _finalize(result, requested_by=requested_by)
            return result

        # Deterministic VALIDATE, identical pipeline to submit_job()'s single-loop path.
        after = _snapshot(workdir)
        files_changed = _diff_snapshots(before_all, after)
        job_ledger.checkpoint(job_id, JobState.VALIDATING, phases=phases_state)
        vres = validation.validate(workdir, files_changed, config=validation_config)

        repair_cycles = 0
        last_failure_signature: str | None = None
        # NOTE on scope: DECOMPOSED_MAX_REPAIR_CYCLES is the sole, already-
        # tested bound on this loop (see
        # test_decomposed_repair_cycles_are_bounded, which deliberately
        # asserts the FULL bounded budget is always spent under continuous
        # failure -- a real validator can legitimately report an empty/
        # generic checks list between attempts even when real underlying
        # progress differs, so an identical failure_signature alone is not
        # reliable enough evidence to cut that budget short). NO_PROGRESS
        # here is therefore DETECTED and RECORDED (repeated_failure_signature
        # on the phase entry, for observability/telemetry) but never used to
        # truncate the repair budget -- MAX_RETRIES for VALIDATION_FAILURE/
        # TEST_FAILURE stays exactly DECOMPOSED_MAX_REPAIR_CYCLES, matching
        # ADAPTIVE_RETRY_MATRIX. The stricter, diff-based NO_PROGRESS early
        # exit lives in _run_phase()'s own model-failover loop instead, where
        # an empty sandbox diff IS reliable evidence (see there).
        while not vres.passed and repair_cycles < DECOMPOSED_MAX_REPAIR_CYCLES and len(phases_state) < DECOMPOSED_MAX_TOTAL_PHASES:
            repair_cycles += 1
            failure_class = _classify_validation_failure(vres)  # TEST_FAILURE vs generic VALIDATION_FAILURE
            failure_signature = json.dumps(
                sorted(c.get("name", "") for c in vres.to_json().get("checks", []) if not c.get("passed", True)))
            repeated_failure_signature = last_failure_signature is not None and failure_signature == last_failure_signature
            last_failure_signature = failure_signature
            repair_objective = (
                f"Validation FAILED ({failure_class.upper()}, repair attempt {repair_cycles}/{DECOMPOSED_MAX_REPAIR_CYCLES}). "
                f"Fix the SPECIFIC failing check(s) named below -- do not make unrelated changes. "
                f"Then call finish.\n{json.dumps(vres.to_json())[:1500]}"
            )
            prior_summary, _narrowed = _narrow_prior_summary(prior_summary)  # CONTEXT_PRESSURE
            run, mdl, attempted, attempt_history = _run_phase(
                "repair", repair_objective, workdir, parent_task=task, prior_summary=prior_summary,
                model=model, max_iterations=max_iterations_per_phase,
                timeout_s=max(30, timeout_s - int(time.monotonic() - t0)),
            )
            entry = _record_phase(f"repair_{repair_cycles}", repair_objective, run, mdl, attempted, attempt_history)
            entry["failure_class"] = failure_class
            entry["repeated_failure_signature"] = repeated_failure_signature
            if run.final_action == "escalate":
                partial_progress = {
                    "phases_completed": [p["name"] for p in phases_state[:-1]],
                    "phase_that_escalated": entry["name"],
                    "any_progress_preserved": any(p.get("progress_preserved") for p in phases_state),
                    "files_touched_before_escalation": files_changed,
                    "failure_class_at_escalation": failure_class,
                }
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
                    validation=vres.to_json(), partial_progress=partial_progress, decomposed=True,
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
            decomposed=True,
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
