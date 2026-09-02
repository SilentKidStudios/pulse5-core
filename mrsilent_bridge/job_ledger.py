"""
Durable Job Ledger — the crash-safe operational layer under every OmniEngineer
job. Extends the EXISTING per-job directory (`jobs/<job_id>/`) that
bridge.py/local_model_bridge.py/omniengineer_harness.py already use for
`result.json`/`validation.json`/the sandbox itself — this is not a second,
parallel job-tracking system. `jobs/<job_id>/ledger.json` is written
incrementally, at every meaningful checkpoint, whereas `result.json` is only
ever written once, at the very end. That incremental write is the entire
point: if the process/session/server dies mid-job, `result.json` never gets
written, but `ledger.json` already reflects the last checkpoint reached, so a
later process can inspect it instead of guessing.

Every write here is atomic (temp file + `os.replace`, which is atomic on the
same filesystem) — a checkpoint write interrupted mid-write can never leave a
torn/corrupt `ledger.json` behind; the reader either sees the old complete
state or the new complete state, never a partial one.

This module intentionally does NOT write to audit.jsonl on every checkpoint —
audit.py's log is a job-level (not iteration-level) record, and OBSERVE's
signal detectors already scan it; spamming one audit entry per tool-call
iteration would both bloat that log and manufacture new false-positive
signal volume for no benefit. The ledger is its own, separate, higher-
frequency record; the existing single end-of-job `audit.record()` calls in
bridge.py/omniengineer_harness.py/evolution/advance.py are unchanged.

RECOVERY POLICY (`classify()`) is deliberately conservative and coarse.
Genuinely replaying a partially-completed ReAct tool-call transcript after an
unknown interruption point was considered and rejected: reconstructing exact
conversational state the model "remembers" is fragile, and an interruption
could have happened before, during, or after a tool's side effect actually
landed — there is no way to know from outside whether a `run_command` that
was "in flight" at crash time actually completed. Trying to resume the exact
transcript risks the model acting on a false belief about what it already
did. Instead, recovery is split into exactly two safe categories:

  - SAFE_RESUME: nothing sandbox-mutating has happened yet (state is one of
    CREATED/AUTHORIZED/ROUTED/SANDBOX_READY/PLANNING) — the sandbox is either
    empty or read-only-probed, so simply re-running the same job fresh, in
    the same job_id/sandbox, is unambiguously safe. Nothing to "resume",
    nothing to lose.
  - RESTART_FROM_SANDBOX: some edit may have happened (state is one of
    EDITING/TESTING/REPAIRING/VALIDATING/CANARY) — recovery does NOT replay
    the old transcript. It starts a brand-new bounded agent loop pointed at
    the SAME existing sandbox directory (files preserved, not wiped), with
    the task text explicitly annotated that prior partial progress may exist
    and must be inspected (`list_files`/`read_file`) before writing anything
    new. This is "never blindly repeat an external/destructive action" in
    concrete form: the harness never re-issues the literal same tool calls
    from before crash-blind; it hands the model the CURRENT real state and
    lets it decide what (if anything) still needs doing.

Anything else (already terminal, authority-pending, or a job that has
already been resumed once) is ESCALATE / TERMINAL_FAILURE / FOUNDER_REQUIRED
— never auto-resumed a second time (see MAX_RESUME_ATTEMPTS below), and
"no recovery may bypass authority": a job that was FOUNDER_GATED before
interruption is still FOUNDER_GATED after it.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

BRIDGE_ROOT = Path(__file__).resolve().parent
JOBS_ROOT = BRIDGE_ROOT / "jobs"

# A checkpoint older than this, on a non-terminal job, is "stale" — the
# process that was running it is presumed dead or hung. Generous relative to
# MODEL_CALL_TIMEOUT_S/TOOL_TIMEOUT_S in omniengineer_agent.py so a merely-
# slow-but-alive job is never misclassified as stale.
STALE_AFTER_S = 900  # 15 minutes

MAX_RESUME_ATTEMPTS = 1  # bounded: a job may be auto-resumed at most once


class JobState(str, Enum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    ROUTED = "routed"
    SANDBOX_READY = "sandbox_ready"
    PLANNING = "planning"
    EDITING = "editing"
    TESTING = "testing"
    REPAIRING = "repairing"
    VALIDATING = "validating"
    CANARY = "canary"
    PROMOTION_CANDIDATE = "promotion_candidate"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.ESCALATED})
PRE_MUTATION_STATES = frozenset({
    JobState.CREATED, JobState.AUTHORIZED, JobState.ROUTED,
    JobState.SANDBOX_READY, JobState.PLANNING,
})
POST_MUTATION_STATES = frozenset({
    JobState.EDITING, JobState.TESTING, JobState.REPAIRING,
    JobState.VALIDATING, JobState.CANARY, JobState.PROMOTION_CANDIDATE,
})


class RecoveryPolicy(str, Enum):
    SAFE_RESUME = "safe_resume"
    RESTART_FROM_SANDBOX = "restart_from_sandbox"
    ESCALATE = "escalate"
    TERMINAL_FAILURE = "terminal_failure"
    FOUNDER_REQUIRED = "founder_required"


def task_fingerprint(task: str) -> str:
    """Stable identity for 'is this the same in-flight request', independent
    of who asked — used for duplicate-in-flight suppression (find_active_by_
    fingerprint), not for permanent dedup (a legitimately repeated request
    after completion is not a duplicate)."""
    return hashlib.sha256(task.strip().encode()).hexdigest()[:16]


@dataclass
class LedgerRecord:
    job_id: str
    task: str
    task_fingerprint: str
    requested_by: str
    sandbox_path: str
    state: str = JobState.CREATED.value
    risk_class: str | None = None
    approval_state: str | None = None
    authority_state: str | None = None
    selected_engine: str | None = None
    engines_considered: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    iteration: int = 0
    max_iterations: int = 0
    model_calls: int = 0
    files_touched: dict[str, list[str]] = field(
        default_factory=lambda: {"added": [], "modified": [], "removed": []})
    commands_executed: list[str] = field(default_factory=list)
    validation_result: dict[str, Any] | None = None
    canary_result: dict[str, Any] | None = None
    independent_validation_result: dict[str, Any] | None = None  # SINGLE_VALIDATOR_DEPENDENCY: evolution.independent_validation.recheck() result, if run
    promotion_eligible: bool = False
    created_at: str = ""
    updated_at: str = ""
    heartbeat: str = ""
    terminal_result: str | None = None
    error_class: str | None = None
    resume_count: int = 0
    resume_eligibility: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    submit_params: dict[str, Any] = field(default_factory=dict)  # adapter-specific reconstruction data for resume
    # (e.g. bridge.py stashes tools/source_paths/validation_config here — fields
    # this generic schema deliberately doesn't hardcode. OmniEngineer doesn't
    # need this: model/max_iterations above already cover its own resume needs.)
    attempted_models: list[str] = field(default_factory=list)  # SINGLE_MODEL_DEPENDENCY: every model tried, in order, this job
    model_failure_reasons: dict[str, str] = field(default_factory=dict)  # {model: why it was abandoned} — never overwritten, one entry per attempted model
    attempted_providers: list[str] = field(default_factory=list)  # OLLAMA_SINGLE_PROVIDER_DEPENDENCY: every provider tried, in order, this job
    provider: str | None = None
    fallback_reason: str | None = None
    worker_backend: str | None = None
    # OMNI_GOD_MODE_V1 PHASE 2: durable bounded task-decomposition state, one
    # dict per completed/attempted phase: {name, objective, max_iterations,
    # final_action, files_touched, commands_executed, started_at, ended_at}.
    # Empty for every job that isn't decomposed (the overwhelming majority)
    # -- default_factory=[] keeps this fully backward-compatible with every
    # existing ledger.json on disk. The parent job_id/sandbox/ledger record
    # remains the ONE canonical identity throughout every phase; this is not
    # a second job-tracking system, just structured history on the existing
    # record.
    phases: list[dict[str, Any]] = field(default_factory=list)


def _path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "ledger.json"


def _lock_path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "ledger.lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)  # atomic on the same filesystem — no torn writes


def create(
    job_id: str, *, task: str, requested_by: str, sandbox_path: str,
    model: str | None = None, max_iterations: int = 0,
    submit_params: dict[str, Any] | None = None,
) -> LedgerRecord:
    now = _now()
    record = LedgerRecord(
        job_id=job_id, task=task, task_fingerprint=task_fingerprint(task),
        requested_by=requested_by, sandbox_path=sandbox_path,
        state=JobState.CREATED.value, model=model, max_iterations=max_iterations,
        created_at=now, updated_at=now, heartbeat=now,
        history=[{"state": JobState.CREATED.value, "at": now, "note": "job created"}],
        submit_params=submit_params or {},
    )
    _atomic_write_json(_path(job_id), asdict(record))
    return record


def load(job_id: str) -> LedgerRecord | None:
    p = _path(job_id)
    if not p.exists():
        return None
    return LedgerRecord(**json.loads(p.read_text()))



def record_execution_metadata(
    job_id: str,
    *,
    provider: str | None,
    fallback_reason: str | None,
    worker_backend: str | None = None,
):
    """Persist final execution routing metadata without changing job state."""
    record = load(job_id)
    record.provider = provider
    record.fallback_reason = fallback_reason
    record.worker_backend = worker_backend

    now = _now()
    record.updated_at = now
    record.heartbeat = now

    _atomic_write_json(_path(job_id), asdict(record))
    return record


def checkpoint(job_id: str, state: JobState | str, *, note: str = "", **updates: Any) -> LedgerRecord:
    """The one function every phase transition goes through. Loads the
    current record, applies field updates (any LedgerRecord field), stamps
    state/updated_at/heartbeat, appends a history entry, atomically writes.
    Unknown kwargs are a programming error (fails loudly, not silently)."""
    record = load(job_id)
    if record is None:
        raise ValueError(f"no ledger record for job {job_id} — call create() first")
    state_value = state.value if isinstance(state, JobState) else state
    for k, v in updates.items():
        if not hasattr(record, k):
            raise TypeError(f"LedgerRecord has no field {k!r}")
        setattr(record, k, v)
    record.state = state_value
    now = _now()
    record.updated_at = now
    record.heartbeat = now
    record.history.append({"state": state_value, "at": now, "note": note})
    if state_value in {s.value for s in TERMINAL_STATES}:
        record.resume_eligibility = RecoveryPolicy.TERMINAL_FAILURE.value if state_value != JobState.COMPLETED.value else None
    _atomic_write_json(_path(job_id), asdict(record))
    return record


def touch_heartbeat(job_id: str) -> None:
    """Refresh heartbeat without a state transition — for long single
    operations (e.g. a model call) so staleness detection doesn't false-
    positive on a job that is merely slow but still alive."""
    record = load(job_id)
    if record is None:
        return
    record.heartbeat = _now()
    _atomic_write_json(_path(job_id), asdict(record))


def list_all() -> list[LedgerRecord]:
    if not JOBS_ROOT.exists():
        return []
    out = []
    for d in sorted(JOBS_ROOT.iterdir()):
        p = d / "ledger.json"
        if p.exists():
            try:
                out.append(LedgerRecord(**json.loads(p.read_text())))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def find_active_by_fingerprint(fingerprint: str) -> LedgerRecord | None:
    """Idempotency check: is a job with this exact task text already
    in-flight (non-terminal)? Only considers ACTIVE jobs — a completed job
    with the same fingerprint is not a duplicate, it's history; the same
    task submitted again later is a new, legitimate request."""
    for r in list_all():
        if r.task_fingerprint == fingerprint and r.state not in {s.value for s in TERMINAL_STATES}:
            return r
    return None


def is_stale(record: LedgerRecord, *, stale_after_s: int = STALE_AFTER_S) -> bool:
    """DEFECT_SIGNAL=389f05d585044a8b: a job's ledger `heartbeat` field gets
    refreshed by EVERY checkpoint() call, including the one that starts a
    resume attempt -- so if that resuming process itself then crashes
    (e.g. an infra failure mid-_execute()) before reaching another
    checkpoint, the heartbeat looks "fresh" for up to stale_after_s
    afterward even though the owner is now definitively dead. Meanwhile the
    job's LOCK FILE (if one is currently held) carries a directly
    verifiable fact -- is that exact pid still alive right now -- that is
    strictly stronger evidence than a timestamp either side could have
    refreshed. When a lock is held, its pid-liveness is authoritative and
    OVERRIDES the heartbeat in both directions: a live pid means NOT stale
    even if the heartbeat happens to look old (protects a genuinely slow
    but alive worker — see STALE_AFTER_S's own "generous" comment above),
    and a dead pid means stale even if the heartbeat was just refreshed
    moments before the owner crashed. Only when NO lock is currently held
    (never claimed, or already cleanly released) does this fall back to
    the heartbeat-only check, exactly as before."""
    if record.state in {s.value for s in TERMINAL_STATES}:
        return False
    lock = lock_status(record.job_id)
    if lock.get("held"):
        # corrupt/unreadable lock info can't prove liveness either way --
        # default to "alive" (i.e. NOT stale) so genuinely ambiguous
        # evidence fails closed rather than being treated as recoverable.
        return not lock.get("alive", True)
    try:
        hb = datetime.fromisoformat(record.heartbeat)
    except (ValueError, TypeError):
        return True  # unreadable heartbeat on a non-terminal job is itself a red flag
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb).total_seconds() > stale_after_s


def classify(record: LedgerRecord) -> RecoveryPolicy:
    """Pure, read-only recovery-policy decision — never executes anything.
    See module docstring for the full reasoning."""
    if record.state in {s.value for s in TERMINAL_STATES}:
        return RecoveryPolicy.TERMINAL_FAILURE
    if record.approval_state == "pending_approval":
        # was FOUNDER_GATED and awaiting approval when interrupted — no
        # recovery may bypass authority; a human must decide, not a resumer.
        return RecoveryPolicy.FOUNDER_REQUIRED
    if record.resume_count >= MAX_RESUME_ATTEMPTS:
        return RecoveryPolicy.ESCALATE
    if not is_stale(record):
        # heartbeat is fresh — a live process may genuinely still own this;
        # recovery is not yet warranted (see lock_status()/claim() for the
        # authoritative "is someone actually still working on this" check).
        return RecoveryPolicy.ESCALATE
    if record.state in {s.value for s in PRE_MUTATION_STATES}:
        return RecoveryPolicy.SAFE_RESUME
    if record.state in {s.value for s in POST_MUTATION_STATES}:
        return RecoveryPolicy.RESTART_FROM_SANDBOX
    return RecoveryPolicy.ESCALATE


# ---- concurrency: local file-lock claim/release ----------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still alive
    return True


def lock_status(job_id: str) -> dict[str, Any]:
    p = _lock_path(job_id)
    if not p.exists():
        return {"held": False}
    try:
        info = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"held": True, "corrupt": True}
    alive = _pid_alive(info.get("pid", -1))
    age_s = None
    try:
        claimed = datetime.fromisoformat(info["claimed_at"])
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - claimed).total_seconds()
    except (KeyError, ValueError):
        pass
    return {"held": True, "owner": info.get("owner"), "pid": info.get("pid"),
            "hostname": info.get("hostname"), "claimed_at": info.get("claimed_at"),
            "age_s": age_s, "alive": alive, "stale": (not alive) or (age_s is not None and age_s > STALE_AFTER_S)}


def claim(job_id: str, *, owner: str = "unspecified") -> bool:
    """Atomic exclusive claim via O_CREAT|O_EXCL — fails if another live
    claim already exists. Returns True iff this call acquired the lock."""
    p = _lock_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "owner": owner, "pid": os.getpid(), "hostname": socket.gethostname(), "claimed_at": _now(),
    }).encode()
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


def release(job_id: str, *, owner: str = "unspecified") -> bool:
    """Only removes a lock this exact process holds (checked by pid) — a
    release never clears a lock owned by someone else, even by the same
    logical owner string."""
    p = _lock_path(job_id)
    if not p.exists():
        return False
    try:
        info = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    if info.get("pid") != os.getpid():
        return False
    p.unlink(missing_ok=True)
    return True


def break_stale_lock(job_id: str, *, requested_by: str) -> bool:
    """Refuses unless the lock is genuinely stale (dead pid or aged out) or
    corrupt. Called explicitly by cli.py/autonomous_cycle.py's own startup
    reconciliation, and automatically-but-boundedly by claim_or_break_stale()
    below — never blindly, always gated by lock_status()'s own dead-pid/
    aged-out check."""
    status = lock_status(job_id)
    if not status.get("held"):
        return False
    if not status.get("stale") and not status.get("corrupt"):
        return False
    _lock_path(job_id).unlink(missing_ok=True)
    return True


def claim_or_break_stale(job_id: str, *, owner: str = "unspecified") -> tuple[bool, bool]:
    """Same atomic claim as claim(), but when the claim fails because the
    existing lock is itself provably stale (dead pid, or aged past
    STALE_AFTER_S — the exact same criteria break_stale_lock() already
    enforces), breaks that ONE stale lock and retries the claim exactly
    once. A lock held by a live, non-aged owner is NEVER broken or raced —
    that case still returns (False, False), byte-for-byte the same outcome
    claim() alone would give.

    Closes a real gap: is_stale()/classify() correctly call a job (or a
    proposal-impl-* virtual lock) recovery-eligible once its OWNER process
    is provably dead, but the orphaned per-job/per-proposal lock FILE that
    dead owner left behind (never reaching its own `finally: release()`)
    previously blocked every future claim() forever — resume_job() would
    refuse indefinitely, callers would fall through to a fresh attempt, and
    that fresh attempt would then be correctly deduped against the very
    same still-non-terminal stale record: a permanent mutated=false
    stalemate with no automatic path back to healthy. break_stale_lock()
    already existed for exactly this scenario but nothing in the resume/
    advance path ever called it (see bridge.py/omniengineer_harness.py
    resume_job() and evolution/advance.py advance_one()). Returns
    (claimed, broke_a_stale_lock) so callers can record the break in their
    own durable receipt/audit trail."""
    if claim(job_id, owner=owner):
        return True, False
    if break_stale_lock(job_id, requested_by=owner):
        return claim(job_id, owner=owner), True
    return False, False
