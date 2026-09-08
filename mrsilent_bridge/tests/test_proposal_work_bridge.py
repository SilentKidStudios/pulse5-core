"""Tests for proposal_work_bridge.py — the live WorkItem population +
executor handoff built during the WALK-AWAY CONVERGENCE live-integration
phase (2026-09-04).

evolution/proposal.py and evolution/founder_request.py are both fully
self-contained (zero non-stdlib imports) and are exercised here for REAL —
these tests do not mock or stub either module. evolution/advance.py (the
real executor) is dependency-injected as a fake stand-in matching its real
AdvancementResult field names, since its own untracked transitive
dependencies cannot be imported from this isolated worktree — see
proposal_work_bridge.py's module docstring for the exact untracked files
and why that's a structural fact, not a testing shortcut.
"""
from __future__ import annotations

from dataclasses import dataclass

import proposal_work_bridge as bridge
import scheduler as sched
import dispatch_admission as da
import session_ownership as so
import work_graph as wg
from evolution import founder_request
from evolution import proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    # TEST_LIVE_STORE_CONTAMINATION fix (2026-09-08): STATE_INDEX_DIR is a
    # THIRD WORK_GRAPH_ROOT-derived sibling of ITEMS_DIR/LOCKS_DIR, added
    # after this fixture existed and never patched here — every state
    # transition this file's tests exercise (mark_repairable_failed() in
    # particular) was writing real zero-byte marker files straight into
    # the LIVE production work_graph_state/state_index/ tree even though
    # the item itself only ever existed under tmp_path, leaving permanent
    # orphaned entries with no backing item or proposal (real, live
    # evidence: proposal:55984889-c0f9-49a9-a786-814124d5176b, a dangling
    # REPAIRABLE_FAILED index marker with zero corresponding data, created
    # by this file's own test_repair_budget_exhaustion_blocked_founder_is_
    # not_auto_released).
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(proposal_mod, "NEW_PROPOSAL_SIGNAL_PATH", tmp_path / "new_proposal_signal")
    monkeypatch.setattr(founder_request, "ESCALATIONS_DIR", tmp_path / "escalations")
    monkeypatch.setattr(so, "LEASES_DIR", tmp_path / "leases")


def _complete_founder_gated(weakness: str, upgrade: str) -> proposal_mod.Proposal:
    """PROPOSAL COMPLETENESS GATE (2026-09-07): a bare founder_gated
    proposal (no scope/validation/canary declared) is no longer surfaced as
    BLOCKED_FOUNDER at all — see test_incomplete_founder_gated_proposal_is_
    blocked_refinement_not_founder below for that new behavior. Tests in
    this file whose actual intent is to exercise genuine Founder-gate
    mechanics (approval/denial/release) use this helper to build a
    decision-ready proposal instead, matching what a REAL reviewable
    founder_gated proposal looks like."""
    p = proposal_mod.create(weakness, upgrade, risk_score="founder_gated")
    return proposal_mod.refine(
        p.proposal_id,
        implementation_scope="test fixture: bounded, self-contained change under test",
        non_file_scope="test fixture — no real canonical files touched",
        validation_plan="test fixture: validation.validate() on the sandbox output",
        canary_plan="test fixture: independent re-validation pass",
        paid_resources_required=False, credential_changes_required=False,
        production_promotion_required=False, destructive_action_required=False,
        model_change_required=False, isolation_change_required=False, campaign_collision=False,
    )


def _healthy_vector() -> da.ResourceVector:
    return da.ResourceVector(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
        psi_memory_avg10=0.0, oom_kill_recent=False,  # DIRECT_PSI_OOM_GATE (2026-09-08): healthy baseline
    )


@dataclass
class _FakeAdvancementResult:
    """Stand-in for evolution.advance.AdvancementResult with identical
    field names (verified against evolution/advance.py's real dataclass,
    2026-09-04) — see proposal_work_bridge.py's module docstring for why
    the real one cannot be imported here."""
    proposal_id: str
    final_status: str
    blocked_reason: str | None
    implementation_job_id: str | None = None
    selected_engine: str | None = None


# ---- population -------------------------------------------------------

def test_open_proposal_gets_a_runnable_workitem(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["created"] == 1
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item is not None
    assert item.state == wg.WorkState.RUNNABLE
    assert item.provenance["proposal_id"] == p.proposal_id


def test_closed_proposal_gets_no_workitem(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED, note="test")

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["created"] == 0
    assert wg.load(bridge.workitem_id_for_proposal(p.proposal_id)) is None


def test_founder_gated_proposal_without_decision_is_blocked_founder(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = _complete_founder_gated("risky change", "do it")

    bridge.upsert_workitems_from_proposals()
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item.state == wg.WorkState.BLOCKED_FOUNDER


def test_incomplete_founder_gated_proposal_is_blocked_refinement_not_founder(monkeypatch, tmp_path):
    """PROPOSAL COMPLETENESS GATE (2026-09-07): a bare founder_gated
    proposal with no scope/validation/canary/protected-action fields
    declared — the real, live shape of proposal 8d0d01cb-d2f5-4638-b5f5-
    3b3945de07bf — must never be surfaced as BLOCKED_FOUNDER: there is
    nothing yet in it for a Founder to meaningfully review."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("risky change", "do it", risk_score="founder_gated")

    bridge.upsert_workitems_from_proposals()
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item.state == wg.WorkState.BLOCKED_REFINEMENT
    assert item not in wg.runnable_items()


def test_refining_an_incomplete_founder_gated_proposal_promotes_it_to_blocked_founder(monkeypatch, tmp_path):
    """The REFINE step: once evolution/proposal.py::refine() makes a
    previously-incomplete founder_gated proposal decision-ready, the very
    next population pass alone promotes it from BLOCKED_REFINEMENT to the
    real Founder-visible BLOCKED_FOUNDER — no manual WorkItem edit."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("risky change", "do it", risk_score="founder_gated")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_REFINEMENT

    proposal_mod.refine(
        p.proposal_id,
        implementation_scope="bounded scope", non_file_scope="n/a",
        validation_plan="validate", canary_plan="canary",
        paid_resources_required=False, credential_changes_required=False,
        production_promotion_required=False, destructive_action_required=False,
        model_change_required=False, isolation_change_required=False, campaign_collision=False,
    )
    bridge.upsert_workitems_from_proposals()
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_FOUNDER


def test_founder_approved_proposal_is_runnable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = _complete_founder_gated("risky change", "do it")
    escalation = founder_request.request_founder_decision(
        subject=p.proposal_id, finding="risky change needed", capability_needed="approve proposal",
        reason_required="founder_gated risk", recommended_action="advance the proposal",
        risk="founder_gated", affected={"proposal_id": p.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")

    bridge.upsert_workitems_from_proposals()
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item.state == wg.WorkState.RUNNABLE


def test_population_is_idempotent_across_repeated_cycles(monkeypatch, tmp_path):
    """Same canonical work does not duplicate WorkItems across cycles."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")

    for _ in range(5):
        bridge.upsert_workitems_from_proposals()

    assert len(wg.list_all()) == 1
    matching = [r for r in wg.list_all() if r.provenance.get("proposal_id") == p.proposal_id]
    assert len(matching) == 1


def test_population_never_resets_a_running_item(monkeypatch, tmp_path):
    """A cycle firing while a proposal's WorkItem is mid-execution must
    never re-arm it back to RUNNABLE — that would be a duplicate dispatch."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    wg.claim(work_id, owner="worker-a")
    assert wg.load(work_id).state == wg.WorkState.RUNNING

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["left_untouched"] == 1
    assert wg.load(work_id).state == wg.WorkState.RUNNING


def test_population_never_resets_a_completed_item(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    wg.mark_completed(work_id, result={"ok": True})

    bridge.upsert_workitems_from_proposals()
    assert wg.load(work_id).state == wg.WorkState.COMPLETED


def test_founder_decision_resolved_later_releases_the_workitem_naturally(monkeypatch, tmp_path):
    """No manual step required: a later cycle's population pass alone
    lifts BLOCKED_FOUNDER once a real decision is recorded."""
    _fresh(monkeypatch, tmp_path)
    p = _complete_founder_gated("risky change", "do it")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_FOUNDER

    escalation = founder_request.request_founder_decision(
        subject=p.proposal_id, finding="risky change needed", capability_needed="approve proposal",
        reason_required="founder_gated risk", recommended_action="advance the proposal",
        risk="founder_gated", affected={"proposal_id": p.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")

    bridge.upsert_workitems_from_proposals()
    assert wg.load(work_id).state == wg.WorkState.RUNNABLE


def test_proposal_orphaned_at_canary_is_never_dispatched_runnable(monkeypatch, tmp_path):
    """ORPHANED_STATUS gap-closure (2026-09-08): a proposal left at
    status=canary by an interrupted prior advance() call — the real,
    live shape of proposals 83c43e59-fcfb-4b3f-ab0c-29f2dba8d17b and
    c3b3c10a-dabf-4b92-923b-6039cbb67ad3 — must never get a RUNNABLE
    WorkItem: advance_one() can only start from observed/proposed, so
    dispatching it just fails the identical deterministic way forever."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY)

    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    item = wg.load(work_id)
    assert item.state == wg.WorkState.BLOCKED_FOUNDER
    assert item.provenance.get("blocked_founder_source") == "orphaned_status"
    assert item not in wg.runnable_items()


def test_orphaned_canary_is_reconciled_via_injected_fn_before_classification(monkeypatch, tmp_path):
    """CANARY_RECONCILIATION gap-closure (2026-09-08): the population pass
    must attempt reconciliation BEFORE falling back to the (permanent)
    orphaned_status BLOCKED_FOUNDER classification — a proposal a real
    reconcile_orphaned_canary() successfully resumes must show up here as
    promotion_candidate, not stuck forever. reconcile_orphaned_canary_fn is
    injected exactly like make_advance_executor()'s advance_mod, for the
    identical untracked-dependency reason (see module docstring)."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note="canary passed (fake)")

    def fake_reconcile(proposal_id: str) -> _FakeAdvancementResult:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.PROMOTION_CANDIDATE, note="fake reconciliation")
        return _FakeAdvancementResult(proposal_id=proposal_id, final_status=proposal_mod.ProposalStatus.PROMOTION_CANDIDATE,
                                       blocked_reason=None)

    counts = bridge.upsert_workitems_from_proposals(reconcile_orphaned_canary_fn=fake_reconcile)
    assert counts["reconciled_canary"] == 1
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    item = wg.load(work_id)
    # promotion_candidate is STILL not observed/proposed, so it correctly
    # parks in BLOCKED_FOUNDER/orphaned_status too -- awaiting an actual
    # human --founder-approved promotion, never re-dispatched. The
    # difference from before reconciliation is durable: the proposal's own
    # canonical status genuinely advanced, and a real Founder communication
    # now exists for it (proven at the evolution/advance.py level in
    # tests/test_canary_reconciliation.py).
    assert item.state == wg.WorkState.BLOCKED_FOUNDER
    assert item.provenance.get("blocked_founder_source") == "orphaned_status"
    assert proposal_mod.load(p.proposal_id).status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE


def test_reconciliation_noop_result_still_classifies_as_orphaned_status(monkeypatch, tmp_path):
    """When reconciliation cannot proceed (fn reports the proposal is
    unchanged), the existing orphaned_status BLOCKED_FOUNDER classification
    must still apply exactly as before this fix — no regression for the
    cases reconciliation correctly declines to touch."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY)

    def fake_reconcile_declines(proposal_id: str) -> _FakeAdvancementResult:
        return _FakeAdvancementResult(proposal_id=proposal_id, final_status=proposal_mod.ProposalStatus.CANARY,
                                       blocked_reason="canary_status_unverified")

    counts = bridge.upsert_workitems_from_proposals(reconcile_orphaned_canary_fn=fake_reconcile_declines)
    assert counts["reconciled_canary"] == 0
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    item = wg.load(work_id)
    assert item.state == wg.WorkState.BLOCKED_FOUNDER
    assert item.provenance.get("blocked_founder_source") == "orphaned_status"
    assert proposal_mod.load(p.proposal_id).status == proposal_mod.ProposalStatus.CANARY


def test_orphaned_status_blocked_founder_is_stable_across_repeated_population(monkeypatch, tmp_path):
    """No flicker: re-running population every cycle must never bounce an
    orphaned-status item back to RUNNABLE the way a real founder-gate
    resolution would — the proposal's own status hasn't changed."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED)
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY)
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)

    for _ in range(5):
        bridge.upsert_workitems_from_proposals()
        assert wg.load(work_id).state == wg.WorkState.BLOCKED_FOUNDER


def test_repair_budget_exhaustion_blocked_founder_is_not_auto_released(monkeypatch, tmp_path):
    """RELEASE-CONFLATION fix (2026-09-08): a BLOCKED_FOUNDER caused by
    work_graph.retry_repairable_failed() exhausting its repair budget on a
    genuinely non-founder_gated proposal must NOT be silently released by
    the next population pass just because this proposal was never
    founder-gated to begin with — that conflation is exactly what turned
    a real escalation into an infinite retry storm (proposals 83c43e59...
    /c3b3c10a...). Only a real founder-gate release stays automatic."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert wg.load(work_id).state == wg.WorkState.RUNNABLE

    # MAX_REPAIR_ATTEMPTS blind retries succeed in requeuing; the
    # (MAX_REPAIR_ATTEMPTS + 1)-th failure is what actually trips the
    # budget-exhausted -> BLOCKED_FOUNDER escalation in retry_repairable_
    # failed() (repair_attempts >= MAX_REPAIR_ATTEMPTS).
    for _attempt in range(wg.MAX_REPAIR_ATTEMPTS + 1):
        wg.mark_repairable_failed(work_id, result={"error_class": "RuntimeError", "error": "boom"})
        record = wg.load(work_id)
        record.next_retry_at = ""  # backoff already elapsed, for this test's purposes
        wg._save(record)
        wg.retry_repairable_failed()

    final = wg.load(work_id)
    assert final.state == wg.WorkState.BLOCKED_FOUNDER
    assert final.provenance.get("blocked_founder_source") == "repair_budget_exhausted"

    bridge.upsert_workitems_from_proposals()
    still = wg.load(work_id)
    assert still.state == wg.WorkState.BLOCKED_FOUNDER, (
        "a repair-budget escalation must never be auto-released just because "
        "this proposal's own founder-gate check is unrelated/False"
    )


def test_closed_proposal_reconciles_a_stale_blocked_workitem_to_terminal_failed(monkeypatch, tmp_path):
    """DOMAIN_F_TRUTHFULNESS gap-closure (2026-09-08): real, live evidence —
    founder_request.retire_for_backlog_hygiene() closes a proposal directly
    (evolution/proposal.py::advance()) with zero WorkGraph awareness, so a
    WorkItem parked BLOCKED_FOUNDER before that closure was frozen there
    forever (population's own `if p.status in CLOSED_STATUSES: continue`
    never touches it again) — a stale WorkItem-state-vs-proposal-status
    mismatch. Real cases this closed: 4 real WorkItems stuck at
    blocked_founder backing proposals that had already reached a final
    REJECTED verdict via exactly this path."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    wg.mark_blocked_founder(work_id, note="test", source="orphaned_status")
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_FOUNDER

    # Closes the proposal via a path that never touches the WorkGraph at
    # all — exactly what retire_for_backlog_hygiene() does in the real
    # incident this test is grounded in.
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED, note="closed outside WorkGraph")

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["reconciled_terminal"] == 1
    reconciled = wg.load(work_id)
    assert reconciled.state == wg.WorkState.TERMINAL_FAILED


def test_promoted_proposal_reconciles_a_stale_workitem_to_completed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    wg.mark_blocked_founder(work_id, note="test", source="orphaned_status")

    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROMOTED, note="promoted outside WorkGraph")

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["reconciled_terminal"] == 1
    assert wg.load(work_id).state == wg.WorkState.COMPLETED


def test_terminal_reconciliation_is_idempotent_and_never_touches_running(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p1 = proposal_mod.create("weak spot 1", "fix it", risk_score="low")
    p2 = proposal_mod.create("weak spot 2", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id1 = bridge.workitem_id_for_proposal(p1.proposal_id)
    work_id2 = bridge.workitem_id_for_proposal(p2.proposal_id)
    wg.claim(work_id2, owner="worker-a")  # RUNNING — must never be force-terminated

    proposal_mod.advance(p1.proposal_id, proposal_mod.ProposalStatus.REJECTED, note="closed")
    proposal_mod.advance(p2.proposal_id, proposal_mod.ProposalStatus.REJECTED, note="closed while its WorkItem is genuinely RUNNING")

    first = bridge.upsert_workitems_from_proposals()
    assert first["reconciled_terminal"] == 1  # only work_id1 — work_id2 is RUNNABLE->claimed, never blocked_founder-parked
    assert wg.load(work_id1).state == wg.WorkState.TERMINAL_FAILED
    assert wg.load(work_id2).state == wg.WorkState.RUNNING, "a genuinely RUNNING item must never be force-terminated"

    second = bridge.upsert_workitems_from_proposals()
    assert second["reconciled_terminal"] == 0, "already-reconciled terminal state must be idempotent, never re-touched"


def test_owner_path_from_source_paths_enables_collision_protection(monkeypatch, tmp_path):
    """An owned/conflicting path is not concurrently dispatched: a proposal
    naming a source_path that collides with an externally-owned session
    lease must not be scheduled."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("edit shared file", "fix it", risk_score="low",
                             source_paths=["mrsilent_bridge/evolution/advance.py"])
    so.register("peer-session", "peer-campaign", ["mrsilent_bridge/evolution"])

    bridge.upsert_workitems_from_proposals()
    runnable = wg.runnable_items(self_session_id="this-session")
    assert bridge.workitem_id_for_proposal(p.proposal_id) not in {r.work_id for r in runnable}
    assert wg.load(bridge.workitem_id_for_proposal(p.proposal_id)).state == wg.WorkState.BLOCKED_EXTERNAL_SESSION


def test_multiple_independent_proposals_all_become_runnable_together(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ids = [proposal_mod.create(f"weak spot {i}", "fix it", risk_score="low").proposal_id for i in range(5)]
    bridge.upsert_workitems_from_proposals()
    runnable_ids = {r.work_id for r in wg.runnable_items()}
    for pid in ids:
        assert bridge.workitem_id_for_proposal(pid) in runnable_ids


def test_founder_blocked_proposal_does_not_block_unrelated_runnable_ones(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    gated = proposal_mod.create("risky", "do it", risk_score="founder_gated")
    normal = proposal_mod.create("normal", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()

    runnable_ids = {r.work_id for r in wg.runnable_items()}
    assert bridge.workitem_id_for_proposal(normal.proposal_id) in runnable_ids
    assert bridge.workitem_id_for_proposal(gated.proposal_id) not in runnable_ids


# ---- executor / result translation -------------------------------------

def test_successful_advancement_completes_the_correct_workitem(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()

    class _FakeAdvanceMod:
        def advance_one(self, proposal_id, *, requested_by):
            return _FakeAdvancementResult(proposal_id=proposal_id, final_status="promotion_candidate",
                                           blocked_reason=None, implementation_job_id="job-1", selected_engine="claude_code")

    executor = bridge.make_advance_executor(advance_mod=_FakeAdvanceMod())
    result = sched.run_scheduler_pass(self_session_id="self", executor_fn=executor, resource_vector=_healthy_vector())

    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert work_id in result.completed
    assert wg.load(work_id).state == wg.WorkState.COMPLETED


def test_rejected_advancement_terminally_fails_the_workitem_not_repairable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("bad idea", "do it", risk_score="low")
    bridge.upsert_workitems_from_proposals()

    class _FakeAdvanceMod:
        def advance_one(self, proposal_id, *, requested_by):
            return _FakeAdvancementResult(proposal_id=proposal_id, final_status="rejected",
                                           blocked_reason="validation failed")

    executor = bridge.make_advance_executor(advance_mod=_FakeAdvanceMod())
    result = sched.run_scheduler_pass(self_session_id="self", executor_fn=executor, resource_vector=_healthy_vector())

    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert work_id in result.terminal_failed
    assert wg.load(work_id).state == wg.WorkState.TERMINAL_FAILED


def test_transient_blocked_reason_enters_governed_repair_state(monkeypatch, tmp_path):
    """A failed executor result enters governed retry/repair state, and
    the existing bounded retry_repairable_failed() budget applies."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("needs another attempt", "do it", risk_score="low")
    bridge.upsert_workitems_from_proposals()

    class _FakeAdvanceMod:
        def advance_one(self, proposal_id, *, requested_by):
            return _FakeAdvancementResult(proposal_id=proposal_id, final_status="proposed",
                                           blocked_reason="no_engine_available_this_attempt")

    executor = bridge.make_advance_executor(advance_mod=_FakeAdvanceMod())
    result = sched.run_scheduler_pass(self_session_id="self", executor_fn=executor, resource_vector=_healthy_vector())

    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert work_id in result.repairable_failed
    assert wg.load(work_id).state == wg.WorkState.REPAIRABLE_FAILED
    assert wg.load(work_id).next_retry_at != ""  # governed backoff applies, not an immediate blind retry


def test_workitem_with_no_proposal_id_is_rejected_not_silently_completed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("not-a-proposal", kind="task", description="unrelated")

    executor = bridge.make_advance_executor(advance_mod=object())  # never actually called
    try:
        executor(wg.load("not-a-proposal"))
        assert False, "expected NotAProposalWorkItem"
    except bridge.NotAProposalWorkItem:
        pass


def test_restart_reload_preserves_durable_state_no_duplication(monkeypatch, tmp_path):
    """Simulates a process restart: fresh module-level state, same on-disk
    files. Re-running population against the same durable proposal/
    work_graph directories must not duplicate or lose anything."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    wg.claim(work_id, owner="worker-a")
    wg.release(work_id, owner="worker-a")
    wg.mark_completed(work_id, result={"ok": True})

    # "restart": nothing in-memory carries over except the durable files
    # already on disk (tmp_path) -- re-running population must see exactly
    # the same, single, completed record.
    counts = bridge.upsert_workitems_from_proposals()
    assert counts == {"created": 0, "updated": 0, "left_untouched": 1, "reconciled_canary": 0, "reconciled_terminal": 0}
    assert len(wg.list_all()) == 1
    assert wg.load(work_id).state == wg.WorkState.COMPLETED


# ---- DEFERRED_CHURN gap-closure (2026-09-06) ---------------------------
# Cases A-H exactly as specified: actionable/gated/deferred/terminal
# proposals each materialize into the correct WorkItem state; a deferred
# proposal never becomes runnable churn but naturally re-opens once its
# canonical status changes; population stays idempotent; and DEFERRED work
# never consumes retry budget or a scheduler dispatch pass.

def test_case_A_actionable_proposal_materializes_runnable_workitem(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    counts = bridge.upsert_workitems_from_proposals()
    assert counts["created"] == 1
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item.state == wg.WorkState.RUNNABLE
    assert item in wg.runnable_items()


def test_case_B_founder_gated_proposal_remains_gated(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = _complete_founder_gated("risky change", "do it")
    bridge.upsert_workitems_from_proposals()
    item = wg.load(bridge.workitem_id_for_proposal(p.proposal_id))
    assert item.state == wg.WorkState.BLOCKED_FOUNDER
    assert item not in wg.runnable_items()


def test_case_C_deferred_proposal_never_becomes_runnable_churn(monkeypatch, tmp_path):
    """The exact real defect this fix closes: a DEFERRED proposal used to
    get a RUNNABLE WorkItem, get dispatched, get rejected by advance_one()
    (which never accepts DEFERRED as an eligible starting status), and
    churn through REPAIRABLE_FAILED/retry forever. It must now park in
    BLOCKED_DEFERRED and never appear in runnable_items() at all."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("proactive scouting finding", "n/a", risk_score="low")
    p = proposal_mod.defer(p.proposal_id, until="indefinite", reason="not auto-actionable yet")

    counts = bridge.upsert_workitems_from_proposals()
    assert counts["created"] == 1
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    item = wg.load(work_id)
    assert item.state == wg.WorkState.BLOCKED_DEFERRED
    assert item not in wg.runnable_items()

    ok, reason = advance_mod_eligible_stub(p)
    assert ok is False and "deferred" in reason


def test_case_D_deferred_proposal_becomes_actionable_later_when_status_changes(monkeypatch, tmp_path):
    """No manual WorkItem step required: a later population pass alone
    releases BLOCKED_DEFERRED once the canonical proposal status is
    explicitly changed away from DEFERRED -- mirrors the existing
    Founder-decision-release pattern exactly."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("proactive scouting finding", "n/a", risk_score="low")
    proposal_mod.defer(p.proposal_id, until="indefinite", reason="not auto-actionable yet")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_DEFERRED

    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED,
                          note="deliberate human/reactive un-defer")

    bridge.upsert_workitems_from_proposals()
    assert wg.load(work_id).state == wg.WorkState.RUNNABLE


def test_case_D2_deferred_and_founder_gated_releases_to_blocked_founder_not_runnable(monkeypatch, tmp_path):
    """A un-deferred proposal that is ALSO still founder_gated-and-
    unapproved must release into BLOCKED_FOUNDER, never straight to
    RUNNABLE -- the two gates are independent axes, both must clear."""
    _fresh(monkeypatch, tmp_path)
    p = _complete_founder_gated("risky + deferred", "do it")
    proposal_mod.defer(p.proposal_id, until="indefinite", reason="park for now")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_DEFERRED

    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.PROPOSED, note="un-defer")
    bridge.upsert_workitems_from_proposals()
    assert wg.load(work_id).state == wg.WorkState.BLOCKED_FOUNDER  # still gated, not runnable


def test_case_E_terminal_proposal_does_not_dispatch(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("weak spot", "fix it", risk_score="low")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED, note="test")
    counts = bridge.upsert_workitems_from_proposals()
    assert counts["created"] == 0
    assert wg.load(bridge.workitem_id_for_proposal(p.proposal_id)) is None


def test_case_F_repeated_population_remains_idempotent_for_deferred(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("proactive scouting finding", "n/a", risk_score="low")
    proposal_mod.defer(p.proposal_id, until="indefinite", reason="park")
    first = bridge.upsert_workitems_from_proposals()
    assert first == {"created": 1, "updated": 0, "left_untouched": 0, "reconciled_canary": 0, "reconciled_terminal": 0}
    second = bridge.upsert_workitems_from_proposals()
    assert second == {"created": 0, "updated": 0, "left_untouched": 1, "reconciled_canary": 0, "reconciled_terminal": 0}
    third = bridge.upsert_workitems_from_proposals()
    assert third == {"created": 0, "updated": 0, "left_untouched": 1, "reconciled_canary": 0, "reconciled_terminal": 0}


def test_case_G_no_duplicate_workitems_for_deferred_proposal(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("proactive scouting finding", "n/a", risk_score="low")
    proposal_mod.defer(p.proposal_id, until="indefinite", reason="park")
    for _ in range(5):
        bridge.upsert_workitems_from_proposals()
    assert len(wg.list_all()) == 1


def test_case_H_no_retry_budget_consumed_by_permanently_ineligible_deferred_work(monkeypatch, tmp_path):
    """BLOCKED_DEFERRED is never RUNNABLE, so it's never dispatched, never
    becomes REPAIRABLE_FAILED, and retry_repairable_failed()'s bounded
    backoff/budget machinery never even sees it -- zero retry attempts
    consumed for work that can never actually advance."""
    _fresh(monkeypatch, tmp_path)
    p = proposal_mod.create("proactive scouting finding", "n/a", risk_score="low")
    proposal_mod.defer(p.proposal_id, until="indefinite", reason="park")
    bridge.upsert_workitems_from_proposals()
    work_id = bridge.workitem_id_for_proposal(p.proposal_id)

    requeued = wg.retry_repairable_failed()
    assert work_id not in requeued
    item = wg.load(work_id)
    assert item.state == wg.WorkState.BLOCKED_DEFERRED
    assert item.repair_attempts == 0
    assert item not in wg.runnable_items()


def advance_mod_eligible_stub(p):
    """Local stand-in for evolution.advance._eligible()'s DEFERRED branch
    (that module's own untracked transitive deps can't be imported in this
    worktree -- see the file docstring). Verifies only the specific,
    documented semantics this fix relies on: DEFERRED is never in
    _eligible()'s accepted starting-status set (OBSERVED, PROPOSED)."""
    eligible_statuses = (proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED)
    if p.status not in eligible_statuses:
        return False, f"status={p.status!r} is not an eligible starting point"
    return True, ""
