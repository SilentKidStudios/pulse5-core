#!/usr/bin/env python3
"""
Tests for bridge.py's job_ledger integration and resume_job() — closing the
asymmetry OmniEngineer's job ledger left open (bridge.py previously had no
checkpointing/resume at all).

These tests mock `subprocess.run` rather than invoking a real `claude -p`
process: unlike OmniEngineer (free, local, genuinely proven live throughout
this project), a nested `claude -p` call from within an active interactive
Claude Code session is a real, deliberate action this project has
consistently avoided spending in its test suite. The LEDGER/checkpoint/
resume LOGIC is what's under test here — not the Claude CLI itself, which
this bridge has always treated as an external, already-trusted dependency.
rejected_policy paths ARE tested for real (no subprocess call happens before
that check, so nothing is mocked or skipped there).

Same plain-script style as the other tests/test_*.py files.

Run: python3 tests/test_bridge_ledger.py
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge
import job_ledger
from job_ledger import JobState, RecoveryPolicy

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _backdate_heartbeat(job_id: str, seconds_ago: int) -> None:
    r = job_ledger.load(job_id)
    r.heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    job_ledger._atomic_write_json(job_ledger._path(job_id), job_ledger.asdict(r))


# ---- real (no subprocess involved) ------------------------------------

def test_rejected_policy_job_gets_a_terminal_ledger_record() -> None:
    result = bridge.submit_job(task="delete the stored credentials from the config directory", requested_by="test")
    check("a gated task is rejected_policy", result.status == "rejected_policy", result.status)
    record = job_ledger.load(result.job_id)
    check("the ledger record exists and is terminal/FAILED", record is not None and record.state == JobState.FAILED.value)
    check("the ledger records the authority error class", record.error_class == "authority", record.error_class)
    check("classify() correctly refuses to resume a rejected_policy job", job_ledger.classify(record) == RecoveryPolicy.TERMINAL_FAILURE)


def test_duplicate_submission_is_suppressed() -> None:
    task = f"synthetic duplicate test {uuid.uuid4()}"
    job_id = f"test-bridge-dup-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task=task, requested_by="test", sandbox_path=str(workdir))
    job_ledger.checkpoint(job_id, JobState.EDITING)  # non-terminal -> "active"

    try:
        result = bridge.submit_job(task=task, requested_by="test")
        check("an identical in-flight task is suppressed, not duplicated",
              result.status == "duplicate_suppressed", result.status)
        check("the suppressed result points at the existing job_id", result.job_id == job_id)
    finally:
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


def test_resume_refuses_terminal_and_founder_required_jobs() -> None:
    rejected = bridge.submit_job(task="modify the scorpio corner voice pipeline", requested_by="test")
    refusal = bridge.resume_job(rejected.job_id, requested_by="test")
    check("resume refuses a job already in a terminal state",
          refusal.status == f"resume_refused_{RecoveryPolicy.TERMINAL_FAILURE.value}", refusal.status)

    job_id = f"test-bridge-founder-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="synthetic founder-required test", requested_by="test", sandbox_path=str(workdir))
    job_ledger.checkpoint(job_id, JobState.AUTHORIZED, approval_state="pending_approval")
    _backdate_heartbeat(job_id, job_ledger.STALE_AFTER_S + 60)
    try:
        refusal2 = bridge.resume_job(job_id, requested_by="test")
        check("resume refuses a job that was awaiting Founder approval — never bypasses authority",
              refusal2.status == f"resume_refused_{RecoveryPolicy.FOUNDER_REQUIRED.value}", refusal2.status)
    finally:
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


def test_resume_refuses_when_another_process_holds_the_claim() -> None:
    job_id = f"test-bridge-claimed-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="synthetic claimed-job test", requested_by="test", sandbox_path=str(workdir))
    job_ledger.checkpoint(job_id, JobState.PLANNING if False else JobState.AUTHORIZED)  # any pre-mutation state
    _backdate_heartbeat(job_id, job_ledger.STALE_AFTER_S + 60)
    job_ledger.claim(job_id, owner="other_worker")
    try:
        refusal = bridge.resume_job(job_id, requested_by="test")
        check("resume refuses to race a concurrently-held claim",
              refusal.status == f"resume_refused_{RecoveryPolicy.ESCALATE.value}", refusal.status)
    finally:
        job_ledger.release(job_id, owner="other_worker")
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


# ---- mocked subprocess: checkpoint sequence + resume dispatch --------------

def test_successful_run_produces_the_full_checkpoint_sequence() -> None:
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # write a minimal valid file into the sandbox, as if Claude had edited it
        workdir = Path(kwargs["cwd"])
        (workdir / "hello.txt").write_text("hi")
        return _FakeCompletedProcess(0, stdout='{"result": "ok"}', stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="write hello.txt", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run

    check("the mocked run reaches status=succeeded", result.status == "succeeded", result.status)
    record = job_ledger.load(result.job_id)
    states = [h["state"] for h in record.history]
    check("the checkpoint history shows the full expected sequence",
          states == ["created", "authorized", "routed", "sandbox_ready", "editing", "validating", "promotion_candidate", "completed"],
          str(states))
    check("the ledger's selected_engine is correctly recorded as claude_code",
          record.selected_engine == "claude_code", record.selected_engine)
    check("the final terminal_result is 'succeeded'", record.terminal_result == "succeeded", record.terminal_result)


def test_resume_dispatches_restart_from_sandbox_with_note_and_no_source_recopy() -> None:
    """Simulates an interrupted job (real ledger record, synthetic state —
    never a killed process) at EDITING with a planted partial file, then
    resumes it with a mocked subprocess — proving RESTART_FROM_SANDBOX (a)
    annotates the task with the prior-progress note and (b) does NOT
    re-copy source_paths (which would overwrite in-progress edits)."""
    job_id = f"test-bridge-restart-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "partial.txt").write_text("PARTIAL")  # prior progress a crash would have left
    job_ledger.create(job_id, task="original synthetic task", requested_by="test", sandbox_path=str(workdir),
                       submit_params={"tools": ["Write"], "source_paths": [], "validation_config": None})
    job_ledger.checkpoint(job_id, JobState.EDITING, selected_engine="claude_code")
    _backdate_heartbeat(job_id, job_ledger.STALE_AFTER_S + 60)

    captured = {}
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        captured["task"] = cmd[2]  # ["claude", "-p", task, ...]
        return _FakeCompletedProcess(0, stdout='{"result": "ok"}', stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.resume_job(job_id, requested_by="test")
    finally:
        bridge.subprocess.run = original_run

    check("RESTART_FROM_SANDBOX resume reaches a determinate outcome", result.status == "succeeded", result.status)
    check("the resumed task text is annotated with the prior-progress note",
          "interrupted prior attempt" in captured.get("task", ""), captured.get("task", "")[:200])
    check("the pre-existing planted file survived (sandbox not wiped, not re-copied over)",
          (workdir / "partial.txt").read_text() == "PARTIAL")
    check("resume_count was incremented", job_ledger.load(job_id).resume_count == 1)


# ---- GOVERNED CANONICAL SOURCE STAGING REPAIR --------------------------

def test_authorized_source_staged_and_manual_candidates_excluded() -> None:
    """AUTHORIZED_CANONICAL_SOURCE_STAGED / UNAUTHORIZED_SOURCE_EXCLUDED /
    MANUAL_CANDIDATES_EXCLUDED, for the Claude Code engine specifically --
    proves bridge.py now reuses the exact same shared context_staging.py
    filter omniengineer_harness.py's decomposed path already uses, closing
    the gap this campaign targeted. Kept separate from the secrets case
    below: authority_policy.classify() rejects the WHOLE job outright when
    any source_path matches a GATED_PATH_MARKERS substring like "secrets"
    (a stronger, whole-job guarantee), so a path list combining an
    authorized real path with a secrets-marked path never reaches the
    per-path staging filter at all -- it never gets that far."""
    import shutil as _shutil
    import tempfile as _tempfile

    real_src_dir = Path(_tempfile.mkdtemp(prefix="bridge_ctx_staging_real_"))
    (real_src_dir / "helper.py").write_text("# real canonical helper\n")
    noise_root = Path(_tempfile.mkdtemp(prefix="bridge_ctx_staging_noise_"))
    manual_candidates_dir = noise_root / "manual_candidates"
    manual_candidates_dir.mkdir()
    (manual_candidates_dir / "withdrawn.py").write_text("# must never be staged\n")

    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout='{"result": "ok"}', stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(
            task="synthetic test: authorized source staging", requested_by="test", tools=["Read", "Write"],
            source_paths=[str(real_src_dir), str(manual_candidates_dir)],
        )
    finally:
        bridge.subprocess.run = original_run
        _shutil.rmtree(real_src_dir, ignore_errors=True)
        _shutil.rmtree(noise_root, ignore_errors=True)

    workdir = Path(result.workdir)
    staged_files = list(workdir.rglob("*"))
    check("AUTHORIZED_CANONICAL_SOURCE_STAGED: the real, non-excluded directory was copied into the sandbox",
          any(f.name == "helper.py" for f in staged_files), [str(f) for f in staged_files])
    check("MANUAL_CANDIDATES_EXCLUDED: withdrawn.py never reached the sandbox",
          not any(f.name == "withdrawn.py" for f in staged_files), [str(f) for f in staged_files])

    record = job_ledger.load(result.job_id)
    excluded = (record.submit_params or {}).get("context_staging_excluded", [])
    check("the exclusion is durably recorded with its reason, not silently dropped",
          len(excluded) == 1, excluded)


def test_secrets_source_path_rejects_the_whole_job() -> None:
    """SECRETS_EXCLUDED, proven via the stronger whole-job authority-level
    rejection: authority_policy.classify() runs on the FULL, unfiltered
    source_paths list BEFORE any staging/copying occurs, so a secrets-marked
    path never merely gets silently dropped -- it takes the entire job down
    as rejected_policy, which is a stronger guarantee than per-path
    exclusion. Nothing is ever staged, including any otherwise-authorized
    path submitted alongside it."""
    import shutil as _shutil
    import tempfile as _tempfile

    noise_root = Path(_tempfile.mkdtemp(prefix="bridge_ctx_staging_secrets_"))
    secrets_dir = noise_root / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "key.txt").write_text("must never be staged\n")

    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout='{"result": "ok"}', stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(
            task="synthetic test: secrets path rejected", requested_by="test", tools=["Read", "Write"],
            source_paths=[str(secrets_dir)],
        )
    finally:
        bridge.subprocess.run = original_run
        _shutil.rmtree(noise_root, ignore_errors=True)

    check("SECRETS_EXCLUDED: a secrets-marked source_path rejects the whole job at the authority level",
          result.status == "rejected_policy", result.status)


def test_sandbox_write_only_no_direct_canonical_write() -> None:
    """SANDBOX_WRITE_ONLY / CANONICAL_DIRECT_WRITE_BLOCKED: the mocked
    'engineer' writes into its own sandbox copy; the original real source
    file on disk must be completely untouched."""
    import tempfile as _tempfile
    import shutil as _shutil

    real_src_dir = Path(_tempfile.mkdtemp(prefix="bridge_sandbox_write_only_"))
    original_path = real_src_dir / "canonical.py"
    original_path.write_text("ORIGINAL CONTENT\n")

    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # simulate the engineer editing its OWN sandbox copy, never the real path
        workdir = Path(kwargs["cwd"])
        sandbox_copy = workdir / real_src_dir.name / "canonical.py"
        if sandbox_copy.exists():
            sandbox_copy.write_text("EDITED IN SANDBOX ONLY\n")
        return _FakeCompletedProcess(0, stdout='{"result": "ok"}', stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(
            task="synthetic test: sandbox-only write", requested_by="test", tools=["Read", "Write"],
            source_paths=[str(real_src_dir)],
        )
    finally:
        bridge.subprocess.run = original_run

    check("the REAL canonical file on disk is completely untouched",
          original_path.read_text() == "ORIGINAL CONTENT\n", original_path.read_text() if original_path.exists() else "MISSING")
    check("the job reached a determinate outcome", result.status in ("succeeded", "succeeded_validation_failed"), result.status)
    _shutil.rmtree(real_src_dir, ignore_errors=True)


if __name__ == "__main__":
    test_rejected_policy_job_gets_a_terminal_ledger_record()
    test_duplicate_submission_is_suppressed()
    test_resume_refuses_terminal_and_founder_required_jobs()
    test_resume_refuses_when_another_process_holds_the_claim()
    test_successful_run_produces_the_full_checkpoint_sequence()
    test_resume_dispatches_restart_from_sandbox_with_note_and_no_source_recopy()
    test_authorized_source_staged_and_manual_candidates_excluded()
    test_secrets_source_path_rejects_the_whole_job()
    test_sandbox_write_only_no_direct_canonical_write()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
