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


# ---- FILES_TOUCHED / RESULT_PARSE PERSISTENCE REPAIR (2026-09-09) ------
#
# Real, live incident: jobs d35ef27b and 11b56dab (Founder Top-10 ranks
# 8/9, delegated to render-forge-01) genuinely produced real file changes
# -- confirmed directly against the physical sandbox and this function's
# own in-memory files_changed -- but only the PROMOTION_CANDIDATE branch
# of submit_job() ever persisted files_touched to job_ledger; every other
# terminal branch silently left it empty. That made an ordinary
# validation-contract mismatch indistinguishable, in the durable ledger,
# from a render-forge-01 infrastructure failure that touched nothing at
# all. A second, related gap: claude_result was only ever parsed when
# exit_code==0, so a real, well-formed JSON result with is_error=true
# (e.g. a Bash-tool permission refusal under headless -p) was silently
# discarded even though it was sitting, fully parsed, right there in
# stdout.

import validation as _bridge_validation
from evolution import independent_validation as _bridge_independent_validation


def test_files_touched_persisted_on_validation_failure() -> None:
    """REGRESSION for the exact real incident: real files are written, but
    validation.py fails (no required test file) -- files_touched must
    still durably reflect the real change, not silently show empty."""
    original_run = subprocess.run
    original_validate = _bridge_validation.validate

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "ASSESSMENT.md").write_text("real content")
        return _FakeCompletedProcess(0, stdout='{"result": "done", "is_error": false}', stderr="")

    _bridge_validation.validate = lambda *a, **k: type(
        "V", (), {"passed": False, "to_json": lambda self: {"passed": False, "checks": []}})()
    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: report-only deliverable", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run
        _bridge_validation.validate = original_validate

    record = job_ledger.load(result.job_id)
    check("ledger terminal_result reaches succeeded_validation_failed", record.terminal_result == "succeeded_validation_failed", record.terminal_result)
    check("JobResult.files_changed shows the real added file", "ASSESSMENT.md" in result.files_changed.get("added", []), result.files_changed)
    check("REGRESSION: durable ledger files_touched is no longer dropped on a validation-failure branch",
          "ASSESSMENT.md" in (record.files_touched or {}).get("added", []), record.files_touched)


def test_files_touched_persisted_on_validator_disagreement() -> None:
    original_run = subprocess.run
    original_validate = _bridge_validation.validate
    original_recheck = _bridge_independent_validation.recheck

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "output.txt").write_text("real content")
        return _FakeCompletedProcess(0, stdout='{"result": "done"}', stderr="")

    _bridge_validation.validate = lambda *a, **k: type(
        "V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()
    _bridge_independent_validation.recheck = lambda *a, **k: type(
        "IV", (), {"ran": True, "agrees_with_primary": False,
                   "to_json": lambda self: {"ran": True, "agrees_with_primary": False}})()
    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: validator disagreement persistence", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run
        _bridge_validation.validate = original_validate
        _bridge_independent_validation.recheck = original_recheck

    record = job_ledger.load(result.job_id)
    check("ledger terminal_result reaches succeeded_validator_disagreement", record.terminal_result == "succeeded_validator_disagreement", record.terminal_result)
    check("REGRESSION: files_touched is persisted even on a validator disagreement",
          "output.txt" in (record.files_touched or {}).get("added", []), record.files_touched)
    check("ledger error_class stays 'validator_disagreement', unchanged by this repair",
          record.error_class == "validator_disagreement", record.error_class)


def test_files_touched_persisted_on_generic_infra_failure() -> None:
    """A genuine nonzero exit / infra-style failure -- files_touched must
    still reflect any real partial edits, and error_class stays 'infra'
    (unchanged) so job_ledger.classify()'s existing recovery policy is
    completely undisturbed by this repair."""
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "partial_edit.txt").write_text("partial")
        return _FakeCompletedProcess(1, stdout="", stderr="some real stderr output")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: generic nonzero-exit failure", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run

    check("job reaches a failed terminal status", result.status == "failed", result.status)
    record = job_ledger.load(result.job_id)
    check("REGRESSION: files_touched reflects the real partial edit even on generic failure",
          "partial_edit.txt" in (record.files_touched or {}).get("added", []), record.files_touched)
    check("error_class stays 'infra' -- unchanged, so job_ledger.classify()'s existing recovery policy is untouched",
          record.error_class == "infra", record.error_class)
    check("stderr is preserved on disk", Path(result.stderr_path).read_text() == "some real stderr output", result.stderr_path)


def test_nonzero_exit_with_valid_json_result_is_now_parsed() -> None:
    """REGRESSION for the second real gap: a genuine claude CLI refusal
    (is_error=true, a real Bash-tool permission denial) with a nonzero
    exit code used to be discarded entirely (claude_result stayed None).
    It must now be parsed and available on the JobResult, without
    changing `status` (still driven by exit_code exactly as before)."""
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(
            1, stdout='{"is_error": true, "stop_reason": "refusal", "result": "need Bash approval"}', stderr="",
        )

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: refusal with valid JSON on nonzero exit", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run

    check("status classification is unaffected by this repair (still driven by exit_code alone)",
          result.status == "failed", result.status)
    check("REGRESSION: claude_result is no longer silently discarded on a nonzero exit",
          result.claude_result is not None and result.claude_result.get("stop_reason") == "refusal", result.claude_result)


def test_empty_stdout_zero_exit_is_never_silently_marked_a_content_success() -> None:
    """A real, legitimate zero-exit run that touched nothing must still
    reach a determinate, honest outcome -- never fabricated into a false
    'this produced something' result."""
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout="", stderr="")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: empty stdout zero exit", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run

    check("claude_result is None for genuinely empty stdout (not a JSON parse crash)", result.claude_result is None, result.claude_result)
    check("no real file changes recorded", not any(result.files_changed.get(k) for k in ("added", "modified", "removed")), result.files_changed)
    record = job_ledger.load(result.job_id)
    check("ledger files_touched correctly stays empty (truthfully reflects zero real changes)",
          not any((record.files_touched or {}).get(k) for k in ("added", "modified", "removed")), record.files_touched)


def test_timeout_persists_empty_files_touched_correctly() -> None:
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: timeout persistence", requested_by="test", tools=["Write"], timeout_s=1)
    finally:
        bridge.subprocess.run = original_run

    check("job reaches timeout status", result.status == "timeout", result.status)
    record = job_ledger.load(result.job_id)
    check("ledger files_touched correctly empty on a timeout with no sandbox changes",
          not any((record.files_touched or {}).get(k) for k in ("added", "modified", "removed")), record.files_touched)
    check("error_class stays 'infra' -- unchanged classification for a timeout",
          record.error_class == "infra", record.error_class)


def test_transport_failure_never_treated_as_success() -> None:
    """A real render_claude_relay.RenderUnreachableError (simulated here
    via the exact same re-raise bridge.py itself performs) must reach a
    real ERROR status, never success, regardless of this repair."""
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude CLI not found locally, and the Render delegation relay is unreachable: simulated transport failure")

    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: transport failure", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run

    check("a transport failure reaches ERROR status, never success", result.status == "error", result.status)
    record = job_ledger.load(result.job_id)
    check("files_touched correctly empty -- no sandbox changes could have occurred", record.files_touched == {"added": [], "modified": [], "removed": []}, record.files_touched)


def test_files_touched_persistence_survives_reload() -> None:
    original_run = subprocess.run
    original_validate = _bridge_validation.validate

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "reload_check.txt").write_text("real content")
        return _FakeCompletedProcess(0, stdout='{"result": "done"}', stderr="")

    _bridge_validation.validate = lambda *a, **k: type(
        "V", (), {"passed": False, "to_json": lambda self: {"passed": False, "checks": []}})()
    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: reload persistence", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run
        _bridge_validation.validate = original_validate

    first = job_ledger.load(result.job_id)
    second = job_ledger.load(result.job_id)
    check("repeated load() calls return identical files_touched", first.files_touched == second.files_touched,
          (first.files_touched, second.files_touched))
    check("a fresh load() sees the real persisted file", "reload_check.txt" in (second.files_touched or {}).get("added", []), second.files_touched)


def test_retryable_classification_unchanged_by_this_repair() -> None:
    """classify_ledger_failure_transience() must classify a validation
    failure identically before and after this repair -- the fix only
    persists files_touched, it never touches error_class/terminal_result
    semantics that retry classification depends on."""
    original_run = subprocess.run
    original_validate = _bridge_validation.validate

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "x.txt").write_text("x")
        return _FakeCompletedProcess(0, stdout='{"result": "done"}', stderr="")

    _bridge_validation.validate = lambda *a, **k: type(
        "V", (), {"passed": False, "to_json": lambda self: {"passed": False, "checks": []}})()
    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: classification unchanged", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run
        _bridge_validation.validate = original_validate

    record = job_ledger.load(result.job_id)
    label = job_ledger.classify_ledger_failure_transience(record.error_class, record.terminal_result)
    check("REGRESSION-FREE: validation failure still classifies as MODEL_FAILURE, exactly as before this repair",
          label == "MODEL_FAILURE", label)


def test_no_silent_success_despite_files_touched_now_populated() -> None:
    """The core safety invariant: persisting real files_touched on a
    failure branch must NEVER let that job reach promotion_candidate --
    only the dedicated success branch ever sets promotion_eligible=True."""
    original_run = subprocess.run
    original_validate = _bridge_validation.validate

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "y.txt").write_text("y")
        return _FakeCompletedProcess(0, stdout='{"result": "done"}', stderr="")

    _bridge_validation.validate = lambda *a, **k: type(
        "V", (), {"passed": False, "to_json": lambda self: {"passed": False, "checks": []}})()
    bridge.subprocess.run = fake_run
    try:
        result = bridge.submit_job(task="synthetic test: no silent success", requested_by="test", tools=["Write"])
    finally:
        bridge.subprocess.run = original_run
        _bridge_validation.validate = original_validate

    check("promotion_eligible stays False despite real files_touched being persisted", result.promotion_eligible is False, result.promotion_eligible)
    record = job_ledger.load(result.job_id)
    check("ledger state never reaches promotion_candidate/completed for a real validation failure",
          record.state == JobState.FAILED.value, record.state)


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
    test_files_touched_persisted_on_validation_failure()
    test_files_touched_persisted_on_validator_disagreement()
    test_files_touched_persisted_on_generic_infra_failure()
    test_nonzero_exit_with_valid_json_result_is_now_parsed()
    test_empty_stdout_zero_exit_is_never_silently_marked_a_content_success()
    test_timeout_persists_empty_files_touched_correctly()
    test_transport_failure_never_treated_as_success()
    test_files_touched_persistence_survives_reload()
    test_retryable_classification_unchanged_by_this_repair()
    test_no_silent_success_despite_files_touched_now_populated()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
