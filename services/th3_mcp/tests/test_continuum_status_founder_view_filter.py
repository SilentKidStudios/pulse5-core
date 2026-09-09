"""Tests for the CONTINUUM_STATUS_FOUNDER_VIEW_FILTER_REPAIR in tools.py.

WHAT THIS CLOSES: TRUE WALK-AWAY V1 rescore (2026-09-09) identified a real
Domain A/E capability defect: continuum_status()'s pending_founder_gated_jobs
listing showed every raw approval_state=="pending_approval" job_ledger
record unfiltered -- mixing real Founder-gated work with accumulated
test/synthetic artifacts (requested_by=="test", "synthetic ..." task text)
-- while natural_language_status()'s own approvals count, founder_view_
filter.py, and the Telegram synthetic-escalation filter already correctly
distinguish real from synthetic using the SAME classify_pending_item()
classifier. continuum_status() now reuses that exact classifier (never a
second, competing one) so every Founder-facing pending listing agrees.

WHAT THIS DELIBERATELY DOES NOT DO: it never mutates job_ledger, proposal,
or workgraph state -- purely a read-time view filter, same guarantee
founder_view_filter.py's own module docstring already makes. It never
marks a suppressed synthetic item approved/rejected/resolved/
notification_sent. It never touches authority_policy's GATED_TOOLS/
GATED_KEYWORDS/GATED_PATH_MARKERS/GATED_ADAPTERS or any other protection.

Run: python3 -m pytest services/th3_mcp/tests/test_continuum_status_founder_view_filter.py -v
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path("/opt/pulse5-core")
sys.path.insert(0, str(ROOT / "services" / "th3_mcp"))
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))

import pytest

import tools as T
import job_ledger
from job_ledger import JobState


def _make_pending_job(*, task: str, requested_by: str) -> str:
    job_id = str(uuid.uuid4())
    job_ledger.create(job_id, task=task, requested_by=requested_by, sandbox_path=str(ROOT / "mrsilent_bridge" / "jobs" / job_id / "workdir"))
    job_ledger.checkpoint(job_id, JobState.AUTHORIZED, risk_class="founder_gated", approval_state="pending_approval")
    return job_id


def _cleanup(job_id: str) -> None:
    # Never deleted -- same soft-terminal convention test_recover_work.py
    # already uses; the ledger record itself is left intact, just moved
    # to a terminal state so it stops appearing as live pending work.
    job_ledger.checkpoint(job_id, JobState.FAILED, terminal_result="test_fixture_cleanup", error_class="synthetic_test")


@pytest.fixture
def real_and_synthetic_jobs():
    real_id = _make_pending_job(
        task="Founder decision needed: approve production-wide rollout of the new pricing tier for paid customers.",
        requested_by="founder_priority_governor",
    )
    synthetic_id = _make_pending_job(
        task="synthetic test: sandbox-only write, no real effect",
        requested_by="test",
    )
    try:
        yield real_id, synthetic_id
    finally:
        _cleanup(real_id)
        _cleanup(synthetic_id)


def _isolated_status(monkeypatch, real_id: str, synthetic_id: str):
    """Isolates continuum_status()'s view of job_ledger to exactly these
    two records -- thousands of other real, already-live pending_approval
    records exist on this host and would otherwise push either one out of
    the existing top-10 display cap purely by volume/ordering, unrelated
    to whether the filter itself is correct. Only job_ledger.list_all()'s
    return value is patched; the filtering logic under test (founder_view_
    filter.classify_pending_item, via the real, unmocked continuum_status())
    runs exactly as it does in production."""
    real_record = job_ledger.load(real_id)
    synthetic_record = job_ledger.load(synthetic_id)
    monkeypatch.setattr(job_ledger, "list_all", lambda: [real_record, synthetic_record])
    return T.continuum_status()


def test_real_founder_item_appears(real_and_synthetic_jobs, monkeypatch):
    real_id, synthetic_id = real_and_synthetic_jobs
    status = _isolated_status(monkeypatch, real_id, synthetic_id)
    ids = {item["work_id"] for item in status["pending_founder_gated_jobs"]}
    assert real_id in ids, "a real Founder-gated item must remain visible in the filtered listing"


def test_synthetic_item_does_not_appear(real_and_synthetic_jobs, monkeypatch):
    real_id, synthetic_id = real_and_synthetic_jobs
    status = _isolated_status(monkeypatch, real_id, synthetic_id)
    ids = {item["work_id"] for item in status["pending_founder_gated_jobs"]}
    assert synthetic_id not in ids, "a synthetic/test-only item must be excluded from the Founder-facing listing"


def test_underlying_canonical_item_unchanged(real_and_synthetic_jobs):
    real_id, synthetic_id = real_and_synthetic_jobs
    before_real = job_ledger.load(real_id)
    before_synthetic = job_ledger.load(synthetic_id)
    T.continuum_status()  # the read that performs the filtering
    after_real = job_ledger.load(real_id)
    after_synthetic = job_ledger.load(synthetic_id)

    for before, after, label in ((before_real, after_real, "real"), (before_synthetic, after_synthetic, "synthetic")):
        assert after.approval_state == before.approval_state == "pending_approval", \
            f"{label} item's approval_state must be unchanged by a mere read (never approved/rejected/resolved as a side effect)"
        assert after.state == before.state, f"{label} item's ledger state must be unchanged"
        assert after.risk_class == before.risk_class, f"{label} item's risk_class must be unchanged"
        assert getattr(after, "notification_sent", None) == getattr(before, "notification_sent", None), \
            f"{label} item must never be marked notification_sent merely because it was filtered from a view"


def test_other_continuum_status_output_unaffected(real_and_synthetic_jobs):
    status = T.continuum_status()
    for key in (
        "canonical_authority", "governing_priority", "active_work_summary",
        "latest_completed_job", "runtime_health_summary",
        "next_actionable_ordinary_work", "external_blockers", "note",
    ):
        assert key in status, f"continuum_status() must still return '{key}' unaffected by this filter"
    assert isinstance(status["pending_founder_gated_jobs"], list)
    assert len(status["pending_founder_gated_jobs"]) <= 10, "the existing top-10 display cap must be preserved"


def test_filter_reuses_the_canonical_classifier_not_a_new_one():
    """Structural guard against a second, competing classifier: tools.py
    must call the SAME founder_view_filter.classify_pending_item()/
    CATEGORY_REAL_ACTIONABLE this module already exports, not a locally
    reimplemented copy."""
    import inspect
    from evolution import founder_view_filter
    src = inspect.getsource(T.continuum_status)
    assert "founder_view_filter.classify_pending_item" in src
    assert "founder_view_filter.CATEGORY_REAL_ACTIONABLE" in src
    assert callable(founder_view_filter.classify_pending_item)
