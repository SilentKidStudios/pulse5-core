"""Tests for approval_backlog_hygiene.py — Studio-wide Walk-Away, discovery
source #2 (2026-09-07)."""
from __future__ import annotations

import json

import approval_backlog_hygiene as abh
import job_ledger
from evolution import founder_request
from evolution import proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    d = tmp_path / "escalations"
    monkeypatch.setattr(founder_request, "ESCALATIONS_DIR", d)
    monkeypatch.setattr(abh, "founder_request", founder_request)
    # 2026-09-08 fix: this was missing, so every test creating a job_ledger
    # record (test_underlying_job_already_terminal,
    # test_underlying_job_not_terminal_falls_through) leaked a real
    # "finished-job-1"/"running-job-1" directory into the production
    # mrsilent_bridge/jobs/ store on every run — discovered via the
    # whole-Studio census (job_ledger.list_all() surfaced them as
    # non-terminal/real-looking jobs). Cleaned up separately; this
    # prevents recurrence.
    monkeypatch.setattr(job_ledger, "JOBS_ROOT", tmp_path / "jobs")
    # 2026-09-08 backlog-hygiene-retirement tests: isolates real proposal
    # creation (PROPOSAL_ALREADY_SATISFIED/PROPOSAL_SUPERSEDED tests need
    # real Proposal records) the same way JOBS_ROOT is isolated above.
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(abh, "proposal_mod", proposal_mod)
    return d


def _request(subject="s", capability_needed="production_promotion", job_id="job-1",
             proposal_id="prop-1", finding="a real finding", requested_by="mrsilent"):
    return founder_request.request_founder_decision(
        subject=subject, finding=finding, capability_needed=capability_needed,
        reason_required="always founder-gated", recommended_action="do the thing",
        risk="low", affected={"proposal_id": proposal_id, "job_id": job_id},
        requested_by=requested_by,
    )


def test_true_founder_gate_classified_correctly(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.TRUE_FOUNDER_GATE


def test_synthetic_test_noise_detected_by_job_id_prefix(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(job_id="test-fake-omni_engineer-ok-abc123", capability_needed="human_review_of_failed_self_correction")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.SYNTHETIC_TEST_NOISE


def test_synthetic_test_noise_detected_by_fake_job_id_without_test_prefix(monkeypatch, tmp_path):
    """Real live leak found 2026-09-08 (escalation c634ab2d33d97a70):
    job_id='fake-canary-job', requested_by='mrsilent', finding='x' —
    slipped past the original job_id.startswith('test-') check."""
    _fresh(monkeypatch, tmp_path)
    rec = _request(job_id="fake-canary-job", finding="x", capability_needed="production_promotion")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.SYNTHETIC_TEST_NOISE


def test_synthetic_test_noise_detected_by_fake_in_recommended_action(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(job_id="normal-looking-job-id", finding="x", capability_needed="production_promotion")
    rec["payload"]["recommended_action"] = "promote job fake-canary-job to a real path"
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.SYNTHETIC_TEST_NOISE


def test_synthetic_test_noise_detected_by_finding_text(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(finding="synthetic test: recovery step 1", job_id="normal-job", capability_needed="human_review_of_failed_self_correction")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.SYNTHETIC_TEST_NOISE


def test_synthetic_test_noise_detected_by_requested_by(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(requested_by="test", capability_needed="human_review_of_failed_self_correction")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.SYNTHETIC_TEST_NOISE


def test_denied_approval_is_resolved_non_pending(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request()
    founder_request.resolve_founder_decision(rec["escalation_id"], "denied")
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    c = abh.classify_escalation(updated)
    assert c.category == abh.Category.RESOLVED_NON_PENDING
    assert "denied" in c.reason


def test_approved_approval_is_resolved_non_pending(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request()
    founder_request.resolve_founder_decision(rec["escalation_id"], "approved")
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    c = abh.classify_escalation(updated)
    assert c.category == abh.Category.RESOLVED_NON_PENDING


def test_superseded_duplicate_proposal_pending_record_resolved_via_other_escalation(monkeypatch, tmp_path):
    """Same proposal_id raised under two different subjects/escalation_ids —
    one gets a real decision; the OTHER, still-pending record for the same
    proposal must be classified ALREADY_RESOLVED_ELSEWHERE, not as a fresh
    founder gate needing a second decision."""
    _fresh(monkeypatch, tmp_path)
    rec_a = _request(subject="subject A", proposal_id="shared-prop", capability_needed="human_review_of_failed_self_correction")
    rec_b = _request(subject="subject B", proposal_id="shared-prop", capability_needed="human_review_of_failed_self_correction")
    assert rec_a["escalation_id"] != rec_b["escalation_id"]
    founder_request.resolve_founder_decision(rec_a["escalation_id"], "denied")

    still_pending_b = json.loads((founder_request.ESCALATIONS_DIR / f"{rec_b['escalation_id']}.json").read_text())
    c = abh.classify_escalation(still_pending_b)
    assert c.category == abh.Category.ALREADY_RESOLVED_ELSEWHERE
    assert "denied" in c.reason


def test_underlying_job_already_terminal(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="finished-job-1", task="t", requested_by="mrsilent", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("finished-job-1", job_ledger.JobState.COMPLETED)
    rec = _request(job_id="finished-job-1", proposal_id="prop-for-finished-job", capability_needed="human_review_of_failed_self_correction")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.UNDERLYING_JOB_TERMINAL
    assert "finished-job-1" in c.reason


def test_underlying_job_not_terminal_falls_through(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="running-job-1", task="t", requested_by="mrsilent", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("running-job-1", job_ledger.JobState.EDITING)
    rec = _request(job_id="running-job-1", proposal_id="prop-for-running-job", capability_needed="human_review_of_failed_self_correction")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT


def test_needs_founder_or_operator_judgment_when_no_evidence_resolves_it(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="manual investigation and/or job_ledger.break_stale_lock()+resume/checkpoint",
                    job_id="unresolved-job", proposal_id="unresolved-prop")
    c = abh.classify_escalation(rec)
    assert c.category == abh.Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT


def test_stale_real_pending_counted_only_past_threshold(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion")
    path = d / f"{rec['escalation_id']}.json"
    stored = json.loads(path.read_text())
    from datetime import datetime, timedelta, timezone
    stored["created_at"] = (datetime.now(timezone.utc) - timedelta(days=abh.STALE_AFTER_DAYS + 1)).isoformat()
    path.write_text(json.dumps(stored))
    report = abh.backlog_report()
    assert report.stale_real_pending == 1


def test_malformed_json_file_skipped_not_crashed(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not valid json")
    _request()  # one good record alongside the broken one
    classifications = abh.classify_all()
    assert len(classifications) == 1  # broken file silently skipped, never fabricated into a match


def test_repeated_discovery_cycle_does_not_recreate_or_duplicate(monkeypatch, tmp_path):
    """Calling classify_all()/backlog_report() repeatedly must never mutate
    the store or change counts — this module is read-only."""
    _fresh(monkeypatch, tmp_path)
    _request(capability_needed="production_promotion")
    _request(subject="second", job_id="job-2", proposal_id="prop-2", capability_needed="human_review_of_failed_self_correction")
    first = abh.backlog_report()
    second = abh.backlog_report()
    third = abh.backlog_report()
    assert first == second == third
    assert first.total == 2


def test_delegable_for_retirement_excludes_true_founder_gate_and_judgment_needed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    _request(subject="gate", capability_needed="production_promotion", proposal_id="p1")
    _request(subject="synthetic one", finding="synthetic test case", proposal_id="p2",
              capability_needed="human_review_of_failed_self_correction")
    delegable = abh.delegable_for_retirement()
    assert len(delegable) == 1
    assert delegable[0].category == abh.Category.SYNTHETIC_TEST_NOISE


def test_natural_language_summary_reflects_real_counts(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    _request(capability_needed="production_promotion")
    _request(subject="synthetic", finding="synthetic test noise", proposal_id="p2",
              capability_needed="human_review_of_failed_self_correction")
    summary = abh.natural_language_summary()
    assert "1 of those are test/synthetic fixtures" in summary
    assert "1 are genuine Founder-authority gates" in summary


def test_empty_backlog_reports_all_zero_no_fake_work(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    report = abh.backlog_report()
    assert report.total == 0
    assert report.pending_real == 0
    assert abh.delegable_for_retirement() == []


# --- Founder-authorized backlog-hygiene retirement (2026-09-08) -----------

def test_genuine_unresolved_founder_gate_stays_pending(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion", proposal_id=None, job_id=None)
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 0
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "pending_founder_review"


def test_approved_request_remains_approved_untouched_by_hygiene(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion", proposal_id=None, job_id=None)
    founder_request.resolve_founder_decision(rec["escalation_id"], "approved")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 0
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "approved"


def test_denied_request_remains_denied_untouched_by_hygiene(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion", proposal_id=None, job_id=None)
    founder_request.resolve_founder_decision(rec["escalation_id"], "denied")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 0
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "denied"


def test_hygiene_retirement_can_never_mutate_an_approved_record_directly(monkeypatch, tmp_path):
    """Direct guard test on retire_for_backlog_hygiene() itself, not just
    the orchestrator — the hard rule ('APPROVE/DENY history must never be
    rewritten') must hold even if a caller tried to bypass run_backlog_
    hygiene_pass()'s own filtering."""
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="production_promotion", proposal_id=None, job_id=None)
    founder_request.resolve_founder_decision(rec["escalation_id"], "approved")
    try:
        founder_request.retire_for_backlog_hygiene(
            rec["escalation_id"], founder_request.HYGIENE_RETIRED_NONACTIONABLE,
            evidence={"x": "y"}, reason="attempted bypass",
        )
        assert False, "must have raised"
    except ValueError as e:
        assert "already carries a real Founder decision" in str(e)
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "approved"


def test_synthetic_test_only_approval_retired_as_nonactionable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(subject="synthetic run", finding="synthetic test: recovery step",
                    job_id="normal-job", proposal_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["label"] == "HYGIENE_RETIRED_NONACTIONABLE"
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_NONACTIONABLE"
    assert updated["hygiene_retirement"]["authority"] == "delegated_founder_backlog_hygiene_authority_v1"
    assert updated["hygiene_retirement"]["category"] == "HYGIENE_RETIRED_NONACTIONABLE"
    assert updated["hygiene_retirement"]["evidence"]["category"] == "synthetic_test_noise"


def test_already_terminal_job_retired(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="finished-job-x", task="t", requested_by="mrsilent", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("finished-job-x", job_ledger.JobState.COMPLETED)
    rec = _request(job_id="finished-job-x", proposal_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["label"] == "HYGIENE_RETIRED_ALREADY_TERMINAL"
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_ALREADY_TERMINAL"


def test_malformed_invalid_retired(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    escalation_id = "malformedrec0001"
    (d / f"{escalation_id}.json").write_text(json.dumps({
        "escalation_id": escalation_id, "status": "pending_founder_review",
        "created_at": "2026-01-01T00:00:00+00:00", "requested_by": "mrsilent",
        "payload": {"finding": "x"},  # missing capability_needed and affected
    }))
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["label"] == "HYGIENE_RETIRED_INVALID"
    updated = json.loads((d / f"{escalation_id}.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_INVALID"


def test_duplicate_pending_retired_pointing_to_canonical_survivor(monkeypatch, tmp_path):
    """Two genuinely separate pending escalation records sharing the SAME
    fingerprint (constructed directly on disk — request_founder_decision()
    itself already prevents this at create time via its own dedup, so this
    simulates the historical-drift case the classifier defends against)."""
    d = _fresh(monkeypatch, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    shared_fp = "sharedfingerprint01"
    payload = {
        "fingerprint": shared_fp, "subject": "s", "finding": "a real finding",
        "capability_needed": "human_review_of_failed_self_correction",
        "affected": {"proposal_id": None, "job_id": None}, "human_readable": "x",
    }
    (d / "aaaa000000000001.json").write_text(json.dumps({
        "escalation_id": "aaaa000000000001", "status": "pending_founder_review",
        "created_at": "2026-01-01T00:00:00+00:00", "requested_by": "mrsilent", "payload": payload,
    }))
    (d / "bbbb000000000002.json").write_text(json.dumps({
        "escalation_id": "bbbb000000000002", "status": "pending_founder_review",
        "created_at": "2026-01-02T00:00:00+00:00", "requested_by": "mrsilent", "payload": payload,
    }))
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["escalation_id"] == "bbbb000000000002"  # later-created one retired
    updated = json.loads((d / "bbbb000000000002.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_DUPLICATE"
    assert updated["hygiene_retirement"]["canonical_survivor_id"] == "aaaa000000000001"
    survivor = json.loads((d / "aaaa000000000001.json").read_text())
    assert survivor["status"] == "pending_founder_review"  # the earlier one is untouched, still real


def test_already_satisfied_proposal_retired(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create(
        observed_weakness="a real weakness", proposed_upgrade="a real upgrade", risk_score="low",
    )
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROMOTED)
    rec = _request(proposal_id=p.proposal_id, job_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["label"] == "HYGIENE_RETIRED_ALREADY_SATISFIED"
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_ALREADY_SATISFIED"


def test_superseded_proposal_advancement_request_retired(monkeypatch, tmp_path):
    """Narrowly scoped: capability_needed must be a real proposal-
    advancement capability (production_promotion), never a general
    root-cause-review request — see the real live case
    (55273f2985a5a602) this scoping was built specifically to NOT touch."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create(
        observed_weakness="a real weakness", proposed_upgrade="a real upgrade", risk_score="founder_gated",
    )
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED)
    rec = _request(proposal_id=p.proposal_id, job_id=None, capability_needed="production_promotion")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 1
    assert result["retired"][0]["label"] == "HYGIENE_RETIRED_SUPERSEDED"
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "HYGIENE_RETIRED_SUPERSEDED"
    assert updated["hygiene_retirement"]["superseding_reference"] == p.proposal_id


def test_rejected_proposal_with_root_cause_review_request_stays_pending(monkeypatch, tmp_path):
    """The exact real live case that shaped PROPOSAL_SUPERSEDED's narrow
    scope: a rejected proposal does NOT retire a general root-cause-review
    escalation, because rejection doesn't actually resolve that concern."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create(
        observed_weakness="omni_engineer degraded", proposed_upgrade="investigate", risk_score="low",
    )
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED)
    rec = _request(proposal_id=p.proposal_id, job_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    result = abh.run_backlog_hygiene_pass()
    assert result["retired_count"] == 0
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] == "pending_founder_review"


def test_repeated_hygiene_cycle_is_idempotent(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    _request(subject="synthetic run", finding="synthetic test noise", job_id=None, proposal_id=None,
              capability_needed="human_review_of_failed_self_correction")
    first = abh.run_backlog_hygiene_pass()
    second = abh.run_backlog_hygiene_pass()
    third = abh.run_backlog_hygiene_pass()
    assert first["retired_count"] == 1
    assert second["retired_count"] == 0
    assert third["retired_count"] == 0
    report = abh.backlog_report()
    assert report.synthetic_noise == 0  # no longer classified as pending synthetic noise
    assert report.resolved_non_pending == 1  # now correctly recognized as already-retired


def test_downstream_approval_still_works_after_hygiene_pass_runs(monkeypatch, tmp_path):
    """A genuine Founder gate resolves via the EXACT SAME resolve_founder_
    decision() path, completely unaffected by hygiene having run."""
    _fresh(monkeypatch, tmp_path)
    gate = _request(capability_needed="production_promotion", proposal_id="real-prop-1", job_id=None)
    _request(subject="synthetic run", finding="synthetic test noise", job_id=None, proposal_id=None,
              capability_needed="human_review_of_failed_self_correction")
    abh.run_backlog_hygiene_pass()
    resolved = founder_request.resolve_founder_decision(gate["escalation_id"], "approved")
    assert resolved["status"] == "approved"
    assert founder_request.exact_proposal_decision("real-prop-1") == "approved"


def test_retired_hygiene_record_never_counts_as_a_fake_approval(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(subject="synthetic run", finding="synthetic test noise",
                    proposal_id="prop-retired-1", job_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    abh.run_backlog_hygiene_pass()
    updated = json.loads((founder_request.ESCALATIONS_DIR / f"{rec['escalation_id']}.json").read_text())
    assert updated["status"] not in ("approved", "denied")
    assert founder_request.exact_proposal_decision("prop-retired-1") is None


def test_no_duplicate_founder_notification_after_retirement(monkeypatch, tmp_path):
    """A retired record must disappear from list_pending_founder_requests()
    — the exact list any notifier scans — so nothing can re-notify on it."""
    _fresh(monkeypatch, tmp_path)
    _request(subject="synthetic run", finding="synthetic test noise", job_id=None, proposal_id=None,
              capability_needed="human_review_of_failed_self_correction")
    before = len(founder_request.list_pending_founder_requests())
    abh.run_backlog_hygiene_pass()
    after = len(founder_request.list_pending_founder_requests())
    assert before == 1
    assert after == 0


def test_hygiene_retirement_requires_evidence_and_reason(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(subject="synthetic run", finding="synthetic test noise", job_id=None, proposal_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    try:
        founder_request.retire_for_backlog_hygiene(
            rec["escalation_id"], founder_request.HYGIENE_RETIRED_NONACTIONABLE, evidence={}, reason="x",
        )
        assert False, "must have raised"
    except ValueError as e:
        assert "evidence is required" in str(e)
    try:
        founder_request.retire_for_backlog_hygiene(
            rec["escalation_id"], founder_request.HYGIENE_RETIRED_NONACTIONABLE, evidence={"a": 1}, reason="",
        )
        assert False, "must have raised"
    except ValueError as e:
        assert "reason is required" in str(e)


def test_hygiene_retirement_rejects_unknown_category(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = _request(subject="synthetic run", finding="synthetic test noise", job_id=None, proposal_id=None,
                    capability_needed="human_review_of_failed_self_correction")
    try:
        founder_request.retire_for_backlog_hygiene(
            rec["escalation_id"], "approved", evidence={"a": 1}, reason="attempted category smuggle",
        )
        assert False, "must have raised"
    except ValueError as e:
        assert "must be one of" in str(e)


def test_stale_vocabulary_available_for_direct_use(monkeypatch, tmp_path):
    """HYGIENE_RETIRED_STALE is real vocabulary (Founder-requested) even
    though the automatic classifier found zero real orphaned-referent
    cases NOT already covered by NONACTIONABLE (every real one this
    campaign found was also synthetic/test-fixture in origin — see
    approval_backlog_hygiene.py's module docstring history). Proves the
    low-level primitive works for this category directly, honestly
    distinct from claiming the auto-classifier produces it today."""
    _fresh(monkeypatch, tmp_path)
    rec = _request(capability_needed="human_review_of_failed_self_correction", proposal_id=None, job_id=None)
    result = founder_request.retire_for_backlog_hygiene(
        rec["escalation_id"], founder_request.HYGIENE_RETIRED_STALE,
        evidence={"referenced_entity": "orphaned-id-x", "checked": True},
        reason="referenced entity no longer exists in any durable store",
    )
    assert result["status"] == "HYGIENE_RETIRED_STALE"


def test_malformed_record_never_silently_hidden_from_total(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "malformedrec0002.json").write_text(json.dumps({
        "escalation_id": "malformedrec0002", "status": "pending_founder_review",
        "created_at": "2026-01-01T00:00:00+00:00", "requested_by": "mrsilent",
        "payload": {"finding": "x"},
    }))
    report = abh.backlog_report()
    assert report.malformed_invalid == 1
    assert report.total == 1  # counted, never dropped silently
