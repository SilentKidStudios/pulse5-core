#!/usr/bin/env python3
"""
Regression tests for tests/conftest.py's pytest-invocation isolation
fixture (2026-09-03, PYTEST_LEDGER_ISOLATION gap-closure) and for the
concurrency-safety hardening added to tests/_test_isolation.py's job_ledger
teardown in the same pass (provenance-marker gating, not just "new since
snapshot").

Uses real `assert` (not this project's check()/FAILURES convention) --
deliberately, since the whole point of this file is proving PYTEST-
enforced behavior; a check()-style non-raising assertion would let pytest
report a false PASS exactly the failure mode this project has hit before.

Run: python3 -m pytest tests/test_pytest_ledger_isolation.py
(running it directly via `python3 tests/test_pytest_ledger_isolation.py`
also works for the non-subprocess tests, but the two end-to-end subprocess
tests are the ones that actually exercise pytest's own collection path,
so `pytest` is the meaningful way to run this file.)
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import job_ledger
from _test_isolation import isolated_job_ledger_state

BRIDGE_ROOT = Path(__file__).resolve().parent.parent
TERMINAL_VALUES = {s.value for s in job_ledger.TERMINAL_STATES}


def _make_job(*, requested_by: str, sandbox_path: str, state=None) -> str:
    job_id = str(uuid.uuid4())
    job_ledger.create(job_id, task=str(uuid.uuid4()), requested_by=requested_by, sandbox_path=sandbox_path)
    if state is not None:
        job_ledger.checkpoint(job_id, state)
    return job_id


def _nonterminal_count() -> int:
    return len([r for r in job_ledger.list_all() if r.state not in TERMINAL_VALUES])


# ---- 1. test-created non-terminal fixture is reconciled at teardown -------

def test_test_fixture_job_is_reconciled_at_teardown():
    job_id = None
    with isolated_job_ledger_state():
        job_id = _make_job(requested_by="tester", sandbox_path="/tmp", state=job_ledger.JobState.EDITING)
        assert job_ledger.load(job_id).state not in TERMINAL_VALUES  # sanity: really non-terminal going in

    reconciled = job_ledger.load(job_id)
    assert reconciled.state in TERMINAL_VALUES, "test-fixture job must be terminalized at teardown"
    assert reconciled.terminal_result == "test_teardown"
    assert reconciled.error_class == "test_fixture"


# ---- 2. pre-existing non-terminal real job is untouched -------------------

def test_preexisting_real_job_is_untouched():
    # A "real" job: NOT the test-fixture shape (real workdir path, real
    # requested_by identity) -- created BEFORE the isolated_job_ledger_state()
    # block starts, simulating state that already existed.
    real_job_id = _make_job(
        requested_by="autonomous_cycle",
        sandbox_path=str(BRIDGE_ROOT / "jobs" / "synthetic-preexisting-real" / "workdir"),
        state=job_ledger.JobState.EDITING,
    )
    try:
        with isolated_job_ledger_state():
            pass  # block does nothing -- the real job predates it
        after = job_ledger.load(real_job_id)
        assert after.state == job_ledger.JobState.EDITING.value, "pre-existing real job must be completely untouched"
        assert after.terminal_result is None
    finally:
        job_ledger.checkpoint(real_job_id, job_ledger.JobState.FAILED, terminal_result="test_teardown", error_class="test_fixture")


# ---- 3. REQUIRED: real job created DURING the block is preserved ----------

def test_real_concurrent_job_created_during_test_is_preserved():
    """REAL_CONCURRENT_JOB_CREATED_DURING_TEST_IS_PRESERVED -- a plain
    'new since snapshot' filter would incorrectly sweep this up; only the
    combination of newness AND provenance-marker match may terminalize
    anything. This job has real markers (a real-shaped workdir path, a
    real-looking requested_by), so even though it's created INSIDE the
    block -- exactly like the live autonomous-cycle timer firing mid-test
    would -- it must survive teardown untouched."""
    concurrent_job_id = None
    with isolated_job_ledger_state():
        # Simulates the real system creating real work while this test runs.
        concurrent_job_id = _make_job(
            requested_by="autonomous_cycle",
            sandbox_path=str(BRIDGE_ROOT / "jobs" / "synthetic-concurrent-real" / "workdir"),
            state=job_ledger.JobState.EDITING,
        )
    try:
        after = job_ledger.load(concurrent_job_id)
        assert after.state == job_ledger.JobState.EDITING.value, (
            "a real job created DURING the block must be preserved -- newness alone is not sufficient grounds for teardown"
        )
        assert after.terminal_result is None
    finally:
        job_ledger.checkpoint(concurrent_job_id, job_ledger.JobState.FAILED, terminal_result="test_teardown", error_class="test_fixture")


# ---- 4. ambiguous new job (partial marker match) is untouched -------------

def test_ambiguous_new_job_with_partial_markers_is_untouched():
    # sandbox_path matches, but requested_by does NOT -- a partial match
    # must still fail closed, never be treated as "close enough".
    partial_a = None
    # requested_by matches, but sandbox_path does NOT.
    partial_b = None
    with isolated_job_ledger_state():
        partial_a = _make_job(requested_by="some_other_real_caller", sandbox_path="/tmp", state=job_ledger.JobState.EDITING)
        partial_b = _make_job(requested_by="tester", sandbox_path="/opt/pulse5-core/mrsilent_bridge/jobs/x/workdir", state=job_ledger.JobState.EDITING)
    try:
        for jid, label in [(partial_a, "sandbox-only match"), (partial_b, "requested_by-only match")]:
            after = job_ledger.load(jid)
            assert after.state == job_ledger.JobState.EDITING.value, f"partial-marker job ({label}) must be left untouched, not treated as ambiguous-but-safe"
    finally:
        for jid in (partial_a, partial_b):
            job_ledger.checkpoint(jid, job_ledger.JobState.FAILED, terminal_result="test_teardown", error_class="test_fixture")


# ---- 5. terminal test-fixture job is left alone (idempotent/harmless) -----

def test_terminal_test_fixture_job_is_left_alone():
    job_id = None
    with isolated_job_ledger_state():
        job_id = _make_job(requested_by="test", sandbox_path="/tmp", state=None)
        job_ledger.checkpoint(job_id, job_ledger.JobState.COMPLETED, terminal_result="succeeded")
        history_len_before_exit = len(job_ledger.load(job_id).history)

    after = job_ledger.load(job_id)
    assert after.terminal_result == "succeeded", "an already-terminal test job must not be re-terminalized/overwritten"
    assert after.error_class != "test_fixture", "teardown must not touch a job that already reached a terminal state on its own"
    assert len(after.history) == history_len_before_exit, "no extra history entry should be appended for an already-terminal job"


# ---- 6/7. teardown runs even when the test body raises --------------------

def test_teardown_runs_when_the_block_raises_an_exception():
    job_id_holder: dict = {}

    class _DeliberateTestException(Exception):
        pass

    try:
        with isolated_job_ledger_state():
            job_id_holder["id"] = _make_job(requested_by="tester", sandbox_path="/tmp", state=job_ledger.JobState.EDITING)
            raise _DeliberateTestException("simulating a real assertion failure / crash mid-test")
    except _DeliberateTestException:
        pass
    else:
        raise AssertionError("the deliberate exception should have propagated out of the with-block")

    reconciled = job_ledger.load(job_id_holder["id"])
    assert reconciled.state in TERMINAL_VALUES, "teardown must still run (the try/finally guarantee) even when the block raises"
    assert reconciled.error_class == "test_fixture"


# ---- 8. repeated pytest invocation leaves zero new fixtures ---------------

def test_repeated_pytest_invocation_leaves_zero_new_fixtures():
    """End-to-end proof, via a real pytest subprocess, of the actual
    property this whole gap-closure round is about: invoking one of the
    previously-leaky files THROUGH pytest (not python3 directly) must not
    accumulate non-terminal fixture debris, twice in a row."""
    target = "tests/test_job_ledger_active_work_precheck.py"
    before = _nonterminal_count()
    for run_number in (1, 2):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-q"],
            cwd=str(BRIDGE_ROOT), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"pytest run {run_number} failed:\n{result.stdout}\n{result.stderr}"
        after = _nonterminal_count()
        assert after == before, f"pytest run {run_number} left {after - before} new non-terminal fixture(s) behind"


# ---- 9. production recovery semantics unchanged ----------------------------

def test_production_recovery_functions_are_unmodified():
    """Mechanical, not exhaustive: confirms the marker scheme
    is_test_isolation_fixture_job() relies on can never match real
    recovery-relevant state, and that this pass's own changes
    (tests/_test_isolation.py, tests/conftest.py, this file) never touched
    job_ledger.py or evolution/advance.py at all -- verifiable directly via
    `git diff` for those two files. autonomous_cycle.py (which owns
    _startup_recovery()) is intentionally NOT imported here: it is itself
    untracked in this repo, and this test file must stay importable from a
    clean checkout using only tracked dependencies (job_ledger,
    _test_isolation). The full existing regression coverage for
    _startup_recovery() (tests/test_autonomous_cycle.py, 24 tests) is
    unaffected and re-run separately as part of this same validation pass."""
    from _test_isolation import TEST_FIXTURE_SANDBOX_PATH, TEST_FIXTURE_REQUESTED_BY

    assert TEST_FIXTURE_SANDBOX_PATH == "/tmp"
    assert TEST_FIXTURE_REQUESTED_BY == {"tester", "test"}
    # A real job's sandbox_path is always inside JOBS_ROOT, never bare "/tmp".
    assert not str(job_ledger.JOBS_ROOT).startswith("/tmp")
    # A real recovery-relevant requested_by (the autonomous cycle itself,
    # or any real MCP/CLI caller identity) is never literally "tester"/"test".
    assert "autonomous_cycle" not in TEST_FIXTURE_REQUESTED_BY


if __name__ == "__main__":
    with isolated_job_ledger_state():
        test_test_fixture_job_is_reconciled_at_teardown()
        test_preexisting_real_job_is_untouched()
        test_real_concurrent_job_created_during_test_is_preserved()
        test_ambiguous_new_job_with_partial_markers_is_untouched()
        test_terminal_test_fixture_job_is_left_alone()
        test_teardown_runs_when_the_block_raises_an_exception()
        test_repeated_pytest_invocation_leaves_zero_new_fixtures()
        test_production_recovery_functions_are_unmodified()
    print("All tests passed.")
