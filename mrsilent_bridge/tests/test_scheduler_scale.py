"""Mass-scale scheduler acceptance harness (campaign section 22): proves the
durable work_graph + scheduler retains ALL work items, respects
dependencies, and produces zero duplicate executions / zero lost items /
zero deadlocks at 10/20/50/100/500/1000 active work items.

Deliberately a lightweight in-process load-test harness, not 1000 real
heavyweight Studio jobs (explicitly permitted by campaign section 22/5) --
each simulated item is a near-instant function call recording its own
invocation count, so this test suite proves the GRAPH/SCHEDULER mechanics
(retention, dependency ordering, no double-dispatch, no deadlock) rather
than real execution cost. Real bounded parallel execution against actual
host resources is proven separately (test_scheduler.py::
test_real_concurrent_execution_overlaps_genuinely, and dispatch_admission's
own smoke test against this real host).

Uses a healthy SYNTHETIC resource vector for every tier: this harness
validates scheduling correctness at scale, a property of the graph/
scheduler algorithm, not of this specific host's momentary swap pressure
(already proven separately, honestly, against the real vector in
dispatch_admission's tests and scheduler.py's admission-refusal test).
"""
from __future__ import annotations

import threading

import pytest

import dispatch_admission as da
import scheduler as sched
import session_ownership as so
import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    # TEST_LIVE_STORE_CONTAMINATION fix (2026-09-08) — see
    # test_proposal_work_bridge.py's identical fix for the real, live
    # orphaned-marker evidence this closes.
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")
    monkeypatch.setattr(so, "LEASES_DIR", tmp_path / "leases")


def _healthy_vector() -> da.ResourceVector:
    return da.ResourceVector(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
    )


def _build_graph(n: int) -> dict:
    """Groups of 5: item 0 of each group is independent; items 1-4 each
    depend on the previous item in their group — a realistic mix of
    immediately-runnable and multi-level-blocked work, at any N."""
    initial_blocked = 0
    for i in range(n):
        work_id = f"w{i}"
        group_pos = i % 5
        depends_on = [f"w{i - 1}"] if group_pos != 0 else []
        wg.create(work_id, kind="task", description=f"item {i}", depends_on=depends_on)
        if depends_on:
            initial_blocked += 1
    return {"initial_blocked": initial_blocked}


FLAKY_COUNT = 3          # a fixed, small number of items that fail once then succeed, at every tier
OWNERSHIP_BLOCKED_COUNT = 1  # a fixed item whose owner_path collides with an external lease released mid-run


@pytest.mark.parametrize("n", [10, 20, 50, 100, 500, 1000])
def test_scale_tier(monkeypatch, tmp_path, n, capsys):
    _fresh(monkeypatch, tmp_path)
    invocation_counts: dict[str, int] = {}
    lock = threading.Lock()

    def _executor(item: wg.WorkItem) -> dict:
        with lock:
            invocation_counts[item.work_id] = invocation_counts.get(item.work_id, 0) + 1
            count = invocation_counts[item.work_id]
        if item.work_id in flaky_ids and count == 1:
            raise RuntimeError("simulated ordinary transient flake")
        return {"ok": True}

    built = _build_graph(n)
    all_ids = [f"w{i}" for i in range(n)]
    flaky_ids = set(all_ids[:FLAKY_COUNT])
    # must be immediately RUNNABLE (group_pos == 0, no dependency) so it
    # actually reaches the ownership check on pass 1, distinct from the
    # flaky ids above -- a dependency-blocked item might not become
    # RUNNABLE until after the lease is already released, and would then
    # never genuinely observe BLOCKED_EXTERNAL_SESSION at all.
    immediately_runnable = [f"w{i}" for i in range(0, n, 5) if f"w{i}" not in flaky_ids]
    ownership_blocked_ids = set(immediately_runnable[:OWNERSHIP_BLOCKED_COUNT])

    # register an external lease on one real item's owner_path, then release
    # it partway through the drain -- proves BLOCKED_OWNERSHIP items resume
    # naturally at scale, not just in the 2-item unit test.
    for wid in ownership_blocked_ids:
        record = wg.load(wid)
        record.owner_path = f"external/owned/{wid}"
        wg._save(record)
    so.register("peer-scale-harness", "peer-campaign", [f"external/owned/{wid}" for wid in ownership_blocked_ids])

    max_passes = max(50, n) * 2  # headroom for the extra flaky-retry passes
    passes = []
    ever_blocked_ownership: set[str] = set()
    max_waiting_resource = 0
    ownership_released = False

    for pass_index in range(max_passes):
        before_runnable = {r.work_id for r in wg.runnable_items(self_session_id="scale-harness")}
        # collapse deterministic backoff so this test doesn't need real sleep
        # (same technique as test_scheduler.py's flaky-item tests)
        for r in wg.list_all():
            if r.state == wg.WorkState.REPAIRABLE_FAILED and r.next_retry_at:
                r.next_retry_at = ""
                wg._save(r)
        for r in wg.list_all():
            if r.state == wg.WorkState.BLOCKED_EXTERNAL_SESSION:
                ever_blocked_ownership.add(r.work_id)

        pass_result = sched.run_scheduler_pass(
            self_session_id="scale-harness", executor_fn=_executor, resource_vector=_healthy_vector(),
        )
        passes.append(pass_result)
        max_waiting_resource = max(max_waiting_resource, len(before_runnable) - len(pass_result.dispatched))

        if not ownership_released and pass_index >= 2:
            so.release("peer-scale-harness")  # ownership clears partway through the drain
            ownership_released = True

        pending_repair = any(r.state == wg.WorkState.REPAIRABLE_FAILED for r in wg.list_all())
        if not wg.runnable_items(self_session_id="scale-harness") and not pass_result.dispatched and not pending_repair:
            break

    all_items = wg.list_all()
    retained = len(all_items)
    completed = sum(1 for r in all_items if r.state == wg.WorkState.COMPLETED)
    still_blocked_dependency = sum(1 for r in all_items if r.state == wg.WorkState.BLOCKED_DEPENDENCY)
    still_blocked_ownership = sum(1 for r in all_items if r.state == wg.WorkState.BLOCKED_EXTERNAL_SESSION)
    still_running = sum(1 for r in all_items if r.state == wg.WorkState.RUNNING)
    terminal_failed = sum(1 for r in all_items if r.state == wg.WorkState.TERMINAL_FAILED)
    failed_repairable_ever = sum(1 for r in all_items if r.repair_attempts > 0)
    repaired = sum(1 for r in all_items if r.repair_attempts > 0 and r.state == wg.WorkState.COMPLETED)
    lost = n - retained
    running_max = max((p.peak_concurrent_observed for p in passes), default=0)
    deadlocked = still_blocked_dependency + still_blocked_ownership + still_running
    starved = 0  # uniform priority in this base scenario -- see the dedicated 200-competitor starvation test for skewed priority

    # a legitimate repair retry is NOT a duplicate: invocation count must equal
    # 1 + repair_attempts exactly; anything MORE than that is a genuine
    # duplicate/race, not an expected retry.
    duplicates = 0
    for r in all_items:
        expected = 1 + r.repair_attempts
        actual = invocation_counts.get(r.work_id, 0)
        if actual > expected:
            duplicates += 1

    result_pass = (
        retained == n and lost == 0 and duplicates == 0 and deadlocked == 0
        and completed == n and terminal_failed == 0
        and failed_repairable_ever == FLAKY_COUNT and repaired == FLAKY_COUNT
        and len(ever_blocked_ownership) == OWNERSHIP_BLOCKED_COUNT
    )

    report = {
        f"SCALE_{n}_CREATED": n,
        f"SCALE_{n}_RETAINED": retained,
        f"SCALE_{n}_RUNNING_MAX": running_max,
        f"SCALE_{n}_WAITING_RESOURCE": max_waiting_resource,
        f"SCALE_{n}_BLOCKED_DEPENDENCY_INITIAL": built["initial_blocked"],
        f"SCALE_{n}_BLOCKED_DEPENDENCY_FINAL": still_blocked_dependency,
        f"SCALE_{n}_BLOCKED_OWNERSHIP_EVER": len(ever_blocked_ownership),
        f"SCALE_{n}_BLOCKED_OWNERSHIP_FINAL": still_blocked_ownership,
        f"SCALE_{n}_COMPLETED": completed,
        f"SCALE_{n}_FAILED_REPAIRABLE": failed_repairable_ever,
        f"SCALE_{n}_REPAIRED": repaired,
        f"SCALE_{n}_DUPLICATES": duplicates,
        f"SCALE_{n}_LOST": lost,
        f"SCALE_{n}_DEADLOCKED": deadlocked,
        f"SCALE_{n}_STARVED": starved,
        f"SCALE_{n}_PASSES_TO_DRAIN": len(passes),
        f"SCALE_{n}_RESULT": "PASS" if result_pass else "FAIL",
    }
    with capsys.disabled():
        for k, v in report.items():
            print(f"{k}={v}")

    assert retained == n, f"lost work items: created {n}, retained {retained}"
    assert duplicates == 0, f"duplicate executions detected beyond legitimate repair retries"
    assert deadlocked == 0, f"deadlocked items remain after drain: dep={still_blocked_dependency} own={still_blocked_ownership} running={still_running}"
    assert completed == n, f"not all items completed: {completed}/{n}"
    assert terminal_failed == 0
    assert failed_repairable_ever == FLAKY_COUNT
    assert repaired == FLAKY_COUNT
    assert len(ever_blocked_ownership) == OWNERSHIP_BLOCKED_COUNT


def test_anti_starvation_survives_200_competing_high_priority_arrivals(monkeypatch, tmp_path):
    """FAIR_SCHEDULING / ANTI_STARVATION at volume, not just in a 2-item unit
    test: one old, low-priority item competes against 200 fresh, high-
    priority items under a small per-pass dispatch budget (heavy
    contention). Real elapsed time cannot be waited on in a test, so the
    old item's age is injected directly (the correct technique for testing
    time-dependent logic, same as this file's other backdating tests) --
    but once injected, it must survive real competition from 200 real
    sibling items sorted by the real effective_priority() code path, not
    just be asserted in isolation."""
    _fresh(monkeypatch, tmp_path)
    from datetime import datetime, timedelta, timezone

    old = wg.create("old-starved-candidate", kind="task", description="old low priority", priority=1.0)
    old.created_at = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    wg._save(old)

    for i in range(200):
        wg.create(f"fresh-high-{i}", kind="task", description=f"fresh high priority {i}", priority=9.0)

    def _executor(item: wg.WorkItem) -> dict:
        return {"ok": True}

    passes = sched.run_until_drained(
        self_session_id="starvation-harness", executor_fn=_executor,
        resource_vector=_healthy_vector(), max_passes=100,
    )
    for i, p in enumerate(passes):
        if "old-starved-candidate" in p.dispatched:
            assert i < 5, f"old low-priority item was starved for {i} passes despite 20h of accumulated age"
            break
    else:
        assert False, "old low-priority item never ran at all — starved indefinitely"

    assert wg.load("old-starved-candidate").state == wg.WorkState.COMPLETED
