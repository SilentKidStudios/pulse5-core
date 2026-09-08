"""Tests for scheduler.py — bounded resource-aware parallel dispatch over
work_graph.py, built during the WALK-AWAY CONVERGENCE gap-closure pass
(2026-09-04)."""
from __future__ import annotations

import time

import dispatch_admission as da
import scheduler as sched
import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    # TEST_LIVE_STORE_CONTAMINATION fix (2026-09-08) — see
    # test_proposal_work_bridge.py's identical fix for the real, live
    # orphaned-marker evidence this closes.
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")


def _healthy_vector(**overrides) -> da.ResourceVector:
    base = dict(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
    )
    base.update(overrides)
    return da.ResourceVector(**base)


def _trivial_executor(item: wg.WorkItem) -> dict:
    return {"ok": True, "work_id": item.work_id}


def test_pass_dispatches_and_completes_runnable_items(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("t1", kind="task", description="t1")
    wg.create("t2", kind="task", description="t2")

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_trivial_executor, resource_vector=_healthy_vector(),
    )
    assert set(result.completed) == {"t1", "t2"}
    assert wg.load("t1").state == wg.WorkState.COMPLETED
    assert wg.load("t2").state == wg.WorkState.COMPLETED
    assert result.admission_refused is False


def test_pass_refuses_admission_under_resource_pressure(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("t1", kind="task", description="t1")
    pressured = _healthy_vector(swap_total_mb=2000, swap_used_mb=2000)  # this project's own real observed condition

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_trivial_executor, resource_vector=pressured,
    )
    assert result.admission_refused is True
    assert result.dynamic_safe_concurrency == 0
    assert result.dispatched == []
    assert wg.load("t1").state == wg.WorkState.RUNNABLE  # untouched, not lost — QUEUED_WORK_CONTINUES_PROGRESS


def test_pass_never_dispatches_more_than_dynamic_safe_concurrency(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    for i in range(10):
        wg.create(f"t{i}", kind="task", description=f"t{i}")

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_trivial_executor,
        resource_vector=_healthy_vector(), max_items=2,
    )
    assert len(result.dispatched) == 2
    remaining_runnable = [r for r in wg.list_all() if r.state == wg.WorkState.RUNNABLE]
    assert len(remaining_runnable) == 8  # rest stayed queued, not lost


def test_a_failing_item_becomes_repairable_failed_not_a_crash(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("bad", kind="task", description="bad")

    def _boom(item):
        raise RuntimeError("simulated ordinary failure")

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_boom, resource_vector=_healthy_vector(),
    )
    assert result.repairable_failed == ["bad"]
    assert wg.load("bad").state == wg.WorkState.REPAIRABLE_FAILED
    assert wg.load("bad").result["error_class"] == "RuntimeError"


def test_already_claimed_item_is_not_double_dispatched(monkeypatch, tmp_path):
    """Exercises the real race window in work_graph.claim(): the exclusive
    lock FILE is what actually provides atomicity (O_CREAT|O_EXCL) — the
    JSON `state` field is bookkeeping written just after. Two schedulers
    that both snapshot the item as RUNNABLE a moment apart (state not yet
    flipped) must still never both win the underlying claim() race — one
    gets duplicate_claim_rejections, never a second dispatch."""
    _fresh(monkeypatch, tmp_path)
    wg.create("t1", kind="task", description="t1")
    lock_path = wg._lock_path("t1")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text('{"owner": "someone-else", "pid": 999999999, "hostname": "other", "claimed_at": "2026-01-01T00:00:00+00:00"}')

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_trivial_executor, resource_vector=_healthy_vector(),
    )
    assert result.duplicate_claim_rejections == 1
    assert result.dispatched == []
    assert wg.load("t1").state == wg.WorkState.RUNNABLE  # untouched — never silently marked running/completed


def test_real_concurrent_execution_overlaps_genuinely(monkeypatch, tmp_path):
    """REAL_PARALLEL_EXECUTION: proves genuine overlap (peak_concurrent_
    observed > 1), not just N sequential submissions — each task sleeps
    briefly so two dispatched at once must overlap in wall-clock time for
    the peak counter to ever exceed 1."""
    _fresh(monkeypatch, tmp_path)
    for i in range(4):
        wg.create(f"t{i}", kind="task", description=f"t{i}")

    def _slow_task(item):
        time.sleep(0.05)
        return {"ok": True}

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_slow_task, resource_vector=_healthy_vector(),
    )
    assert len(result.completed) == 4
    assert result.peak_concurrent_observed >= 2


def test_run_until_drained_resolves_a_dependency_chain_across_passes(monkeypatch, tmp_path):
    """End-to-end: A depends on B depends on C, driven purely by repeated
    scheduler passes with no manual "now finish C" / "go back to B" —
    exactly campaign section 3's example, exercised through the scheduler
    rather than calling work_graph functions directly."""
    _fresh(monkeypatch, tmp_path)
    wg.create("c", kind="task", description="c")
    wg.create("b", kind="task", description="b", depends_on=["c"])
    wg.create("a", kind="objective", description="a", depends_on=["b"])

    passes = sched.run_until_drained(
        self_session_id="self", executor_fn=_trivial_executor, resource_vector=_healthy_vector(),
    )
    assert wg.load("a").state == wg.WorkState.COMPLETED
    assert wg.load("b").state == wg.WorkState.COMPLETED
    assert wg.load("c").state == wg.WorkState.COMPLETED
    assert len(passes) >= 3  # could not have resolved in fewer than 3 passes given the chain


def test_founder_gated_branch_does_not_block_unrelated_work(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.mark_blocked_founder(wg.create("gated", kind="task", description="gated").work_id)
    wg.create("unrelated", kind="task", description="unrelated")

    result = sched.run_scheduler_pass(
        self_session_id="self", executor_fn=_trivial_executor, resource_vector=_healthy_vector(),
    )
    assert "unrelated" in result.completed
    assert "gated" not in result.dispatched
    assert wg.load("gated").state == wg.WorkState.BLOCKED_FOUNDER


def test_ordinary_failure_autonomously_repairs_and_completes_without_manual_dispatch(monkeypatch, tmp_path):
    """FAILURE_REPAIR_OR_REPROPOSAL_AUTONOMOUS end-to-end through the
    scheduler: an item fails twice (simulated transient flake), is
    auto-retried via retry_repairable_failed() each pass once its backoff
    elapses, and completes on its third attempt -- no manual re-dispatch."""
    _fresh(monkeypatch, tmp_path)
    wg.create("flaky", kind="task", description="flaky")
    attempts = {"count": 0}

    def _flaky_then_succeeds(item):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("simulated transient flake")
        return {"ok": True}

    for _ in range(6):  # enough passes for two backoff-elapsed retries
        record = wg.load("flaky")
        if record.state == wg.WorkState.REPAIRABLE_FAILED and record.next_retry_at:
            record.next_retry_at = ""  # test-only: collapse backoff so the loop doesn't need real sleep
            wg._save(record)
        sched.run_scheduler_pass(self_session_id="self", executor_fn=_flaky_then_succeeds, resource_vector=_healthy_vector())
        if wg.load("flaky").state == wg.WorkState.COMPLETED:
            break

    assert wg.load("flaky").state == wg.WorkState.COMPLETED
    assert attempts["count"] == 3
    assert wg.load("flaky").repair_attempts == 2


def test_repair_budget_exhaustion_surfaces_as_founder_gate_not_silent_loss(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(wg, "MAX_REPAIR_ATTEMPTS", 1)
    wg.create("always-fails", kind="task", description="always-fails")

    def _always_fails(item):
        raise RuntimeError("permanent-looking failure")

    for _ in range(4):
        record = wg.load("always-fails")
        if record.state == wg.WorkState.REPAIRABLE_FAILED and record.next_retry_at:
            record.next_retry_at = ""
            wg._save(record)
        sched.run_scheduler_pass(self_session_id="self", executor_fn=_always_fails, resource_vector=_healthy_vector())
        if wg.load("always-fails").state == wg.WorkState.BLOCKED_FOUNDER:
            break

    assert wg.load("always-fails").state == wg.WorkState.BLOCKED_FOUNDER  # escalated, not lost
