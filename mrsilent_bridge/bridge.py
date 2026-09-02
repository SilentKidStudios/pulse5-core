"""
Claude Engineer Bridge — programmatic, bounded, non-interactive dispatch of
Claude Code as an engineering worker.

Every job:
  1. gets a UUID job_id and an isolated working directory under jobs/<job_id>/workdir
  2. is classified by authority_policy.classify() BEFORE anything runs
  3. if FOUNDER_GATED and not pre-approved, is recorded and returned WITHOUT executing
  4. otherwise is invoked via `claude -p` with an explicit tool allowlist, a minimal
     environment, a timeout, and its cwd locked to the sandbox
  5. has its sandbox file tree snapshotted before/after to produce a changed-file report
  6. writes a structured result JSON to jobs/<job_id>/result.json
  7. is recorded in the audit trail regardless of outcome

No step here starts a background service, retries indefinitely, or grants itself
tools beyond what authority_policy.classify() approved.

CRASH-SAFE CHECKPOINTING / RESUME, closing the one asymmetry the OmniEngineer
job ledger left open: this bridge now checkpoints through job_ledger.py the
same way omniengineer_harness.py does — CREATED/AUTHORIZED/ROUTED/
SANDBOX_READY before the `claude -p` subprocess starts, EDITING/VALIDATING/
COMPLETED-or-FAILED around and after it. Recovery is necessarily coarser
than OmniEngineer's, because the Claude CLI's own internal tool-use loop is
opaque to this bridge (no per-iteration hook the way omniengineer_agent.py's
on_checkpoint gives us) — but the same two-category recovery policy still
applies cleanly:
  - SAFE_RESUME (interrupted before the subprocess started, i.e. state is
    CREATED/AUTHORIZED/ROUTED/SANDBOX_READY): nothing has run yet, so
    resume_job() just re-runs the job fresh, same job_id/sandbox,
    re-copying source_paths (safe — nothing has touched them yet).
  - RESTART_FROM_SANDBOX (interrupted during/after the subprocess, i.e.
    state is EDITING/VALIDATING/COMPLETED/FAILED but the ledger never
    reached a real terminal state): resume_job() re-invokes `claude -p`
    fresh, pointed at the SAME existing sandbox (files preserved, never
    wiped, source_paths NOT re-copied — that would overwrite any partial
    edits Claude already made), with the task text annotated that prior
    partial progress may exist and must be inspected first. Never replays
    anything; never blindly repeats a destructive action.

`tools`/`source_paths`/`validation_config` aren't part of job_ledger's
generic schema (that module is deliberately adapter-agnostic) — they're
stashed in the ledger record's `submit_params` field at creation time so
resume_job() can reconstruct the exact same call.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import audit
import job_ledger
import validation
from authority_policy import ApprovalState, RiskClass, classify
from context_staging import stage_context_source_paths
from evolution import independent_validation
from env_util import minimal_env
from job_ledger import JobState, RecoveryPolicy

try:
    import render_claude_relay
except ImportError:  # module is Pulse-only by design (see its own docstring) — a
    render_claude_relay = None  # node with a working local Claude install never needs it

BRIDGE_ROOT = Path(__file__).resolve().parent
JOBS_ROOT = BRIDGE_ROOT / "jobs"
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 1800  # hard ceiling; no job may run unbounded


def _resolve_claude_cli() -> tuple[str, bool]:
    """Real bug found and fixed (2026-08-19, Founder-authorized): the
    claude_code adapter has had a genuine 0% real production success rate
    for ~2 days — every real invocation from mrsilent-autonomous-cycle.
    service instantly raised FileNotFoundError. Root cause: the real
    `claude` binary lives at ~/.local/bin/claude, only on PATH in a LOGIN
    shell (added by profile/rc scripts); systemd services never source
    those, and the unit has no Environment=PATH= override. Narrowest fix
    (Founder's explicit preference over broadening the service's PATH):
    resolve the real absolute executable path ONCE here, so the actual
    service environment is never touched. shutil.which() is tried FIRST
    (so this keeps working unchanged if 'claude' is ever genuinely on
    PATH in some other environment) before falling back to the real,
    confirmed install location; if neither resolves, falls back to the
    bare command name so the exact same (now at least explicable, still
    honestly surfaced) FileNotFoundError path is preserved rather than
    crashing at import time or silently masking a genuinely different
    future problem (e.g. the CLI being uninstalled entirely).

    RENDER_CLAUDE_RELAY (Founder-authorized 2026-08-24): a second, honest
    return value — whether a LOCAL binary was actually found (True) versus
    the bare-name last resort (False) — lets the real invocation site below
    decide whether to route to render_claude_relay instead of guessing from
    the returned path string alone."""
    found = shutil.which("claude")
    if found:
        return found, True
    known_install = Path.home() / ".local" / "bin" / "claude"
    if known_install.exists():
        return str(known_install), True
    return "claude", False


CLAUDE_CLI_PATH, CLAUDE_CLI_FOUND_LOCALLY = _resolve_claude_cli()

RESUME_NOTE = (
    "\n\nNOTE: this sandbox may already contain partial progress from an "
    "interrupted prior attempt at this exact task. Inspect what's already "
    "there before writing or patching anything — do not blindly redo work "
    "that may already be done."
)


class JobStatus(str, Enum):
    REJECTED_POLICY = "rejected_policy"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"
    CLAUDE_UNAVAILABLE = "claude_unavailable"  # quota/rate-limit/auth — see _classify_claude_unavailable()


# Executive orchestration (Founder-authorized 2026-08-18): a non-zero exit
# is not automatically "the task failed" — the Claude CLI itself may be
# unable to run at all (quota exhausted, rate-limited, not authenticated).
# Without this distinction, "MR. SILENT survives Claude quota exhaustion
# and falls back appropriately" is impossible: a generic FAILED looks
# identical to "Claude tried and the work was bad," which must NOT be
# treated the same as "Claude was never actually able to try." Patterns are
# deliberately conservative (matched against real, documented Claude CLI/
# Anthropic API error language) — a false negative (missing a real
# unavailability and reporting FAILED instead) is far safer than a false
# positive (wrongly excusing a real implementation failure as "unavailable").
_CLAUDE_UNAVAILABLE_PATTERNS = (
    "usage limit", "rate limit", "rate_limit", "429", "quota",
    "please run /login", "not authenticated", "invalid api key",
    "overloaded", "529", "credit balance",
)


def _classify_claude_unavailable(exit_code: int | None, stderr: str, stdout: str) -> bool:
    if exit_code == 0:
        return False
    combined = f"{stderr}\n{stdout}".lower()
    return any(p in combined for p in _CLAUDE_UNAVAILABLE_PATTERNS)


@dataclass
class JobResult:
    job_id: str
    task: str
    requested_tools: list[str]
    granted_tools: list[str]
    risk_class: str
    approval_state: str
    status: str
    workdir: str
    started_at: str
    ended_at: str | None
    duration_s: float | None
    exit_code: int | None
    files_changed: dict[str, list[str]]
    claude_result: dict[str, Any] | None
    stdout_path: str | None
    stderr_path: str | None
    policy_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    validation: dict[str, Any] | None = None
    promotion_eligible: bool = False
    independent_validation: dict[str, Any] | None = None  # cross-organ parity (Founder-authorized 2026-08-18): evolution.independent_validation.recheck() result, if run


def _snapshot(root: Path) -> dict[str, str]:
    """relative_path -> sha256, for every file under root."""
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            snap[str(p.relative_to(root))] = h
    return snap


def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    return {"added": added, "modified": modified, "removed": removed}


def _duplicate_result(existing: job_ledger.LedgerRecord, task: str) -> JobResult:
    audit.record(
        job_id=existing.job_id, requested_by=existing.requested_by,
        task_summary=task[:200], tool_agent_selected="claude_code",
        permissions_granted=[], files_touched=existing.files_touched, commands_executed=[],
        test_results=None, risk_class=existing.risk_class or "n/a",
        approval_state=existing.approval_state or "n/a",
        final_disposition="duplicate_suppressed",
        lesson=f"suppressed: task already in-flight as job {existing.job_id} (state={existing.state})",
    )
    return JobResult(
        job_id=existing.job_id, task=task,
        requested_tools=(existing.submit_params or {}).get("tools", []), granted_tools=[],
        risk_class=existing.risk_class or "", approval_state=existing.approval_state or "",
        status="duplicate_suppressed", workdir=existing.sandbox_path,
        started_at=existing.created_at, ended_at=None, duration_s=None,
        exit_code=None, files_changed=existing.files_touched,
        claude_result=None, stdout_path=None, stderr_path=None,
        policy_reasons=[f"identical task already in-flight as job {existing.job_id} (state={existing.state}); this submission was not started"],
    )


def submit_job(
    task: str,
    *,
    requested_by: str = "unspecified",
    tools: list[str] | None = None,
    source_paths: list[str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    founder_approved: bool = False,
    model: str | None = None,
    validation_config: dict[str, Any] | None = None,
    on_job_created: Callable[[str], None] | None = None,
) -> JobResult:
    # IDEMPOTENCY: an identical task already actively in-flight is suppressed
    # BEFORE any job_id/sandbox/ledger is created — see
    # omniengineer_harness.submit_job()'s identical mechanism.
    existing = job_ledger.find_active_by_fingerprint(job_ledger.task_fingerprint(task))
    if existing is not None:
        return _duplicate_result(existing, task)

    job_id = str(uuid.uuid4())
    # Linked to any caller-side durable record (e.g. a self-evolution
    # proposal's implementation lineage) THE INSTANT the job_id exists —
    # see omniengineer_harness.submit_job()'s identical hook for the exact
    # gap this closes.
    if on_job_created:
        try:
            on_job_created(job_id)
        except Exception:  # noqa: BLE001 — a caller's linking hook must never break job execution
            pass
    workdir = JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    timeout_s = min(timeout_s, MAX_TIMEOUT_S)
    requested_tools = set(tools) if tools else {"Read", "Grep", "Glob"}

    # GOVERNED CANONICAL SOURCE STAGING REPAIR: context-staging exclusions
    # (manual_candidates/, backups, secrets, other jobs' workdirs, etc.) are
    # recorded at submission time for observability -- the actual filtering
    # is re-applied (cheap, pure, deterministic) inside _execute() right
    # before the copy step, same single-filtering-point design as
    # omniengineer_harness.submit_job_decomposed().
    _staged_preview, _excluded_preview = stage_context_source_paths([Path(p) for p in (source_paths or [])])
    job_ledger.create(
        job_id, task=task, requested_by=requested_by, sandbox_path=str(workdir), model=model,
        submit_params={
            "tools": sorted(requested_tools), "source_paths": source_paths or [],
            "validation_config": validation_config,
            "context_staging_excluded": _excluded_preview,
        },
    )
    return _execute(
        job_id, workdir, task, requested_by=requested_by, requested_tools=requested_tools,
        source_paths=[Path(p) for p in (source_paths or [])], timeout_s=timeout_s,
        founder_approved=founder_approved, model=model, validation_config=validation_config,
        copy_source_paths=True,
    )


def resume_job(job_id: str, *, requested_by: str = "recovery") -> JobResult:
    """Recovery entrypoint — see module docstring for the SAFE_RESUME vs
    RESTART_FROM_SANDBOX reasoning. Re-derives authority from scratch
    (classify() runs again inside _execute()); a job that needed Founder
    approval before interruption still needs it."""
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

    claimed, broke_stale_lock = job_ledger.claim_or_break_stale(job_id, owner=requested_by)
    if not claimed:
        status = job_ledger.lock_status(job_id)
        return _recovery_refusal(record, RecoveryPolicy.ESCALATE,
                                  f"another process already holds a live claim on this job (owner={status.get('owner')!r}, pid={status.get('pid')}); refusing to resume concurrently")

    try:
        note = f"resume attempt started (policy={policy.value})"
        if broke_stale_lock:
            note += " — broke a job-level lock left behind by a dead prior owner"
        job_ledger.checkpoint(job_id, record.state, resume_count=record.resume_count + 1, note=note)
        params = record.submit_params or {}
        task = record.task
        copy_source_paths = policy == RecoveryPolicy.SAFE_RESUME
        if policy == RecoveryPolicy.RESTART_FROM_SANDBOX:
            task = record.task + RESUME_NOTE
        return _execute(
            job_id, Path(record.sandbox_path), task, requested_by=requested_by,
            requested_tools=set(params.get("tools") or []),
            source_paths=[Path(p) for p in (params.get("source_paths") or [])],
            timeout_s=DEFAULT_TIMEOUT_S, founder_approved=False, model=record.model,
            validation_config=params.get("validation_config"),
            copy_source_paths=copy_source_paths, is_resume=True,
        )
    finally:
        job_ledger.release(job_id, owner=requested_by)


def _recovery_refusal(record: job_ledger.LedgerRecord, policy: RecoveryPolicy, reason: str) -> JobResult:
    return JobResult(
        job_id=record.job_id, task=record.task,
        requested_tools=(record.submit_params or {}).get("tools", []), granted_tools=[],
        risk_class=record.risk_class or "", approval_state=record.approval_state or "",
        status=f"resume_refused_{policy.value}", workdir=record.sandbox_path,
        started_at=record.created_at, ended_at=None, duration_s=None,
        exit_code=None, files_changed=record.files_touched,
        claude_result=None, stdout_path=None, stderr_path=None,
        policy_reasons=[reason],
    )


def _execute(
    job_id: str, workdir: Path, task: str, *, requested_by: str, requested_tools: set[str],
    source_paths: list[Path], timeout_s: int, founder_approved: bool, model: str | None,
    validation_config: dict[str, Any] | None, copy_source_paths: bool, is_resume: bool = False,
) -> JobResult:
    if not is_resume:
        job_ledger.claim(job_id, owner=requested_by)
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        decision = classify(
            task_description=task,
            requested_tools=requested_tools,
            sandbox_root=workdir,
            source_paths=source_paths,
            founder_approved=founder_approved,
        )
        job_ledger.checkpoint(job_id, JobState.AUTHORIZED, risk_class=decision.risk_class.value,
                               approval_state=decision.approval_state.value,
                               authority_state="granted" if decision.may_execute else "denied")

        if not decision.may_execute:
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="rejected_policy", error_class="authority")
            result = JobResult(
                job_id=job_id, task=task,
                requested_tools=sorted(requested_tools), granted_tools=[],
                risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
                status=JobStatus.REJECTED_POLICY.value, workdir=str(workdir),
                started_at=started_at, ended_at=started_at, duration_s=0.0,
                exit_code=None, files_changed={"added": [], "modified": [], "removed": []},
                claude_result=None, stdout_path=None, stderr_path=None,
                policy_reasons=decision.reasons,
            )
            _finalize(result, requested_by=requested_by, commands_executed=[])
            return result

        job_ledger.checkpoint(job_id, JobState.ROUTED, selected_engine="claude_code")

        # copy any explicitly-approved source paths into the sandbox for
        # editing — only on a fresh/SAFE_RESUME run; RESTART_FROM_SANDBOX
        # must never overwrite whatever Claude already partially edited.
        # GOVERNED CANONICAL SOURCE STAGING REPAIR: default-excludes noisy/
        # historical/secret-adjacent trees (manual_candidates/, backups,
        # other jobs' workdirs, etc.) even when authority-safe -- the same
        # shared filter omniengineer_harness.py's decomposed path already
        # uses. GATED_PATH_MARKERS above already rejected the WHOLE job for
        # genuinely protected paths; this is a separate, complementary
        # hygiene layer.
        if copy_source_paths:
            staged_source_paths, _excluded = stage_context_source_paths(source_paths)
            for src in staged_source_paths:
                dest = workdir / src.name
                if src.is_dir():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                elif src.is_file():
                    shutil.copy2(src, dest)

        job_ledger.checkpoint(job_id, JobState.SANDBOX_READY)

        before = _snapshot(workdir)

        cmd = [
            CLAUDE_CLI_PATH, "-p", task,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(sorted(decision.granted_tools)),
            "--add-dir", str(workdir),
            "--no-session-persistence",
        ]
        if model:
            cmd += ["--model", model]

        stdout_path = JOBS_ROOT / job_id / "stdout.txt"
        stderr_path = JOBS_ROOT / job_id / "stderr.txt"

        status = JobStatus.RUNNING
        exit_code: int | None = None
        claude_result: dict[str, Any] | None = None
        error: str | None = None
        t0 = time.monotonic()

        # RENDER_CLAUDE_RELAY (Founder-authorized 2026-08-24): pulse5-core-01
        # is this Studio's canonical MR. SILENT authority/system-of-record
        # (job_ledger, campaigns, autonomous-cycle all live here) but has no
        # local Claude Code install; Render-forge-01 is the attached compute
        # node where Claude Code is installed/authenticated. When no LOCAL
        # binary was found (CLAUDE_CLI_FOUND_LOCALLY is False), dispatch the
        # EXACT SAME cmd to Render instead of letting it fail immediately —
        # governed authority/gating already happened above, unchanged; this
        # only changes WHERE the already-authorized command physically runs.
        # A node that already has a working local Claude (e.g. Render itself)
        # never takes this branch — CLAUDE_CLI_FOUND_LOCALLY is True there,
        # so behavior is completely unchanged on that node.
        use_remote_relay = (
            not CLAUDE_CLI_FOUND_LOCALLY
            and render_claude_relay is not None
            and render_claude_relay.is_available()
        )
        note = "invoking claude -p"
        if use_remote_relay:
            note = "invoking claude -p (delegated to render-forge-01 via render_claude_relay)"
        job_ledger.checkpoint(job_id, JobState.EDITING, note=note,
                               worker_backend="claude_code@render-forge-01" if use_remote_relay else "claude_code")
        try:
            if use_remote_relay:
                try:
                    proc = render_claude_relay.run_claude_remote(
                        cmd, cwd=workdir, timeout_s=timeout_s, job_id=job_id,
                    )
                except render_claude_relay.RenderUnreachableError as e:
                    # Truthful governed failure -- never a false success, and
                    # distinct from a legitimate-but-failed Claude execution
                    # (which returns a real CompletedProcess instead of
                    # raising here) so this is never misclassified as a
                    # quota/auth unavailability by _classify_claude_unavailable().
                    raise FileNotFoundError(
                        f"claude CLI not found locally, and the Render delegation "
                        f"relay is unreachable: {e}"
                    ) from e
            else:
                proc = subprocess.run(
                    cmd, cwd=str(workdir), env=minimal_env(),
                    capture_output=True, text=True, timeout=timeout_s,
                )
            exit_code = proc.returncode
            stdout_path.write_text(proc.stdout)
            stderr_path.write_text(proc.stderr)
            if exit_code == 0:
                status = JobStatus.SUCCEEDED
                try:
                    claude_result = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    claude_result = None
            elif _classify_claude_unavailable(exit_code, proc.stderr, proc.stdout):
                status = JobStatus.CLAUDE_UNAVAILABLE
                error = "Claude CLI reported quota/rate-limit/auth unavailability — see stderr.txt"
            else:
                status = JobStatus.FAILED
        except subprocess.TimeoutExpired as e:
            status = JobStatus.TIMEOUT
            stdout_path.write_text(e.stdout or "" if isinstance(e.stdout, str) else "")
            stderr_path.write_text(e.stderr or "" if isinstance(e.stderr, str) else "")
            error = f"job exceeded timeout of {timeout_s}s"
        except FileNotFoundError as e:
            status = JobStatus.ERROR
            error = f"claude CLI not found: {e}"
        except Exception as e:  # noqa: BLE001 — surfaced in result JSON, not swallowed
            status = JobStatus.ERROR
            error = repr(e)

        duration_s = time.monotonic() - t0
        after = _snapshot(workdir)
        files_changed = _diff_snapshots(before, after)
        ended_at = datetime.now(timezone.utc).isoformat()

        # SANDBOX CHANGE -> AUTOMATIC VALIDATION -> PASS/FAIL. Only a successfully
        # *run* agent produces a sandbox worth validating; a failed/timed-out/errored
        # run may have left a partial or inconsistent sandbox, so it is never
        # promotion-eligible regardless of what validation would say.
        validation_result: dict[str, Any] | None = None
        independent_validation_result: dict[str, Any] | None = None
        promotion_eligible = False
        if status == JobStatus.SUCCEEDED:
            job_ledger.checkpoint(job_id, JobState.VALIDATING)
            vres = validation.validate(workdir, files_changed, config=validation_config)
            validation_result = vres.to_json()
            (JOBS_ROOT / job_id / "validation.json").write_text(json.dumps(validation_result, indent=2))
            promotion_eligible = vres.passed

            # Cross-organ independent-validation parity (Founder-authorized
            # 2026-08-18): the SAME genuinely-separate recheck already
            # proven for omni_engineer_v1 — a disagreement is NEVER
            # silently overridden, it forces promotion_eligible=False even
            # though validation.py said PASS.
            if promotion_eligible:
                ivres = independent_validation.recheck(workdir, files_changed, primary_passed=True)
                independent_validation_result = ivres.to_json()
                if ivres.ran and ivres.agrees_with_primary is False:
                    promotion_eligible = False

        final_disposition = status.value

        if status == JobStatus.SUCCEEDED and promotion_eligible:
            job_ledger.checkpoint(job_id, JobState.PROMOTION_CANDIDATE, validation_result=validation_result,
                                   independent_validation_result=independent_validation_result,
                                   promotion_eligible=True, files_touched=files_changed)
            job_ledger.checkpoint(job_id, JobState.COMPLETED, terminal_result="succeeded")
        elif status == JobStatus.SUCCEEDED and validation_result and not validation_result.get("passed"):
            final_disposition = "succeeded_validation_failed"
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="succeeded_validation_failed",
                                   error_class="validation", validation_result=validation_result)
        elif status == JobStatus.SUCCEEDED:
            # validation.py itself passed, but the independent recheck disagreed.
            final_disposition = "succeeded_validator_disagreement"
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="succeeded_validator_disagreement",
                                   error_class="validator_disagreement", validation_result=validation_result,
                                   independent_validation_result=independent_validation_result)
        else:
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result=status.value, error_class="infra")

        result = JobResult(
            job_id=job_id, task=task,
            requested_tools=sorted(requested_tools), granted_tools=sorted(decision.granted_tools),
            risk_class=decision.risk_class.value, approval_state=decision.approval_state.value,
            status=status.value, workdir=str(workdir),
            started_at=started_at, ended_at=ended_at, duration_s=duration_s,
            exit_code=exit_code, files_changed=files_changed,
            claude_result=claude_result,
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            policy_reasons=decision.reasons, error=error,
            validation=validation_result, promotion_eligible=promotion_eligible,
            independent_validation=independent_validation_result,
        )
        _finalize(result, requested_by=requested_by, commands_executed=[" ".join(cmd)], final_disposition=final_disposition)
        return result
    finally:
        if not is_resume:
            job_ledger.release(job_id, owner=requested_by)


def _finalize(result: JobResult, *, requested_by: str, commands_executed: list[str], final_disposition: str | None = None) -> None:
    result_path = JOBS_ROOT / result.job_id / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(asdict(result), indent=2))

    audit.record(
        job_id=result.job_id,
        requested_by=requested_by,
        task_summary=result.task[:200],
        tool_agent_selected="claude_code",
        permissions_granted=result.granted_tools,
        files_touched=result.files_changed,
        commands_executed=commands_executed,
        test_results=result.validation,
        risk_class=result.risk_class,
        approval_state=result.approval_state,
        final_disposition=final_disposition or result.status,
    )
