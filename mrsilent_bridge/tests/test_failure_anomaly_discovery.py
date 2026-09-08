"""Tests for failure_anomaly_discovery.py — Studio-wide Walk-Away, discovery
source #3 (2026-09-08)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import failure_anomaly_discovery as fad
import job_ledger
from evolution import observe
from evolution import proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    obs_dir = tmp_path / "observations"
    jobs_root = tmp_path / "jobs"
    proposals_dir = tmp_path / "proposals"
    monkeypatch.setattr(observe, "OBSERVATIONS_DIR", obs_dir)
    monkeypatch.setattr(job_ledger, "JOBS_ROOT", jobs_root)
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", proposals_dir)
    obs_dir.mkdir(parents=True, exist_ok=True)
    return obs_dir


def _write_obs(obs_dir, observation_id, *, signal_type="repeated_retry", requested_by="device_client",
               task_prefix="do the thing", count=3, created_at=None, linked_proposal_id=None,
               dedupe_key=None, job_id=None):
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    evidence = {"requested_by": requested_by, "task_prefix": task_prefix, "count": count}
    if job_id:
        evidence["job_id"] = job_id
    rec = {
        "observation_id": observation_id,
        "created_at": created_at,
        "signal_type": signal_type,
        "description": f"task starting '{task_prefix}...' was submitted {count} times by '{requested_by}'",
        "evidence": evidence,
        "severity": "medium",
        "suggested_risk_score": "low",
        "linked_proposal_id": linked_proposal_id,
        "dedupe_key": dedupe_key if dedupe_key is not None else [requested_by, task_prefix],
    }
    (obs_dir / f"20260908T000000Z_{observation_id}.json").write_text(json.dumps(rec))
    return rec


def test_test_only_event_detected_by_requested_by_prefix(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="test_media_failure_taxonomy")
    classifications = fad.classify_all()
    assert classifications[0].category == fad.Category.TEST_ONLY_EVENT


def test_real_device_requester_not_flagged_as_test(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="device_client")
    c = fad.classify_all()[0]
    assert c.category != fad.Category.TEST_ONLY_EVENT


def test_word_boundary_does_not_false_positive_on_latest_or_contest(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="latest_uploader", task_prefix="contest winner processing")
    c = fad.classify_all()[0]
    assert c.category != fad.Category.TEST_ONLY_EVENT


def test_synthetic_marker_detected(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="mrsilent", task_prefix="synthetic cross-provider probe")
    c = fad.classify_all()[0]
    assert c.category == fad.Category.TEST_ONLY_EVENT


def test_six_duplicate_retries_of_one_task_same_fingerprint(monkeypatch, tmp_path):
    """The exact live-event shape: one observation, count=6, real requester."""
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="device_client", task_prefix="live sensor session ended", count=6)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.NEW_FAILURE  # no linked proposal/job yet, real, unaddressed


def test_legitimate_separate_retries_different_fingerprints_not_marked_duplicate(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="device_client", task_prefix="task A")
    _write_obs(d, "obs-2", requested_by="device_client", task_prefix="task B")
    classifications = fad.classify_all()
    assert all(c.category != fad.Category.DUPLICATE_RETRY for c in classifications)


def test_repeated_observation_of_same_fingerprint_marked_duplicate_retry(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="device_client", task_prefix="task A",
               created_at="2026-09-01T00:00:00+00:00")
    _write_obs(d, "obs-2", requested_by="device_client", task_prefix="task A",
               created_at="2026-09-02T00:00:00+00:00")
    classifications = fad.classify_all()
    cats = [c.category for c in classifications]
    assert cats[0] == fad.Category.NEW_FAILURE
    assert cats[1] == fad.Category.DUPLICATE_RETRY


def test_recovered_when_underlying_job_completed(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="job-ok", task="t", requested_by="device_client", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("job-ok", job_ledger.JobState.COMPLETED)
    _write_obs(d, "obs-1", requested_by="device_client", job_id="job-ok")
    c = fad.classify_all()[0]
    assert c.category == fad.Category.RECOVERED


def test_terminal_failure_when_underlying_job_failed(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="job-failed", task="t", requested_by="device_client", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("job-failed", job_ledger.JobState.FAILED)
    _write_obs(d, "obs-1", requested_by="device_client", job_id="job-failed")
    c = fad.classify_all()[0]
    assert c.category == fad.Category.TERMINAL_FAILURE


def test_retrying_when_underlying_job_still_active(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    job_ledger.create(job_id="job-active", task="t", requested_by="device_client", sandbox_path=str(tmp_path))
    job_ledger.checkpoint("job-active", job_ledger.JobState.EDITING)
    _write_obs(d, "obs-1", requested_by="device_client", job_id="job-active")
    c = fad.classify_all()[0]
    assert c.category == fad.Category.RETRYING


def test_stale_failure_when_old_and_unaddressed(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=fad.STALE_AFTER_DAYS + 5)).isoformat()
    _write_obs(d, "obs-1", requested_by="device_client", created_at=old)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.STALE_FAILURE


def test_needs_founder_gate_when_proposal_is_promotion_candidate(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weakness", "upgrade", "low")
    p.status = proposal_mod.ProposalStatus.PROMOTION_CANDIDATE
    proposal_mod.save(p)
    _write_obs(d, "obs-1", requested_by="device_client", linked_proposal_id=p.proposal_id)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.NEEDS_FOUNDER_GATE


def test_already_resolved_when_proposal_promoted(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weakness", "upgrade", "low")
    p.status = proposal_mod.ProposalStatus.PROMOTED
    proposal_mod.save(p)
    _write_obs(d, "obs-1", requested_by="device_client", linked_proposal_id=p.proposal_id)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.ALREADY_RESOLVED


def test_terminal_failure_when_proposal_rejected(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weakness", "upgrade", "low")
    p.status = proposal_mod.ProposalStatus.REJECTED
    proposal_mod.save(p)
    _write_obs(d, "obs-1", requested_by="device_client", linked_proposal_id=p.proposal_id)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.TERMINAL_FAILURE


def test_needs_repair_when_proposal_deferred(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weakness", "upgrade", "low")
    p.status = proposal_mod.ProposalStatus.DEFERRED
    proposal_mod.save(p)
    _write_obs(d, "obs-1", requested_by="device_client", linked_proposal_id=p.proposal_id)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.NEEDS_REPAIR


def test_retrying_when_proposal_still_in_progress(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weakness", "upgrade", "low")  # status=OBSERVED by default
    _write_obs(d, "obs-1", requested_by="device_client", linked_proposal_id=p.proposal_id)
    c = fad.classify_all()[0]
    assert c.category == fad.Category.RETRYING


def test_malformed_observation_file_skipped_not_crashed(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    (d / "20260908T000000Z_broken.json").write_text("{not valid json")
    _write_obs(d, "obs-1", requested_by="device_client")
    classifications = fad.classify_all()
    assert len(classifications) == 1


def test_repeated_discovery_pass_is_read_only_and_stable(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="device_client", task_prefix="task A")
    _write_obs(d, "obs-2", requested_by="test_x", task_prefix="task B")
    first = fad.anomaly_report()
    second = fad.anomaly_report()
    assert first == second
    assert first.total == 2
    assert first.test_only == 1


def test_since_bounds_scan_to_recent_observations(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-old", requested_by="device_client", created_at="2026-01-01T00:00:00+00:00")
    _write_obs(d, "obs-new", requested_by="device_client", created_at="2026-09-08T00:00:00+00:00")
    cutoff = datetime(2026, 6, 1, tzinfo=timezone.utc)
    recent = fad.classify_all(since=cutoff)
    assert len(recent) == 1
    assert recent[0].observation_id == "obs-new"


def test_empty_observation_store_reports_all_zero_no_fake_work(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    r = fad.anomaly_report()
    assert r.total == 0


def test_natural_language_summary_reflects_real_counts(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    _write_obs(d, "obs-1", requested_by="test_something")
    _write_obs(d, "obs-2", requested_by="device_client", task_prefix="task B")
    summary = fad.natural_language_summary()
    assert "1 are test-only events" in summary
    assert "1 are real and still active" in summary


# --- classify_all() `since`-filter pre-filter optimization (2026-09-08) ---

def test_since_bounded_scan_matches_unbounded_scan_filtered_manually(monkeypatch, tmp_path):
    """The core correctness invariant: classify_all(since=X) must return
    EXACTLY the same records classify_all(since=None) would, minus those
    whose created_at is determinable and before X — never more, never
    fewer, regardless of the filename-based pre-filter's involvement."""
    d = _fresh(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=5)
    recent = now - timedelta(hours=1)
    _write_obs(d, "obs-old", requested_by="device_client", task_prefix="old task", created_at=old.isoformat())
    _write_obs(d, "obs-recent", requested_by="device_client", task_prefix="recent task", created_at=recent.isoformat())
    since = now - timedelta(days=1)
    bounded = fad.classify_all(since=since)
    assert len(bounded) == 1
    assert bounded[0].observation_id == "obs-recent"


def test_record_with_no_created_at_always_included_regardless_of_filename_age(monkeypatch, tmp_path):
    """The exact real bug this optimization's own verification caught before
    shipping: a record with no created_at (e.g. a differently-shaped
    snapshot file sharing the observations/ directory) must be included in
    EVERY since-bounded query regardless of how old its filename looks —
    matching the pre-existing, unchanged 'only filter what's determinable'
    semantics. A naive filename-only pre-filter would wrongly drop it."""
    d = _fresh(monkeypatch, tmp_path)
    old_ts = "20200101T000000"  # clearly, unambiguously old by filename
    (d / f"{old_ts}Z_report.json").write_text(json.dumps({
        "observation_id": "snapshot-1", "created_at": None, "signal_type": "service_health",
        "description": "a periodic snapshot with no timestamp of its own",
        "evidence": {}, "severity": "low", "suggested_risk_score": "low",
        "linked_proposal_id": None, "dedupe_key": ["snapshot"],
    }))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    bounded = fad.classify_all(since=since)
    assert len(bounded) == 1  # included despite the filename looking ancient


def test_filename_prefilter_never_skips_a_genuinely_recent_obs_record(monkeypatch, tmp_path):
    """_write_obs()'s own filename prefix is fixed regardless of the
    created_at passed in, so this constructs the file directly with a
    filename timestamp that genuinely matches 'now' — the real-world shape
    this pre-filter actually has to get right."""
    d = _fresh(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    now_prefix = now.strftime("%Y%m%dT%H%M%S")
    (d / f"{now_prefix}Z_obs-fresh.json").write_text(json.dumps({
        "observation_id": "obs-fresh", "created_at": now.isoformat(), "signal_type": "repeated_retry",
        "description": "a genuinely fresh real observation", "evidence": {"requested_by": "device_client"},
        "severity": "low", "suggested_risk_score": "low", "linked_proposal_id": None,
        "dedupe_key": ["device_client", "fresh"],
    }))
    since = now - timedelta(minutes=1)
    bounded = fad.classify_all(since=since)
    assert len(bounded) == 1


def test_filename_prefilter_skips_a_confidently_old_obs_record_without_reading_it(monkeypatch, tmp_path):
    d = _fresh(monkeypatch, tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    # Write with a filename timestamp matching the (old) created_at, unlike
    # _write_obs's fixed "20260908T000000Z_" prefix — need a real old prefix
    # for the pre-filter to actually engage.
    old_prefix = old.strftime("%Y%m%dT%H%M%S")
    (d / f"{old_prefix}Z_obs-ancient.json").write_text(json.dumps({
        "observation_id": "obs-ancient", "created_at": old.isoformat(), "signal_type": "repeated_retry",
        "description": "an old real observation", "evidence": {"requested_by": "device_client"},
        "severity": "low", "suggested_risk_score": "low", "linked_proposal_id": None,
        "dedupe_key": ["device_client", "old"],
    }))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    bounded = fad.classify_all(since=since)
    assert len(bounded) == 0
    skip_result = fad._filename_ts_before(d / f"{old_prefix}Z_obs-ancient.json", since)
    assert skip_result is True  # confirms the fast path actually engaged, not just the fallback
