"""Tests for work_graph.py — the durable dependency-aware work graph built
during the WALK-AWAY CONVERGENCE gap-closure pass (2026-09-04).

Isolation: monkeypatches ITEMS_DIR/LOCKS_DIR to tmp_path, exactly like
test_session_ownership.py does for LEASES_DIR — this module has no real
production data yet (brand new), but the pattern is kept consistent so a
future real caller never risks a test touching genuine work-graph state.
"""
from __future__ import annotations

import os

import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")


def test_create_without_dependencies_is_runnable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    item = wg.create("a", kind="objective", description="Finish OmniForge")
    assert item.state == wg.WorkState.RUNNABLE


def test_create_with_dependency_is_blocked(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("b", kind="task", description="dependency B")
    a = wg.create("a", kind="objective", description="A needs B", depends_on=["b"])
    assert a.state == wg.WorkState.BLOCKED_DEPENDENCY
    assert wg.unmet_dependencies("a") == ["b"]


def test_three_level_chain_resumes_naturally_a_b_c(monkeypatch, tmp_path):
    """Exactly campaign section 3's example: A requires B requires C.
    Finishing C should auto-resume B, and finishing B should auto-resume A
    -- with no caller ever saying "now finish C" / "go back to B"."""
    _fresh(monkeypatch, tmp_path)
    wg.create("c", kind="task", description="C")
    wg.create("b", kind="task", description="B", depends_on=["c"])
    wg.create("a", kind="objective", description="A", depends_on=["b"])

    assert wg.load("a").state == wg.WorkState.BLOCKED_DEPENDENCY
    assert wg.load("b").state == wg.WorkState.BLOCKED_DEPENDENCY
    assert wg.load("c").state == wg.WorkState.RUNNABLE

    wg.mark_completed("c")  # triggers resume_eligible_parents() internally
    assert wg.load("b").state == wg.WorkState.RUNNABLE
    assert wg.load("a").state == wg.WorkState.BLOCKED_DEPENDENCY  # a still waits on b

    wg.mark_completed("b")
    assert wg.load("a").state == wg.WorkState.RUNNABLE


def test_direct_cycle_is_rejected(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("x", kind="task", description="x")
    wg.create("y", kind="task", description="y", depends_on=["x"])
    try:
        wg.add_dependency("x", "y")
        assert False, "expected CycleDetected"
    except wg.CycleDetected:
        pass


def test_transitive_cycle_is_rejected(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("p", kind="task", description="p")
    wg.create("q", kind="task", description="q", depends_on=["p"])
    wg.create("r", kind="task", description="r", depends_on=["q"])
    try:
        wg.add_dependency("p", "r")  # p -> r -> q -> p would close the loop
        assert False, "expected CycleDetected"
    except wg.CycleDetected:
        pass


def test_create_child_for_dependency_suppresses_duplicates(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("parent", kind="objective", description="OmniForge")
    child1 = wg.create_child_for_dependency("parent", kind="dependency", description="Complete Omni-X")
    child2 = wg.create_child_for_dependency("parent", kind="dependency", description="Complete Omni-X")
    assert child1.work_id == child2.work_id
    parent = wg.load("parent")
    assert parent.depends_on.count(child1.work_id) == 1
    assert parent.state == wg.WorkState.BLOCKED_DEPENDENCY


def test_create_child_for_dependency_records_provenance(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("parent", kind="objective", description="OmniForge")
    child = wg.create_child_for_dependency("parent", kind="dependency", description="Complete Omni-X")
    assert child.provenance.get("parent_id") == "parent"
    assert "missing dependency" in child.provenance.get("created_because", "")


def test_bounded_fanout_budget_enforced(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(wg, "MAX_CHILDREN_PER_PARENT", 2)
    wg.create("parent", kind="objective", description="OmniForge")
    wg.create_child_for_dependency("parent", kind="dependency", description="dep 1")
    wg.create_child_for_dependency("parent", kind="dependency", description="dep 2")
    try:
        wg.create_child_for_dependency("parent", kind="dependency", description="dep 3")
        assert False, "expected FanOutBudgetExceeded"
    except wg.FanOutBudgetExceeded:
        pass


def test_recursive_depth_measured_correctly(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("c", kind="task", description="c")
    wg.create("b", kind="task", description="b", depends_on=["c"])
    wg.create("a", kind="task", description="a", depends_on=["b"])
    assert wg.recursive_depth("a") == 2
    assert wg.recursive_depth("b") == 1
    assert wg.recursive_depth("c") == 0


def test_founder_gate_blocks_only_dependent_branch(monkeypatch, tmp_path):
    """A genuine Founder gate on one item must not affect an unrelated item
    (campaign section 11 / 26)."""
    _fresh(monkeypatch, tmp_path)
    gated = wg.create("gated-item", kind="task", description="needs Founder approval")
    unrelated = wg.create("unrelated-item", kind="task", description="independent work")
    wg.mark_blocked_founder("gated-item")
    assert wg.load("gated-item").state == wg.WorkState.BLOCKED_FOUNDER
    assert wg.load("unrelated-item").state == wg.WorkState.RUNNABLE
    runnable_ids = {r.work_id for r in wg.runnable_items()}
    assert "unrelated-item" in runnable_ids
    assert "gated-item" not in runnable_ids


def test_claim_is_exclusive_no_double_dispatch(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("job1", kind="task", description="job1")
    assert wg.claim("job1", owner="worker-a") is True
    assert wg.claim("job1", owner="worker-b") is False  # second claim must fail
    assert wg.load("job1").state == wg.WorkState.RUNNING
    assert wg.load("job1").owner == "worker-a"


def test_release_only_by_owning_pid(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("job1", kind="task", description="job1")
    wg.claim("job1", owner="worker-a")
    assert wg.release("job1", owner="worker-a") is True
    assert wg.claim("job1", owner="worker-b") is True  # freed, now claimable


def test_stale_running_item_is_reclaimed_and_requeued(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("job1", kind="task", description="job1")
    wg.claim("job1", owner="worker-a")
    # simulate a dead owner by writing a lock file with an unused pid
    lock = wg._lock_path("job1")
    import json
    payload = json.loads(lock.read_text())
    payload["pid"] = _unused_pid()
    lock.write_text(json.dumps(payload))

    reclaimed = wg.reclaim_stale()
    assert "job1" in reclaimed
    record = wg.load("job1")
    assert record.state == wg.WorkState.RUNNABLE
    assert record.resume_count == 1


def test_stale_reclaim_exhausts_to_terminal_after_budget(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(wg, "MAX_RESUME_ATTEMPTS", 1)
    wg.create("job1", kind="task", description="job1")

    for _ in range(2):
        wg.claim("job1", owner="worker-a")
        lock = wg._lock_path("job1")
        import json
        payload = json.loads(lock.read_text())
        payload["pid"] = _unused_pid()
        lock.write_text(json.dumps(payload))
        wg.reclaim_stale()

    assert wg.load("job1").state == wg.WorkState.TERMINAL_FAILED


def test_anti_starvation_aging_eventually_outranks_higher_priority(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    _fresh(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)

    old_low = wg.create("old-low", kind="task", description="old low priority", priority=1.0)
    old_low.created_at = (now - timedelta(hours=12)).isoformat()  # actually old, unlike new_high below
    wg._save(old_low)
    new_high = wg.create("new-high", kind="task", description="new high priority", priority=5.0)
    old_low = wg.load("old-low")  # re-load to pick up the rewritten created_at

    # a continuous stream of fresh high-priority work must not starve an
    # older, lower-priority item forever: given enough accumulated age, it
    # eventually outranks a same-moment item with no age bonus at all.
    assert wg.effective_priority(old_low, now=now) > wg.effective_priority(new_high, now=now)


def test_runnable_items_ordered_by_effective_priority(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("low", kind="task", description="low", priority=1.0)
    wg.create("high", kind="task", description="high", priority=9.0)
    ordered = wg.runnable_items()
    assert [r.work_id for r in ordered] == ["high", "low"]


def test_duplicate_execution_prevention_via_fingerprint(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("job1", kind="task", description="do the exact same thing")
    dup = wg.find_active_by_fingerprint(wg.objective_fingerprint("task:do the exact same thing"))
    assert dup is not None
    assert dup.work_id == "job1"


def test_completed_item_is_not_a_duplicate_of_a_new_request(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("job1", kind="task", description="repeatable request")
    wg.mark_completed("job1")
    dup = wg.find_active_by_fingerprint(wg.objective_fingerprint("task:repeatable request"))
    assert dup is None  # completed work is history, not an in-flight duplicate


def test_status_counts_reflect_real_state_distribution(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("r1", kind="task", description="r1")
    wg.create("dep", kind="task", description="dep")
    wg.create("b1", kind="task", description="b1", depends_on=["dep"])
    wg.mark_blocked_founder(wg.create("f1", kind="task", description="f1").work_id)
    counts = wg.status_counts()
    assert counts["TASK_COUNT"] == 4
    assert counts["RUNNABLE"] == 2  # r1, dep
    assert counts["BLOCKED_DEPENDENCY"] == 1  # b1
    assert counts["BLOCKED_FOUNDER"] == 1  # f1


def test_session_ownership_integration_blocks_and_recovers(monkeypatch, tmp_path):
    """A work item whose owner_path collides with an externally-owned
    session lease is excluded from runnable_items(); once that session
    releases, the item becomes runnable again with no manual override."""
    _fresh(monkeypatch, tmp_path)
    import session_ownership as so
    monkeypatch.setattr(so, "LEASES_DIR", tmp_path / "leases")

    so.register("peer-session", "peer-campaign", ["mrsilent_bridge/evolution"])
    wg.create("touches-evolution", kind="task", description="edit evolution/advance.py",
              owner_path="mrsilent_bridge/evolution/advance.py")
    wg.create("unrelated", kind="task", description="edit work_graph.py",
              owner_path="mrsilent_bridge/work_graph.py")

    runnable_ids = {r.work_id for r in wg.runnable_items(self_session_id="this-session")}
    assert "unrelated" in runnable_ids
    assert "touches-evolution" not in runnable_ids
    assert wg.load("touches-evolution").state == wg.WorkState.BLOCKED_EXTERNAL_SESSION

    so.release("peer-session")
    runnable_ids_after = {r.work_id for r in wg.runnable_items(self_session_id="this-session")}
    assert "touches-evolution" in runnable_ids_after


def _unused_pid() -> int:
    candidate = 2_000_000
    while _pid_exists(candidate):
        candidate += 1
    return candidate


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
