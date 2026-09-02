"""Tests for recover_work: the governed stale-work recovery/reconciliation
MCP tool added on top of the EXISTING, already-tested
evolution.advance.advance_one() pipeline (job_ledger.is_stale/classify,
bridge.resume_job/omniengineer_harness.resume_job, the proposal-scoped
job_ledger.claim/release lock). recover_work adds no new staleness/
reconciliation logic of its own -- these tests prove the NEW wrapper
correctly reaches, and does not weaken, that existing governance, using
isolated synthetic proposals/jobs (TEST-ONLY, cleaned up afterward) rather
than any real live job.

bridge.resume_job is monkeypatched only in the one test that must reach the
"stale, resumable" dispatch branch, to avoid spawning a real `claude -p`
subprocess -- same reasoning mrsilent_bridge/tests/test_bridge_ledger.py
gives for mocking subprocess.run rather than bridge.resume_job's own logic.
Every other test exercises the real, unmocked code path.

Run: python3 -m pytest services/th3_mcp/tests/test_recover_work.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/opt/pulse5-core")
sys.path.insert(0, str(ROOT / "services" / "th3_mcp"))
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))

import pytest
import tools as T
import mcp_core
import job_ledger
from job_ledger import JobState
from evolution import proposal as proposal_mod
from evolution import advance as advance_mod


@pytest.fixture
def cleanup_proposals():
    created: list[str] = []
    yield created
    for work_id in created:
        try:
            p = proposal_mod.load(work_id)
            if p.status not in proposal_mod.CLOSED_STATUSES:
                proposal_mod.advance(work_id, proposal_mod.ProposalStatus.REJECTED, note="test cleanup")
        except Exception:
            pass


@pytest.fixture
def cleanup_jobs():
    created: list[str] = []
    yield created
    for job_id in created:
        try:
            job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")
        except Exception:
            pass
        try:
            job_ledger.release(job_id, owner="test")
        except Exception:
            pass


def _make_proposal(cleanup_proposals, *, risk_score: str = "low", status: str | None = None) -> str:
    p = proposal_mod.create(
        observed_weakness="TEST-ONLY: recover_work synthetic fixture",
        proposed_upgrade="TEST-ONLY: no real upgrade, safe to reject",
        risk_score=risk_score,
    )
    cleanup_proposals.append(p.proposal_id)
    if status is not None:
        proposal_mod.advance(p.proposal_id, status, note="test setup")
    return p.proposal_id


def _make_job(cleanup_jobs, *, state: str, selected_engine: str = "claude_code",
              stale_seconds_ago: int | None = None, resume_count: int = 0) -> str:
    job_id = str(uuid.uuid4())
    cleanup_jobs.append(job_id)
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="TEST-ONLY: recover_work synthetic job", requested_by="test",
                       sandbox_path=str(workdir), submit_params={"tools": [], "source_paths": [], "validation_config": None})
    job_ledger.checkpoint(job_id, state, selected_engine=selected_engine, resume_count=resume_count)
    if stale_seconds_ago is not None:
        r = job_ledger.load(job_id)
        r.heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds_ago)).isoformat()
        job_ledger._atomic_write_json(job_ledger._path(job_id), job_ledger.asdict(r))
    return job_id


# ---- not found -------------------------------------------------------------

def test_unknown_work_id_reports_not_found():
    result = T.recover_work("does-not-exist-" + uuid.uuid4().hex)
    assert result["found"] is False


# ---- AMBIGUOUS_STATE_FAIL_CLOSED / PROTECTED_GATES_PRESERVED ---------------

def test_founder_gated_proposal_is_never_recovered(cleanup_proposals, cleanup_jobs):
    work_id = _make_proposal(cleanup_proposals, risk_score="founder_gated")
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, stale_seconds_ago=2000)
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")

    result = T.recover_work(work_id)
    assert result["mutated"] is False
    assert result["decision"]["blocked_reason"] is not None
    assert result["resulting_status"] == result["prior_status"]


def test_medium_risk_proposal_is_never_recovered(cleanup_proposals):
    work_id = _make_proposal(cleanup_proposals, risk_score="medium")
    result = T.recover_work(work_id)
    assert result["mutated"] is False


def test_recover_work_schema_has_no_founder_approved_override():
    resp = mcp_core.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    spec = next(t for t in resp["result"]["tools"] if t["name"] == "recover_work")
    assert "founder_approved" not in spec["inputSchema"]["properties"]
    assert spec["inputSchema"]["required"] == ["work_id"]


# ---- TERMINAL_JOB_UNCHANGED -------------------------------------------------

def test_already_terminal_proposal_is_left_unchanged(cleanup_proposals):
    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.REJECTED)
    result = T.recover_work(work_id)
    assert result["mutated"] is False
    assert result["resulting_status"] == proposal_mod.ProposalStatus.REJECTED


# ---- LIVE_OWNER_NOT_RECOVERED ------------------------------------------------

def test_live_fresh_job_is_not_touched(cleanup_proposals, cleanup_jobs, monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("bridge.resume_job must not be called for a live, non-stale job")
    monkeypatch.setattr(advance_mod.bridge, "resume_job", _must_not_be_called)

    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, stale_seconds_ago=None)  # fresh heartbeat
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")

    result = T.recover_work(work_id)
    assert result["prior_implementation_job"]["is_stale"] is False
    assert result["mutated"] is False
    assert result["decision"]["blocked_reason"] == "prior_job_still_active"
    assert any("still appears active and not stale" in s.get("detail", "") for s in result["decision"]["stages"])


# ---- STALE_OWNER_DETECTION / DETERMINISTIC_RECONCILIATION / DURABLE_RECEIPT -

def test_stale_resumable_job_is_reconciled_via_the_correct_engine(cleanup_proposals, cleanup_jobs, monkeypatch):
    calls = []

    def _fake_resume_job(job_id, *, requested_by="recovery"):
        calls.append((job_id, requested_by))
        # promotion_eligible=False deliberately: this test proves the
        # dispatch-to-the-correct-engine reconciliation itself (the new
        # behavior under test), not the pre-existing, separately-tested
        # validation/canary/promotion pipeline downstream of a real job.
        return SimpleNamespace(status="succeeded", job_id=job_id, promotion_eligible=False,
                                files_changed={"added": [], "modified": [], "removed": []})

    monkeypatch.setattr(advance_mod.bridge, "resume_job", _fake_resume_job)

    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, selected_engine="claude_code", stale_seconds_ago=2000)
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")

    # evidence captured BEFORE mutation
    result = T.recover_work(work_id, reason="TEST-ONLY: reconcile a synthetic stale job")
    assert result["found"] is True
    assert result["prior_implementation_job"]["job_id"] == job_id
    assert result["prior_implementation_job"]["is_stale"] is True
    assert result["prior_implementation_job"]["selected_engine"] == "claude_code"

    # the correct engine's resume_job was actually invoked, exactly once, on the right job_id
    assert calls == [(job_id, "th3_mcp.recover_work")]

    # deterministic reconciliation ran the correct engine's resume, then
    # (as this synthetic job's validation deliberately failed) terminalized
    # the proposal rather than leaving it ambiguously in-flight -- proposal
    # status is no longer {observed, proposed} either way.
    assert result["mutated"] is True
    assert result["resulting_status"] == proposal_mod.ProposalStatus.REJECTED

    # durable receipt shape
    assert result["operation"] == "recover_work"
    assert result["decision"]["stages"], "receipt must carry the stage-by-stage decision trail"
    assert result["generated_at_utc"]

    # IDEMPOTENT_RECOVERY: calling again must not duplicate/re-mutate --
    # the proposal is no longer eligible (status has moved past {observed, proposed}).
    calls.clear()
    result2 = T.recover_work(work_id)
    assert result2["mutated"] is False
    assert calls == [], "a second recovery call must not invoke resume_job again"
    assert result2["resulting_status"] == proposal_mod.ProposalStatus.REJECTED


# ---- DUPLICATE_DISPATCH_PREVENTED / DEDUP_LOCK_RELEASE_ATOMIC ---------------

def test_concurrent_claim_blocks_recovery_without_mutating(cleanup_proposals, monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("resume_job must not run while another process holds the proposal claim")
    monkeypatch.setattr(advance_mod.bridge, "resume_job", _must_not_be_called)

    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    lock_key = advance_mod._proposal_lock_key(work_id)
    assert job_ledger.claim(lock_key, owner="other_process") is True
    try:
        result = T.recover_work(work_id)
        assert result["mutated"] is False
        assert any(s.get("stage") == "concurrency_claim" and s.get("outcome") == "blocked"
                   for s in result["decision"]["stages"])
    finally:
        # release requires the same pid that claimed it -- this test process claimed it, so it can release it.
        job_ledger.release(lock_key, owner="other_process")


# ---- OWLS DEFECT_RECEIPT=e61d2e2b16df4b77 ----------------------------------
#
# Root cause: resume_job() (bridge.py and omniengineer_harness.py) and
# advance_one()'s proposal-scoped claim (evolution/advance.py) both called
# job_ledger.claim() on the job's/proposal's OWN lock file and, on failure,
# refused unconditionally -- even when that lock was left behind by an
# owner process that crashed WHILE HOLDING IT (never reached its own
# `finally: release()`). job_ledger.is_stale()/classify() correctly called
# the underlying WORK recovery-eligible, but the orphaned LOCK FILE itself
# silently blocked every future claim() forever: resume_job() refused,
# advance_one() fell through to a fresh attempt, and that fresh attempt was
# then correctly deduped against the very same still-non-terminal stale
# record -- a permanent mutated=false stalemate. break_stale_lock() already
# existed in this codebase for exactly this scenario (used by
# autonomous_cycle.py/cli.py for other lock keys) but nothing in the
# resume/advance path ever called it. Fixed via job_ledger.
# claim_or_break_stale(), used by both resume_job() implementations and by
# advance_one()'s proposal-scoped claim.

_DEAD_PID = 2 ** 31 - 1  # far beyond any real pid on this system


def _plant_dead_lock(job_id_or_key: str, *, seconds_ago: int = 2000) -> None:
    """Simulates the exact OWLS precondition: a lock file for an owner
    process that is provably not alive -- not merely a stale heartbeat on
    the ledger record itself, but the LOCK FILE surviving its dead owner."""
    job_ledger._lock_path(job_id_or_key).parent.mkdir(parents=True, exist_ok=True)
    job_ledger._lock_path(job_id_or_key).write_text(json.dumps({
        "owner": "crashed_process", "pid": _DEAD_PID, "hostname": "test",
        "claimed_at": (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(),
    }))


def test_claim_or_break_stale_never_touches_a_lock_held_by_a_live_pid():
    job_id = f"test-lock-live-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="TEST-ONLY: live lock must never be broken", requested_by="test", sandbox_path=str(workdir))
    assert job_ledger.claim(job_id, owner="live_owner") is True  # this test process's own real, live pid
    try:
        claimed, broke = job_ledger.claim_or_break_stale(job_id, owner="recover_attempt")
        assert claimed is False
        assert broke is False
        status = job_ledger.lock_status(job_id)
        assert status["held"] is True
        assert status["owner"] == "live_owner"  # untouched -- never raced or broken
    finally:
        job_ledger.release(job_id, owner="live_owner")
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


def test_claim_or_break_stale_breaks_a_dead_pid_lock_and_claims():
    job_id = f"test-lock-dead-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="TEST-ONLY: dead lock must be broken and reclaimed", requested_by="test", sandbox_path=str(workdir))
    _plant_dead_lock(job_id)
    try:
        claimed, broke = job_ledger.claim_or_break_stale(job_id, owner="recover_attempt")
        assert claimed is True
        assert broke is True
        status = job_ledger.lock_status(job_id)
        assert status["owner"] == "recover_attempt"
    finally:
        job_ledger.release(job_id, owner="recover_attempt")
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


def test_owls_defect_dead_job_lock_no_longer_leaves_recovery_stuck(cleanup_proposals, cleanup_jobs, monkeypatch):
    """Full end-to-end reproduction of DEFECT_RECEIPT=e61d2e2b16df4b77 through
    the real (unmocked) resume_job()/advance_one() lock logic -- only the
    outbound `claude -p` subprocess is mocked (same pattern
    test_bridge_ledger.py already uses), so this proves the actual fix, not
    a stand-in."""
    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, selected_engine="claude_code", stale_seconds_ago=2000)
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")

    _plant_dead_lock(job_id)  # the owner that was editing this job crashed mid-run, lock never released
    pre_status = job_ledger.lock_status(job_id)
    assert pre_status["held"] is True and pre_status["stale"] is True, "sanity: precondition must reproduce a stale, held lock"

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "recovered.txt").write_text("recovered")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": "ok"}', stderr="")

    monkeypatch.setattr(advance_mod.bridge.subprocess, "run", fake_run)

    task_fingerprint = job_ledger.load(job_id).task_fingerprint
    # BEFORE the fix this would have stayed permanently non-terminal and
    # permanently visible to dedup -- assert the real starting condition.
    assert job_ledger.find_active_by_fingerprint(task_fingerprint) is not None

    result = T.recover_work(work_id, reason="TEST-ONLY: reproduce and verify the OWLS dead-lock defect fix")

    reconciled = job_ledger.load(job_id)
    assert reconciled.resume_count == 1, "a real resume attempt must have run, not a refusal"
    assert reconciled.state in {s.value for s in job_ledger.TERMINAL_STATES}, \
        "the stale job must be durably reconciled to a real terminal state, never left frozen"
    assert job_ledger.lock_status(job_id)["held"] is False, "the job-level lock must be released after reconciliation"

    # STALE_NO_LONGER_BLOCKS_FRESH_DEDUP
    assert job_ledger.find_active_by_fingerprint(task_fingerprint) is None

    # RECOVERY_ELIGIBLE_STALE_MUTATES / DURABLE_RECONCILIATION
    assert result["mutated"] is True
    assert result["resulting_status"] != result["prior_status"]

    # IDEMPOTENT_RECOVERY: calling again must not re-break anything or
    # re-mutate -- the proposal has already moved past {observed, proposed}.
    result2 = T.recover_work(work_id)
    assert result2["mutated"] is False
    assert job_ledger.load(job_id).resume_count == 1, "idempotent retry must not re-trigger another resume attempt"


def test_owls_defect_dead_proposal_lock_no_longer_leaves_recovery_stuck(cleanup_proposals, monkeypatch):
    """Same defect class, one layer up: the proposal-scoped implementation
    claim (evolution.advance._proposal_lock_key) can be orphaned by a dead
    advancer exactly like a job-level lock. Proves advance_one()'s claim is
    also self-healing now, using a proposal with no implementation job at
    all yet (isolates this from the job-level fix, which is covered by
    test_owls_defect_dead_job_lock_no_longer_leaves_recovery_stuck above).

    The implementation router is neutered to a harmless, real, no-engine-
    available outcome (the same outcome advance_one() already produces when
    e.g. every engine is quota-limited) so this test never dispatches a real
    engine/subprocess -- it isolates the concurrency_claim stage only."""
    def _no_engine_available(task_text, requested_by, proposal_id, *, allow_paid=True):
        return advance_mod.ImplementationOutcome(
            selected_engine=None, engine_selection_reason="TEST-ONLY: neutered, no real engine dispatched",
            engines_considered=[], quota_state={}, authority_state={}, fallback_reason=None,
            local_attempt_result=None, job=None, terminal_reject=False, terminal_reject_detail=None,
        )
    monkeypatch.setattr(advance_mod, "_implementation_router", _no_engine_available)

    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    lock_key = advance_mod._proposal_lock_key(work_id)
    _plant_dead_lock(lock_key)
    pre_status = job_ledger.lock_status(lock_key)
    assert pre_status["held"] is True and pre_status["stale"] is True

    result = advance_mod.advance_one(work_id, requested_by="th3_mcp.recover_work")

    assert not any(s["stage"] == "concurrency_claim" and s["outcome"] == "blocked" for s in result.stages), \
        "a dead advancer's orphaned proposal-level lock must be broken, not refused forever"
    assert any(s["stage"] == "concurrency_claim" and s["outcome"] == "ok" for s in result.stages)
    assert job_ledger.lock_status(lock_key)["held"] is False


# ---- DEFECT_SIGNAL=389f05d585044a8b -----------------------------------------
#
# Root cause: job_ledger.is_stale() was PURELY heartbeat-based. checkpoint()
# refreshes a job's `heartbeat` field on EVERY call -- including the one
# resume_job() makes the instant a resume attempt STARTS. If that resuming
# process then crashes (e.g. an infra failure) before its next checkpoint,
# the ledger's heartbeat looks "fresh" for up to STALE_AFTER_S afterward even
# though the owner is now definitively dead -- while the job's LOCK FILE
# (held by that exact, now-dead pid) is unambiguous, directly verifiable
# proof of death. is_stale() never consulted the lock at all, so
# classify()/_job_terminal_status() trusted the weaker, misleading heartbeat
# signal and reported the job "active_not_stale" -- contradicting the lock's
# own alive=false/stale=true evidence. Fixed by making is_stale() treat a
# HELD lock's pid-liveness as authoritative (overriding heartbeat in both
# directions) and only falling back to heartbeat when no lock is held.

def test_owls_defect_fresh_heartbeat_but_dead_lock_pid_is_still_classified_stale(cleanup_proposals, cleanup_jobs, monkeypatch):
    """A: lock_alive=false + lock_stale=true + eligible stale owner ->
    deterministic stale classification -> governed durable reconciliation.
    Reproduces the EXACT reported contradiction: heartbeat is left FRESH
    (as if the owner had just checkpointed) while the lock's pid is dead."""
    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, selected_engine="claude_code")  # heartbeat left fresh, deliberately
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")

    _plant_dead_lock(job_id, seconds_ago=5)  # claimed moments ago -- staleness can ONLY come from the dead pid, not lock age
    lock = job_ledger.lock_status(job_id)
    assert lock["alive"] is False and lock["stale"] is True, "sanity: reproduces OWLS's WORK_STATUS_LOCK_ALIVE=false / LOCK_STALE=true"

    # THE FIX under direct test: lock evidence must override a merely-fresh heartbeat.
    assert job_ledger.is_stale(job_ledger.load(job_id)) is True

    def fake_run(cmd, **kwargs):
        workdir = Path(kwargs["cwd"])
        (workdir / "recovered.txt").write_text("recovered")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": "ok"}', stderr="")
    monkeypatch.setattr(advance_mod.bridge.subprocess, "run", fake_run)

    task_fingerprint = job_ledger.load(job_id).task_fingerprint
    result = T.recover_work(work_id, reason="TEST-ONLY: reproduce and verify DEFECT_SIGNAL=389f05d585044a8b")

    # DETERMINISTIC_RECONCILIATION: never classified as prior_job_still_active
    assert result["prior_implementation_job"]["is_stale"] is True
    assert result["decision"]["blocked_reason"] != "prior_job_still_active"
    assert not any("still appears active and not stale" in s.get("detail", "") for s in result["decision"]["stages"])

    # DURABLE_RECONCILIATION
    assert result["mutated"] is True
    reconciled = job_ledger.load(job_id)
    assert reconciled.state in {s.value for s in job_ledger.TERMINAL_STATES}

    # E: STALE_NO_LONGER_BLOCKS_FRESH_DEDUP
    assert job_ledger.find_active_by_fingerprint(task_fingerprint) is None

    # D: IDEMPOTENT_RECOVERY
    result2 = T.recover_work(work_id)
    assert result2["mutated"] is False
    assert job_ledger.load(job_id).resume_count == reconciled.resume_count


def test_live_locked_job_protected_even_if_heartbeat_looks_stale(cleanup_proposals, cleanup_jobs, monkeypatch):
    """B: a genuinely live prior job -> recover_work refuses mutation. The
    other direction of the same fix: a lock held by a genuinely LIVE pid
    must protect the job even when its heartbeat happens to look old (e.g.
    one unusually long tool call between checkpoints) -- lock evidence is
    authoritative in both directions, not only the dead-pid direction."""
    def _must_not_be_called(*a, **k):
        raise AssertionError("resume_job must not run against a job a live process still holds the lock on")
    monkeypatch.setattr(advance_mod.bridge, "resume_job", _must_not_be_called)

    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, selected_engine="claude_code", stale_seconds_ago=2000)  # heartbeat LOOKS stale
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")
    assert job_ledger.claim(job_id, owner="genuinely_alive_worker") is True  # this test process's own real, live pid
    try:
        result = T.recover_work(work_id)
        assert result["mutated"] is False
        assert result["prior_implementation_job"]["is_stale"] is False, \
            "a live-held lock must override a stale-looking heartbeat, not merely coexist with the contradiction"
    finally:
        job_ledger.release(job_id, owner="genuinely_alive_worker")
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


def test_corrupt_lock_evidence_is_ambiguous_and_fails_closed():
    """C: contradictory/ambiguous authoritative evidence -> fail closed.
    An unreadable lock file proves nothing about the owner either way --
    must never be treated as license to recover."""
    job_id = f"test-lock-corrupt-{uuid.uuid4()}"
    workdir = job_ledger.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    job_ledger.create(job_id, task="TEST-ONLY: corrupt lock must fail closed", requested_by="test", sandbox_path=str(workdir))
    job_ledger.checkpoint(job_id, JobState.EDITING)
    job_ledger._lock_path(job_id).write_text("{not valid json")
    try:
        status = job_ledger.lock_status(job_id)
        assert status["held"] is True
        assert status.get("corrupt") is True
        assert job_ledger.is_stale(job_ledger.load(job_id)) is False, \
            "ambiguous/corrupt lock evidence must fail closed, never be treated as proof of staleness"
    finally:
        job_ledger._lock_path(job_id).unlink(missing_ok=True)
        job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


# ---- DEFECT_SIGNAL=07387e2947ac43d5 -----------------------------------------
#
# Root cause: a job that is definitively stale/dead-owner (is_stale()
# confirms it) but has exhausted its bounded MAX_RESUME_ATTEMPTS budget hits
# _job_terminal_status()'s "stale_not_resumable" branch. classify() correctly
# refuses to auto-resume it AGAIN (that bound is intentional), but nothing
# ever durably reconciled the ledger RECORD itself -- _resolve_prior_attempt()
# just fell through to a fresh implementation attempt while the old record
# stayed non-terminal forever, so find_active_by_fingerprint() kept reporting
# it "active" and every subsequent fresh attempt was deduped against it: a
# permanent mutated=false stalemate. This is what a job dying a SECOND time
# (once during its original run, once again during its one bounded resume)
# reproduces. Fixed by durably terminalizing the record -- via the same
# atomic claim/checkpoint/release primitives resume_job() uses, so a job
# anyone still legitimately owns is never raced -- before permitting fresh
# routing, EXCEPT when the reason is FOUNDER_REQUIRED (approval-pending),
# which must never be auto-terminalized: "no recovery may bypass authority."

def test_defect_07387e_exhausted_stale_job_is_durably_terminalized_before_fresh_routing(cleanup_proposals, cleanup_jobs):
    """A-E/G: proven stale/dead-owner + exhausted resume budget -> durably
    reconciled to a real terminal state -> lock released -> no longer
    blocks dedup. Exercises _resolve_prior_attempt() directly -- the exact
    function that had the ordering gap."""
    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.EDITING, selected_engine="claude_code", resume_count=1)  # budget already exhausted
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")
    _plant_dead_lock(job_id, seconds_ago=5)  # dead owner, mid-second (resumed) attempt

    fp = job_ledger.load(job_id).task_fingerprint
    assert job_ledger.find_active_by_fingerprint(fp) is not None, "sanity: still blocking before the fix's reconciliation runs"

    p = proposal_mod.load(work_id)
    job, note, refuse = advance_mod._resolve_prior_attempt(p, requested_by="th3_mcp.recover_work")

    # B/C: durably reconciled to a real terminal state, lock released
    reconciled = job_ledger.load(job_id)
    assert reconciled.state == JobState.FAILED.value
    assert reconciled.terminal_result == "stale_resume_exhausted"
    assert job_ledger.lock_status(job_id)["held"] is False

    # G: STALE_NO_LONGER_BLOCKS_FRESH_DEDUP
    assert job_ledger.find_active_by_fingerprint(fp) is None

    # F: caller is correctly told nothing was reused (job=None) but NOT to refuse -- fresh routing is now genuinely permitted
    assert job is None
    assert refuse is False
    assert "will try a fresh attempt" in note

    # D/IDEMPOTENT_RECOVERY: calling again must not re-terminalize or error --
    # the record is already terminal, so _job_terminal_status short-circuits
    # to "failure" long before this branch is even reached again.
    job2, note2, refuse2 = advance_mod._resolve_prior_attempt(proposal_mod.load(work_id), requested_by="th3_mcp.recover_work")
    assert job2 is None and refuse2 is False
    assert job_ledger.load(job_id).state == JobState.FAILED.value  # unchanged, not re-terminalized


def test_defect_07387e_founder_required_job_is_never_auto_terminalized(cleanup_proposals, cleanup_jobs):
    """Founder gate preservation: a job that died while genuinely awaiting
    Founder approval must NEVER be auto-terminalized by the new supersede
    logic, even though it is also stale/dead-owner with resume_count
    already at the bound -- a human must still decide. This is the one
    case within 'stale_not_resumable' the fix must NOT touch."""
    work_id = _make_proposal(cleanup_proposals, risk_score="low", status=proposal_mod.ProposalStatus.PROPOSED)
    job_id = _make_job(cleanup_jobs, state=JobState.AUTHORIZED, selected_engine="claude_code", resume_count=1)
    job_ledger.checkpoint(job_id, JobState.AUTHORIZED, approval_state="pending_approval")
    proposal_mod.append_implementation_job(work_id, job_id, engine="claude_code")
    _plant_dead_lock(job_id, seconds_ago=5)

    p = proposal_mod.load(work_id)
    job, note, refuse = advance_mod._resolve_prior_attempt(p, requested_by="th3_mcp.recover_work")

    reconciled = job_ledger.load(job_id)
    assert reconciled.state == JobState.AUTHORIZED.value, "a Founder-gated job must never be silently terminalized"
    assert reconciled.terminal_result is None
    assert job is None
