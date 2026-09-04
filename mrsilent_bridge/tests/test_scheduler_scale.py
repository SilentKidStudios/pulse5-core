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
import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")


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


@pytest.mark.parametrize("n", [10, 20, 50, 100, 500, 1000])
def test_scale_tier(monkeypatch, tmp_path, n, capsys):
    _fresh(monkeypatch, tmp_path)
    invocation_counts: dict[str, int] = {}
    lock = threading.Lock()

    def _executor(item: wg.WorkItem) -> dict:
        with lock:
            invocation_counts[item.work_id] = invocation_counts.get(item.work_id, 0) + 1
        return {"ok": True}

    built = _build_graph(n)

    passes = sched.run_until_drained(
        self_session_id="scale-harness", executor_fn=_executor,
        resource_vector=_healthy_vector(), max_passes=max(50, n),
    )

    all_items = wg.list_all()
    retained = len(all_items)
    completed = sum(1 for r in all_items if r.state == wg.WorkState.COMPLETED)
    still_blocked = sum(1 for r in all_items if r.state == wg.WorkState.BLOCKED_DEPENDENCY)
    still_running = sum(1 for r in all_items if r.state == wg.WorkState.RUNNING)
    terminal_failed = sum(1 for r in all_items if r.state == wg.WorkState.TERMINAL_FAILED)
    duplicates = sum(1 for count in invocation_counts.values() if count > 1)
    lost = n - retained
    running_max = max((p.peak_concurrent_observed for p in passes), default=0)
    deadlocked = still_blocked + still_running  # non-terminal and not runnable after full drain = deadlocked

    result_pass = (
        retained == n and lost == 0 and duplicates == 0 and deadlocked == 0
        and completed == n and terminal_failed == 0
    )

    report = {
        f"SCALE_{n}_CREATED": n,
        f"SCALE_{n}_RETAINED": retained,
        f"SCALE_{n}_RUNNING_MAX": running_max,
        f"SCALE_{n}_BLOCKED_INITIALLY": built["initial_blocked"],
        f"SCALE_{n}_BLOCKED_FINAL": still_blocked,
        f"SCALE_{n}_COMPLETED": completed,
        f"SCALE_{n}_DUPLICATES": duplicates,
        f"SCALE_{n}_LOST": lost,
        f"SCALE_{n}_DEADLOCKED": deadlocked,
        f"SCALE_{n}_PASSES_TO_DRAIN": len(passes),
        f"SCALE_{n}_RESULT": "PASS" if result_pass else "FAIL",
    }
    with capsys.disabled():
        for k, v in report.items():
            print(f"{k}={v}")

    assert retained == n, f"lost work items: created {n}, retained {retained}"
    assert duplicates == 0, f"duplicate executions detected: {invocation_counts}"
    assert deadlocked == 0, f"deadlocked items remain after drain: blocked={still_blocked} running={still_running}"
    assert completed == n, f"not all items completed: {completed}/{n}"
    assert terminal_failed == 0
