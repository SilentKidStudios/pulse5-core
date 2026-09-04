"""Tests for scheduler_cycle_hook.py — the integration-ready adapter built
during the WALK-AWAY CONVERGENCE gap-closure pass (2026-09-04), stopping
deliberately short of editing autonomous_cycle.py itself (see the module's
own docstring for why)."""
from __future__ import annotations

import dispatch_admission as da
import scheduler_cycle_hook as hook
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


def test_raises_without_an_executor_rather_than_silently_no_opping(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    try:
        hook.run_scheduler_phase(resource_vector=_healthy_vector())
        assert False, "expected NoExecutorConfigured"
    except hook.NoExecutorConfigured:
        pass


def test_runs_a_real_pass_and_returns_a_json_shaped_summary(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("t1", kind="task", description="t1")

    result = hook.run_scheduler_phase(
        executor_fn=lambda item: {"ok": True}, resource_vector=_healthy_vector(),
    )
    assert result["completed"] == ["t1"]
    assert result["work_graph_status"]["COMPLETED"] == 1
    assert result["admission_refused"] is False
    import json
    json.dumps(result)  # must be JSON-serializable, matching every other cycle phase's result shape


def test_uses_the_canonical_session_id_by_default(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("owned", kind="task", description="owned", owner_path="some/path")
    import session_ownership as so
    monkeypatch.setattr(so, "LEASES_DIR", tmp_path / "leases")
    so.register(hook.CANONICAL_CYCLE_SESSION_ID, "cycle", ["some/path"])

    # the item's own owner_path is leased by the canonical session itself,
    # so it must be treated as OWNED_BY_SELF (schedulable), not externally blocked
    result = hook.run_scheduler_phase(executor_fn=lambda item: {"ok": True}, resource_vector=_healthy_vector())
    assert result["completed"] == ["owned"]


def test_honest_admission_refusal_is_surfaced_not_hidden(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("t1", kind="task", description="t1")
    pressured = _healthy_vector()
    pressured.swap_used_mb = pressured.swap_total_mb  # fully exhausted, mirrors this project's own real host

    result = hook.run_scheduler_phase(executor_fn=lambda item: {"ok": True}, resource_vector=pressured)
    assert result["admission_refused"] is True
    assert result["dynamic_safe_concurrency"] == 0
    assert result["dispatched"] == []
