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
