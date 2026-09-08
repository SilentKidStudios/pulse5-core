"""A/B oracle for work_graph.py's runnable_items() scale fix (2026-09-08).

_filter_runnable(list_all()) IS the original, pre-fix full-scan algorithm,
extracted verbatim (not reimplemented) — so it is the ground-truth oracle
this compares the new indexed path against, at every real scenario the
original function had to handle correctly. Isolated: never touches real
production work_graph_state/.
"""
from __future__ import annotations

import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")


def _oracle(self_session_id="oracle"):
    """The exact pre-fix algorithm: full scan, then the shared filter."""
    return wg._filter_runnable(wg.list_all(), self_session_id=self_session_id)


def _same(a, b):
    return [i.work_id for i in a] == [i.work_id for i in b]


def test_no_index_yet_falls_back_to_full_scan_behavior(monkeypatch, tmp_path):
    """Before rebuild_state_index() ever runs, the index directories don't
    exist — runnable_items() must fall back to the exact old behavior, not
    silently report zero runnable items."""
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.create("w2", kind="task", description="d2")
    result = wg.runnable_items(self_session_id="s")
    oracle = _oracle()
    assert _same(result, oracle)
    assert len(result) == 2


def test_indexed_path_matches_oracle_after_rebuild(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.create("w2", kind="task", description="d2")
    wg.create("w3", kind="task", description="d3", depends_on=["w1"])  # blocked_dependency
    wg.rebuild_state_index()
    result = wg.runnable_items(self_session_id="s")
    oracle = _oracle()
    assert _same(result, oracle)
    assert set(i.work_id for i in result) == {"w1", "w2"}


def test_indexed_path_stays_correct_across_live_state_transitions(monkeypatch, tmp_path):
    """The real test: after rebuild, further create()/mark_* calls must
    keep the index correctly maintained WITHOUT another rebuild — proving
    the create()/_save() hooks work, not just the one-time backfill."""
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.create("w2", kind="task", description="d2")
    wg.rebuild_state_index()
    assert _same(wg.runnable_items(), _oracle())

    # complete w1 -- must disappear from runnable via the index too
    wg.mark_completed("w1", result={"ok": True})
    assert _same(wg.runnable_items(), _oracle())
    assert set(i.work_id for i in wg.runnable_items()) == {"w2"}

    # create a new item after the rebuild -- index must pick it up live
    wg.create("w3", kind="task", description="d3")
    assert _same(wg.runnable_items(), _oracle())
    assert set(i.work_id for i in wg.runnable_items()) == {"w2", "w3"}

    # block w2 on founder gate -- must disappear
    wg.mark_blocked_founder("w2", note="test")
    assert _same(wg.runnable_items(), _oracle())
    assert set(i.work_id for i in wg.runnable_items()) == {"w3"}


def test_dependency_chain_honored_by_both_paths(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("parent", kind="task", description="p")
    wg.create("child", kind="task", description="c", depends_on=["parent"])
    wg.rebuild_state_index()
    result = wg.runnable_items()
    assert _same(result, _oracle())
    assert set(i.work_id for i in result) == {"parent"}  # child blocked, correctly excluded

    wg.mark_completed("parent", result={"ok": True})
    wg.resume_eligible_parents()  # existing real mechanism that flips blocked_dependency -> runnable
    result = wg.runnable_items()
    assert _same(result, _oracle())
    assert set(i.work_id for i in result) == {"child"}


def test_terminal_items_never_appear_either_path(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.create("w2", kind="task", description="d2")
    wg.rebuild_state_index()
    wg.mark_completed("w1", result={"ok": True})
    wg.mark_terminal_failed("w2", result={"error": "x"})
    result = wg.runnable_items()
    assert _same(result, _oracle())
    assert result == []


def test_repeated_calls_idempotent_no_duplicate_or_lost_items(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    for i in range(20):
        wg.create(f"w{i}", kind="task", description=f"d{i}")
    wg.rebuild_state_index()
    first = wg.runnable_items()
    second = wg.runnable_items()
    third = wg.runnable_items()
    assert _same(first, second) == True
    assert _same(second, third) == True
    assert len(first) == 20
    assert _same(first, _oracle())


def test_rebuild_state_index_is_idempotent_and_self_healing(monkeypatch, tmp_path):
    """Simulates index drift/corruption by deleting the index directory
    entirely mid-flight, then proves rebuild_state_index() restores
    correctness from ground truth (ITEMS_DIR), not from stale index state."""
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.rebuild_state_index()
    import shutil
    shutil.rmtree(tmp_path / "state_index")
    # index gone entirely -- runnable_items() must fall back safely, not crash or lie
    assert _same(wg.runnable_items(), _oracle())
    # rebuild restores the fast path
    counts = wg.rebuild_state_index()
    assert counts.get(wg.WorkState.RUNNABLE) == 1
    assert _same(wg.runnable_items(), _oracle())


def test_priority_ordering_identical_between_paths(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    wg.create("low", kind="task", description="d", priority=1.0)
    wg.create("high", kind="task", description="d", priority=10.0)
    wg.create("mid", kind="task", description="d", priority=5.0)
    wg.rebuild_state_index()
    result = wg.runnable_items()
    oracle = _oracle()
    assert [i.work_id for i in result] == [i.work_id for i in oracle]
    assert [i.work_id for i in result] == ["high", "mid", "low"]


def test_recreating_an_existing_completed_work_id_never_leaves_stale_index_marker(monkeypatch, tmp_path):
    """The exact edge case create()'s clear_others=False fast path could
    have silently broken: re-calling create() on a work_id that already
    exists in a DIFFERENT state (against the established convention every
    real caller follows, but not something create() itself forbids) must
    never leave a stale marker in the old state's index directory — that
    would make the item appear runnable via two different index buckets
    simultaneously."""
    _fresh(monkeypatch, tmp_path)
    wg.create("w1", kind="task", description="d1")
    wg.rebuild_state_index()
    wg.mark_completed("w1", result={"ok": True})
    assert _same(wg.runnable_items(), _oracle())
    assert wg.runnable_items() == []

    # re-create the SAME work_id (violates the real-caller convention, but
    # create() itself must still behave safely)
    wg.create("w1", kind="task", description="d1 again")
    assert _same(wg.runnable_items(), _oracle())
    assert set(i.work_id for i in wg.runnable_items()) == {"w1"}
    # the stale 'completed' marker must be gone, not just superseded
    completed_dir = tmp_path / "state_index" / wg.WorkState.COMPLETED
    assert not (completed_dir / "w1").exists()
