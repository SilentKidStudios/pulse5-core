"""Tests for omniforge.py — Domain-D failure-taxonomy gap-closure
(2026-09-08): a real, live incident found where 2 real integrate() calls
saw job.status=="duplicate_suppressed" (a concurrent-dispatch guard,
never a content failure — see omniengineer_harness._duplicate_result())
and were routed through the exact same "integration failed" path as a
genuine deterministic failure: a high-confidence negative experience
record and a permanent proposal REJECTED, even though the referenced job
is someone else's still-active attempt at the identical task."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import black_vault
import omniforge
from evolution import proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(proposal_mod, "NEW_PROPOSAL_SIGNAL_PATH", tmp_path / "new_proposal_signal")
    monkeypatch.setattr(black_vault, "VAULT_ROOT", tmp_path / "black_vault")


def _seed_forge_eligible_record(quarantine_id: str) -> black_vault.QuarantineRecord:
    rec = black_vault.QuarantineRecord(
        quarantine_id=quarantine_id, source_identifier="pkg:example-lib",
        name="example-lib", origin="test", created_at="2026-09-08T00:00:00+00:00",
        requested_by="test", verdict="recommended_for_forge_review",
    )
    black_vault._atomic_write(black_vault._path(quarantine_id), {
        "quarantine_id": rec.quarantine_id, "source_identifier": rec.source_identifier,
        "name": rec.name, "origin": rec.origin, "created_at": rec.created_at,
        "requested_by": rec.requested_by, "evaluation": {}, "verdict": rec.verdict, "reasoning": [],
    })
    return rec


@dataclass
class _FakeJobResult:
    job_id: str
    status: str
    workdir: str = "/tmp/x"
    files_changed: dict = field(default_factory=dict)
    policy_reasons: list = field(default_factory=list)
    promotion_eligible: bool = False
    adapter: str = "omni_engineer"


def test_duplicate_suppressed_is_never_treated_as_a_failure(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    _seed_forge_eligible_record("q1")
    existing_job_id = "already-in-flight-job"

    def _fake_submit_job(**kwargs):
        return _FakeJobResult(job_id=existing_job_id, status="duplicate_suppressed")

    monkeypatch.setattr(omniforge.omniengineer_harness, "submit_job", _fake_submit_job)
    experience_calls = []
    monkeypatch.setattr(proposal_mod, "record_experience",
                         lambda *a, **kw: experience_calls.append((a, kw)))

    result = omniforge.integrate("q1", requested_by="test")

    assert result.status == "duplicate_in_flight"
    assert result.job_id == existing_job_id
    assert experience_calls == [], "must never write a negative-confidence experience record for a non-failure"

    p = proposal_mod.load(result.proposal_id)
    assert p.status != proposal_mod.ProposalStatus.REJECTED, (
        "a duplicate-suppressed attempt must never permanently reject the proposal — "
        "the referenced job is still genuinely in flight and may succeed"
    )


def test_genuine_failure_is_still_rejected_with_a_real_experience_record(monkeypatch, tmp_path):
    """Regression guard: the new duplicate_suppressed branch must not
    weaken handling of an actual content failure."""
    _fresh(monkeypatch, tmp_path)
    _seed_forge_eligible_record("q2")

    def _fake_submit_job(**kwargs):
        return _FakeJobResult(job_id="real-attempt", status="failed")

    monkeypatch.setattr(omniforge.omniengineer_harness, "submit_job", _fake_submit_job)

    result = omniforge.integrate("q2", requested_by="test")

    assert result.status == "failed"
    p = proposal_mod.load(result.proposal_id)
    assert p.status == proposal_mod.ProposalStatus.REJECTED
    assert p.history[-1]["note"] == "omniforge integration attempt failed: omniengineer status='failed'"
