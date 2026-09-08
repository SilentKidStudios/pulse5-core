#!/usr/bin/env python3
"""
Tests for evolution/advance.py::reconcile_orphaned_canary() — the
CANARY_RECONCILIATION gap-closure (2026-09-08).

Real, live evidence this closes: proposals 83c43e59-fcfb-4b3f-ab0c-
29f2dba8d17b and c3b3c10a-dabf-4b92-923b-6039cbb67ad3 both crashed/were
interrupted AFTER their canary passed (durably recorded: ProposalStatus.
CANARY) but BEFORE advance_one() reached PROMOTION_CANDIDATE. The
ORPHANED_STATUS gap-closure in proposal_work_bridge.py (same date) already
stopped these from retry-storming — it correctly parks them in
BLOCKED_FOUNDER (source="orphaned_status") forever — but never actually
resumed them; this is that resume.

Same plain-script style, and the same live-store convention (no
monkeypatched PROPOSALS_DIR/JOBS_ROOT), as tests/test_evolution_advance.py
— every proposal created here is real and lives in the same store as
production proposals, always cleaned up via _cleanup() (REJECTED + a
lesson recorded), exactly as that file's own test_promotion_still_requires_
founder_approved already does against this same live store.

Run: python3 tests/test_canary_reconciliation.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import promotion
from evolution import advance
from evolution import founder_request
from evolution import proposal as proposal_mod

JOBS_ROOT = Path(__file__).resolve().parent.parent / "jobs"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    assert condition, f"{name}" + (f" — {detail}" if detail else "")


@dataclass
class _FakeJobResult:
    job_id: str
    status: str
    workdir: str
    files_changed: dict
    promotion_eligible: bool
    adapter: str = "omni_engineer"


def _fake_real_job(job_id: str, *, adapter: str = "omni_engineer") -> _FakeJobResult:
    """Same on-disk shape as test_evolution_advance.py's helper of the same
    name — a real jobs/<job_id>/{workdir/x.py, result.json} pair, so
    evolution.advance._load_job_result_from_disk() works against it
    unmodified, without ever running a live engine."""
    workdir = JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "x.py").write_text("x = 1\n")
    files_changed = {"added": ["x.py"], "modified": [], "removed": []}
    result = {
        "job_id": job_id, "status": "succeeded", "workdir": str(workdir),
        "files_changed": files_changed, "promotion_eligible": True,
        "adapter": adapter, "validation": {"passed": True, "checks": []},
    }
    (JOBS_ROOT / job_id / "result.json").write_text(json.dumps(result))
    return _FakeJobResult(job_id=job_id, status="succeeded", workdir=str(workdir),
                           files_changed=files_changed, promotion_eligible=True, adapter=adapter)


def _make_low_risk_proposal(weakness: str, upgrade: str) -> proposal_mod.Proposal:
    return proposal_mod.create(observed_weakness=weakness, proposed_upgrade=upgrade, risk_score="low", origin="manual")


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)
        proposal_mod.record_lesson(proposal_id, note)


def _advance_to_orphaned_canary(p: proposal_mod.Proposal, job: _FakeJobResult, *, canary_state: str = "CANARY_NOT_REQUIRED") -> proposal_mod.Proposal:
    """Reproduces the EXACT durable shape advance_one() leaves behind right
    before the crash point this fix targets: implemented -> tested ->
    canary, each its own proposal_mod.advance() call, the last one carrying
    the SAME note format advance_one() itself writes
    (f"canary passed ({canary_state})") — never calling advance_one() or
    any real engine, matching "crash/restart with persisted canary status":
    nothing but this proposal's own durable file is ever touched."""
    proposal_mod.append_implementation_job(p.proposal_id, job.job_id, engine=job.adapter, note="fake test job")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED, note="test fixture")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED, note="test fixture")
    return proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note=f"canary passed ({canary_state})")


# ---- successful canary -> correct resumable/reconciled state ---------------

def test_successful_canary_reconciles_to_promotion_candidate() -> None:
    p = _make_low_risk_proposal("synthetic test: canary reconciliation", "prove orphaned canary resumes")
    job = _fake_real_job(f"test-fake-canary-recon-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)

    result = advance.reconcile_orphaned_canary(p.proposal_id)

    check("reaches promotion_candidate", result.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, result.final_status)
    check("blocked_reason is None (a real success, not a blocked no-op)", result.blocked_reason is None, str(result.blocked_reason))
    check("reuses the existing job_id, never a new one", result.implementation_job_id == job.job_id, result.implementation_job_id)

    reloaded = proposal_mod.load(p.proposal_id)
    check("durable status is promotion_candidate", reloaded.status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, reloaded.status)
    check("implementation_job_ids untouched (no duplicate dispatch)", reloaded.implementation_job_ids == [job.job_id], reloaded.implementation_job_ids)

    decision_fingerprint = founder_request._fingerprint(f"proposal {p.proposal_id}", "production_promotion")
    escalation_path = founder_request.ESCALATIONS_DIR / f"{decision_fingerprint}.json"
    check("Founder communication was (re-)issued", escalation_path.exists(), str(escalation_path))

    _cleanup(p.proposal_id, "synthetic canary-reconciliation test — cleaned up per test-artifact policy")


# ---- already-reconciled proposal / repeated reconciliation -----------------

def test_repeated_reconciliation_is_idempotent_and_a_noop_second_time() -> None:
    p = _make_low_risk_proposal("synthetic test: repeated reconciliation", "prove idempotency")
    job = _fake_real_job(f"test-fake-canary-repeat-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)

    first = advance.reconcile_orphaned_canary(p.proposal_id)
    check("first call reconciles", first.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, first.final_status)
    history_len_after_first = len(proposal_mod.load(p.proposal_id).history)

    second = advance.reconcile_orphaned_canary(p.proposal_id)
    check("second call is a no-op (already reconciled)", second.blocked_reason == "not_orphaned_at_canary", second.blocked_reason)
    check("second call reports the current (unchanged) status", second.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, second.final_status)

    reloaded = proposal_mod.load(p.proposal_id)
    check("no new history entries were written on the no-op call",
          len(reloaded.history) == history_len_after_first, (len(reloaded.history), history_len_after_first))

    _cleanup(p.proposal_id, "synthetic repeated-reconciliation test — cleaned up per test-artifact policy")


# ---- failed canary must not masquerade as success --------------------------

def test_failed_canary_left_at_tested_is_never_reconciled() -> None:
    """A GENUINELY failed canary never writes ProposalStatus.CANARY at all
    (see advance_one()'s own `if not canary.passed: ... return` guard,
    which returns before ever reaching the CANARY advance() call) — it
    leaves the proposal at TESTED instead. Reproduces that exact shape
    directly (no real canary run needed) and proves reconciliation refuses
    to touch it."""
    p = _make_low_risk_proposal("synthetic test: failed canary", "prove failed canary is never resumed")
    job = _fake_real_job(f"test-fake-canary-failed-{p.proposal_id[:8]}")
    proposal_mod.append_implementation_job(p.proposal_id, job.job_id, engine=job.adapter, note="fake test job")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED, note="test fixture")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED, note="test fixture")
    # deliberately never advances to CANARY — this IS what a failed canary looks like on disk

    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("refuses to touch a TESTED (not-yet/never-canaried) proposal",
          result.blocked_reason == "not_orphaned_at_canary", result.blocked_reason)

    reloaded = proposal_mod.load(p.proposal_id)
    check("status remains TESTED — never promoted on a failure's behalf",
          reloaded.status == proposal_mod.ProposalStatus.TESTED, reloaded.status)

    _cleanup(p.proposal_id, "synthetic failed-canary test — cleaned up per test-artifact policy")


def test_canary_status_without_verified_pass_note_is_not_reconciled() -> None:
    """Belt-and-suspenders: reconciliation never trusts the bare status
    label alone — only a last history entry that actually records the
    pass. A CANARY status written any other way (corruption, a future
    unrelated code path) must escalate for human review, never be
    silently promoted."""
    p = _make_low_risk_proposal("synthetic test: unverified canary status", "prove unverified canary is not resumed")
    job = _fake_real_job(f"test-fake-canary-unverified-{p.proposal_id[:8]}")
    proposal_mod.append_implementation_job(p.proposal_id, job.job_id, engine=job.adapter, note="fake test job")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED, note="test fixture")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED, note="test fixture")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note="")  # no "canary passed" evidence

    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("refuses to resume an unverified canary status",
          result.blocked_reason == "canary_status_unverified", result.blocked_reason)

    reloaded = proposal_mod.load(p.proposal_id)
    check("status remains CANARY, untouched", reloaded.status == proposal_mod.ProposalStatus.CANARY, reloaded.status)

    _cleanup(p.proposal_id, "synthetic unverified-canary test — cleaned up per test-artifact policy")


# ---- no risk_score mutation / promotion still separately gated -------------

def test_reconciliation_never_mutates_risk_score_and_never_promotes() -> None:
    p = _make_low_risk_proposal("synthetic test: no promotion bypass", "prove reconciliation never promotes or mutates risk")
    job = _fake_real_job(f"test-fake-canary-nopromo-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)

    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("reconciled", result.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, result.final_status)

    reloaded = proposal_mod.load(p.proposal_id)
    check("risk_score untouched", reloaded.risk_score == "low", reloaded.risk_score)
    check("status is promotion_candidate, never promoted", reloaded.status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, reloaded.status)

    never_written_target = str(promotion.PULSE5_ROOT / "_never_written_by_test_canary_reconciliation")
    record = promotion.promote(result.implementation_job_id, never_written_target, founder_approved=False)
    check("promotion without --founder-approved stays a dry_run and writes nothing",
          record.approval_state == "dry_run", record.approval_state)

    _cleanup(p.proposal_id, "synthetic no-promotion-bypass test — cleaned up per test-artifact policy")


# ---- founder-gated proposal after canary -----------------------------------

def test_founder_gated_proposal_after_canary_stays_gated_for_actual_promotion() -> None:
    """A founder_gated proposal can only ever have REACHED canary in the
    first place after an explicit Founder approval (evolution/advance.py::
    _eligible() requires exact_proposal_decision()=='approved' before
    advancing past OBSERVED at all) — reconciliation does not re-decide
    that, does not touch risk_score, and does not itself promote; actual
    promotion remains a separate --founder-approved CLI action."""
    p = proposal_mod.create(
        observed_weakness="synthetic test: founder-gated canary reconciliation",
        proposed_upgrade="prove founder_gated stays founder_gated through reconciliation",
        risk_score="founder_gated", origin="manual",
    )
    escalation = founder_request.request_founder_decision(
        subject=f"proposal {p.proposal_id}", finding="test fixture", capability_needed="implementation_approval",
        reason_required="founder_gated risk", recommended_action="approve implementation",
        risk="founder_gated", affected={"proposal_id": p.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")

    job = _fake_real_job(f"test-fake-canary-foundergated-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)

    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("founder-gated proposal still reconciles to promotion_candidate",
          result.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, result.final_status)

    reloaded = proposal_mod.load(p.proposal_id)
    check("risk_score remains founder_gated", reloaded.risk_score == "founder_gated", reloaded.risk_score)

    never_written_target = str(promotion.PULSE5_ROOT / "_never_written_by_test_canary_reconciliation_fg")
    record = promotion.promote(result.implementation_job_id, never_written_target, founder_approved=False)
    check("actual promotion is STILL separately gated even for a founder-gated, reconciled proposal",
          record.approval_state == "dry_run", record.approval_state)

    _cleanup(p.proposal_id, "synthetic founder-gated canary-reconciliation test — cleaned up per test-artifact policy")


# ---- no duplicate implementation dispatch ----------------------------------

def test_reconciliation_performs_no_duplicate_implementation_dispatch() -> None:
    p = _make_low_risk_proposal("synthetic test: no duplicate dispatch", "prove reconciliation never re-implements")
    job = _fake_real_job(f"test-fake-canary-nodup-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)
    job_dirs_before = {d.name for d in JOBS_ROOT.iterdir() if d.is_dir()}

    advance.reconcile_orphaned_canary(p.proposal_id)

    reloaded = proposal_mod.load(p.proposal_id)
    check("exactly one implementation attempt on record", reloaded.implementation_attempts == 1, reloaded.implementation_attempts)
    check("implementation_job_ids has exactly the original job, nothing appended",
          reloaded.implementation_job_ids == [job.job_id], reloaded.implementation_job_ids)
    job_dirs_after = {d.name for d in JOBS_ROOT.iterdir() if d.is_dir()}
    check("no new job directory was created", job_dirs_after == job_dirs_before, job_dirs_after - job_dirs_before)

    _cleanup(p.proposal_id, "synthetic no-duplicate-dispatch test — cleaned up per test-artifact policy")


# ---- crash/restart with persisted canary status ----------------------------

def test_crash_restart_with_persisted_canary_status_reconciles_from_disk_only() -> None:
    """Simulates a process restart: the ONLY state reconcile_orphaned_
    canary() ever consults is proposal_mod.load(proposal_id) (a fresh disk
    read) plus the job's own result.json on disk — never anything held in
    memory from whatever process originally ran the canary. Building the
    fixture via direct proposal_mod.advance() calls (never advance_one())
    already proves this: there is no in-process state to lose."""
    p = _make_low_risk_proposal("synthetic test: crash/restart persisted canary", "prove reconciliation is disk-only, restart-safe")
    job = _fake_real_job(f"test-fake-canary-restart-{p.proposal_id[:8]}")
    _advance_to_orphaned_canary(p, job)
    check("fixture is durably at CANARY before any reconciliation call",
          proposal_mod.load(p.proposal_id).status == proposal_mod.ProposalStatus.CANARY, "fixture setup")

    # "restart": a brand new call into advance.py with nothing but the
    # proposal_id — exactly what a fresh autonomous-cycle process has.
    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("resumes correctly after a simulated restart",
          result.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, result.final_status)

    _cleanup(p.proposal_id, "synthetic crash-restart canary test — cleaned up per test-artifact policy")


# ---- the two historical target-state shapes --------------------------------

def test_historical_target_proposal_shape_reconciles_correctly() -> None:
    """Reproduces the EXACT durable shape of the two real, live orphaned
    proposals this fix exists for (83c43e59-fcfb-4b3f-ab0c-29f2dba8d17b and
    c3b3c10a-dabf-4b92-923b-6039cbb67ad3): risk_score=low, a single
    implementation_job_id, and a history whose last entry is
    {"event": "advanced", "status": "canary", "note": "canary passed"} —
    read directly from both proposals' own durable JSON records."""
    p = _make_low_risk_proposal(
        "synthetic test: cross-provider failover (historical shape)",
        "prove record_experience fires for a cross-provider outcome",
    )
    job = _fake_real_job(f"test-fake-canary-historical-{p.proposal_id[:8]}")
    proposal_mod.append_implementation_job(p.proposal_id, job.job_id, engine="omni_engineer", note="job created (linked before execution)")
    proposal_mod.record_experience(
        p.proposal_id,
        incident_fingerprint={"symptom": "model/provider failover occurred", "error_signature": "synthetic",
                               "affected_organ": "omni_engineer", "environment_context": "test"},
        root_cause={"explanation": "synthetic", "confidence": "high", "evidence": "test"},
        remediation={"procedure": "failed over to provider='provider_b'", "authority_required": "none", "affected_files_or_services": []},
    )
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED,
                          note=f"job {job.job_id} succeeded via omni_engineer (attempt 1)")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED,
                          note=f"validation passed for job {job.job_id}")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note="canary passed")

    reloaded_before = proposal_mod.load(p.proposal_id)
    check("reproduced shape matches the historical record's last history entry",
          reloaded_before.history[-1] == {"at": reloaded_before.history[-1]["at"], "event": "advanced",
                                           "status": "canary", "note": "canary passed"},
          reloaded_before.history[-1])

    result = advance.reconcile_orphaned_canary(p.proposal_id)
    check("the historical shape reconciles to promotion_candidate",
          result.final_status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, result.final_status)

    _cleanup(p.proposal_id, "synthetic historical-shape canary-reconciliation test — cleaned up per test-artifact policy")


if __name__ == "__main__":
    test_successful_canary_reconciles_to_promotion_candidate()
    test_repeated_reconciliation_is_idempotent_and_a_noop_second_time()
    test_failed_canary_left_at_tested_is_never_reconciled()
    test_canary_status_without_verified_pass_note_is_not_reconciled()
    test_reconciliation_never_mutates_risk_score_and_never_promotes()
    test_founder_gated_proposal_after_canary_stays_gated_for_actual_promotion()
    test_reconciliation_performs_no_duplicate_implementation_dispatch()
    test_crash_restart_with_persisted_canary_status_reconciles_from_disk_only()
    test_historical_target_proposal_shape_reconciles_correctly()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
