"""Consolidated Founder-gate branch isolation acceptance test — WALK-AWAY
CONVERGENCE macro campaign, Phase 5 (2026-09-05). Exercises the exact
graph shape requested: Founder-gated A, dependent child A1, independent B
and C, resource-constrained D — using the REAL evolution.proposal.py/
evolution.founder_request.py (not mocked) for the gate itself, and real
scheduler.py dispatch for the resource-constrained behavior."""
from __future__ import annotations

import dispatch_admission as da
import proposal_work_bridge as bridge
import scheduler as sched
import work_graph as wg
from evolution import founder_request
from evolution import proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    # TEST_LIVE_STORE_CONTAMINATION fix (2026-09-08): STATE_INDEX_DIR is a
    # third WORK_GRAPH_ROOT-derived sibling of ITEMS_DIR/LOCKS_DIR, never
    # patched here — every state transition this file's tests exercise was
    # writing real marker files into the live production work_graph_state/
    # state_index/ tree despite the item itself only ever existing under
    # tmp_path (see test_proposal_work_bridge.py's identical fix for the
    # real, live orphaned-marker evidence this closes).
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(proposal_mod, "NEW_PROPOSAL_SIGNAL_PATH", tmp_path / "signal")
    monkeypatch.setattr(founder_request, "ESCALATIONS_DIR", tmp_path / "escalations")


def _healthy_vector() -> da.ResourceVector:
    return da.ResourceVector(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
        psi_memory_avg10=0.0, oom_kill_recent=False,  # DIRECT_PSI_OOM_GATE (2026-09-08): healthy baseline
    )


def _complete_founder_gated(weakness: str, upgrade: str) -> proposal_mod.Proposal:
    """PROPOSAL COMPLETENESS GATE (2026-09-07): a bare founder_gated
    proposal is no longer surfaced as BLOCKED_FOUNDER at all (see
    test_proposal_work_bridge.py's dedicated coverage of that new
    behavior) — this suite's actual intent is genuine Founder-gate
    mechanics, so its fixture proposal is made decision-ready."""
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


def _build_graph(tmp_path, monkeypatch):
    _fresh(monkeypatch, tmp_path)
    a_proposal = _complete_founder_gated("risky change", "do it")
    bridge.upsert_workitems_from_proposals()
    a_id = bridge.workitem_id_for_proposal(a_proposal.proposal_id)

    wg.create("A1", kind="task", description="A1 depends on A", depends_on=[a_id])
    wg.create("B", kind="task", description="independent B", priority=5.0)
    wg.create("C", kind="task", description="independent C", priority=5.0)
    wg.create("D", kind="task", description="resource-constrained D", priority=-1.0)  # lowest priority

    return a_proposal, a_id


def test_gated_a_blocks_only_a1_while_b_c_d_remain_schedulable(monkeypatch, tmp_path):
    a_proposal, a_id = _build_graph(tmp_path, monkeypatch)

    assert wg.load(a_id).state == wg.WorkState.BLOCKED_FOUNDER
    assert wg.load("A1").state == wg.WorkState.BLOCKED_DEPENDENCY  # blocked on A specifically, not a separate gate

    runnable_ids = {r.work_id for r in wg.runnable_items()}
    assert a_id not in runnable_ids
    assert "A1" not in runnable_ids
    assert "B" in runnable_ids
    assert "C" in runnable_ids
    assert "D" in runnable_ids  # D is eligible — its wait is about resources, not a block


def test_d_waits_for_resources_not_blocked_while_b_c_proceed(monkeypatch, tmp_path):
    """D is not "blocked" in the graph-state sense — it is eligible but
    loses the bounded per-pass budget to higher-priority B/C, exactly
    modeling "waits only for resources.\""""
    a_proposal, a_id = _build_graph(tmp_path, monkeypatch)

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=lambda item: {"ok": True},
        resource_vector=_healthy_vector(), max_items=2,
    )
    assert set(result.completed) == {"B", "C"}
    assert "D" not in result.dispatched
    assert wg.load("D").state == wg.WorkState.RUNNABLE  # still genuinely eligible, just queued
    assert a_id not in result.dispatched
    assert "A1" not in result.dispatched

    # a later pass with headroom picks D up — proving it was never actually blocked
    result2 = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=lambda item: {"ok": True},
        resource_vector=_healthy_vector(),
    )
    assert "D" in result2.completed


def test_exact_founder_approval_makes_a_eligible_and_resumes_a1(monkeypatch, tmp_path):
    a_proposal, a_id = _build_graph(tmp_path, monkeypatch)

    escalation = founder_request.request_founder_decision(
        subject=a_proposal.proposal_id, finding="risky change needed", capability_needed="approve proposal",
        reason_required="founder_gated risk", recommended_action="advance the proposal",
        risk="founder_gated", affected={"proposal_id": a_proposal.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")

    # a later natural population pass consumes the exact decision — no manual state edit
    counts = bridge.upsert_workitems_from_proposals()
    assert counts["updated"] == 1
    assert wg.load(a_id).state == wg.WorkState.RUNNABLE

    # risk classification itself is never mutated to bypass eligibility
    reloaded_proposal = proposal_mod.load(a_proposal.proposal_id)
    assert reloaded_proposal.risk_score == "founder_gated"

    # A executes; A1 auto-resumes with no manual dependency edit
    wg.mark_completed(a_id)
    assert wg.load("A1").state == wg.WorkState.RUNNABLE


def test_exact_founder_denial_keeps_a_and_a1_non_executable_unrelated_continues(monkeypatch, tmp_path):
    a_proposal, a_id = _build_graph(tmp_path, monkeypatch)

    escalation = founder_request.request_founder_decision(
        subject=a_proposal.proposal_id, finding="risky change needed", capability_needed="approve proposal",
        reason_required="founder_gated risk", recommended_action="advance the proposal",
        risk="founder_gated", affected={"proposal_id": a_proposal.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "denied")

    bridge.upsert_workitems_from_proposals()
    assert wg.load(a_id).state == wg.WorkState.BLOCKED_FOUNDER  # remains non-executable
    assert wg.load("A1").state == wg.WorkState.BLOCKED_DEPENDENCY  # remains blocked (depends on A)

    reloaded_proposal = proposal_mod.load(a_proposal.proposal_id)
    assert reloaded_proposal.risk_score == "founder_gated"  # no risk mutation to bypass the gate

    runnable_ids = {r.work_id for r in wg.runnable_items()}
    assert "B" in runnable_ids
    assert "C" in runnable_ids
    assert "D" in runnable_ids  # entirely unrelated work continues regardless of the denial


def test_approval_for_a_never_authorizes_a_different_proposal(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    a = _complete_founder_gated("risky A", "do A")
    other = _complete_founder_gated("risky OTHER", "do other")
    bridge.upsert_workitems_from_proposals()

    escalation = founder_request.request_founder_decision(
        subject=a.proposal_id, finding="f", capability_needed="approve proposal",
        reason_required="r", recommended_action="advance", risk="founder_gated",
        affected={"proposal_id": a.proposal_id},
    )
    founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")
    bridge.upsert_workitems_from_proposals()

    assert wg.load(bridge.workitem_id_for_proposal(a.proposal_id)).state == wg.WorkState.RUNNABLE
    assert wg.load(bridge.workitem_id_for_proposal(other.proposal_id)).state == wg.WorkState.BLOCKED_FOUNDER
