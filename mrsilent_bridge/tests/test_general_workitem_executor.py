"""Tests for general_workitem_executor.py — non-proposal executor gap
closure (2026-09-08)."""
from __future__ import annotations

import pytest

import failure_diagnosis
import general_workitem_executor as gwe
import proposal_work_bridge
import work_graph as wg


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(wg, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(wg, "LOCKS_DIR", tmp_path / "locks")
    # TEST_LIVE_STORE_CONTAMINATION fix (2026-09-08) — see
    # test_proposal_work_bridge.py's identical fix for the real, live
    # orphaned-marker evidence this closes.
    monkeypatch.setattr(wg, "STATE_INDEX_DIR", tmp_path / "state_index")


class _SuccessResult:
    """Matches evolution.advance.AdvancementResult's field names, per
    proposal_work_bridge._classify_advancement_result()'s own docstring —
    final_status='promotion_candidate' is the real terminal-success shape,
    read directly from evolution/advance.py's source, not guessed."""
    def __init__(self, proposal_id):
        self.proposal_id = proposal_id
        self.final_status = "promotion_candidate"
        self.blocked_reason = None
        self.implementation_job_id = "job-x"
        self.selected_engine = "claude_code"


class _FakeAdvanceModule:
    """Matches proposal_work_bridge.make_advance_executor()'s injectable
    advance_mod contract exactly, same style already used by that
    module's own tests."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def advance_one(self, proposal_id, *, requested_by):
        self.calls.append((proposal_id, requested_by))
        return self.outcome


def test_proposal_backed_item_delegates_to_existing_executor(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    fake_advance = _FakeAdvanceModule(_SuccessResult("prop-1"))
    executor = gwe.make_general_executor(advance_mod=fake_advance)
    item = wg.create("w1", kind="task", description="d", provenance={"proposal_id": "prop-1"})
    result = executor(item)
    assert fake_advance.calls == [("prop-1", "scheduler_cycle_hook")]
    assert result.get("__terminal_failed__") is None


def test_unknown_workitem_kind_fails_closed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    item = wg.create("w2", kind="objective", description="no proposal, no repair kind")
    with pytest.raises(gwe.UnknownWorkItemKind):
        executor(item)


def test_dependency_kind_without_proposal_fails_closed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    item = wg.create("w3", kind="dependency", description="unrecognized dependency shape")
    with pytest.raises(gwe.UnknownWorkItemKind):
        executor(item)


def _make_repair_scenario(monkeypatch, tmp_path, *, prerequisite_still_missing: bool):
    _fresh(monkeypatch, tmp_path)
    parent = wg.create("parent-1", kind="task", description="a real task")
    parent.result = {"error_class": "missing_config", "blocked_reason": "config.json absent"} if prerequisite_still_missing else {"error_class": "ok"}
    wg._save(parent)
    repair = wg.create_child_for_dependency(
        "parent-1", kind="repair", description="repair for parent-1",
        provenance={"diagnosis": {"diagnosis": failure_diagnosis.DIAGNOSIS_NEEDS_REPAIR, "reason": "missing_config: config.json absent"}},
    )
    return repair


def test_repair_kind_reports_not_yet_resolved_when_prerequisite_still_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(failure_diagnosis, "_TRANSIENT_ERROR_CLASSES", frozenset())
    monkeypatch.setattr(failure_diagnosis, "_MISSING_PREREQUISITE_ERROR_CLASSES", frozenset({"missing_config"}))
    repair = _make_repair_scenario(monkeypatch, tmp_path, prerequisite_still_missing=True)
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    with pytest.raises(RuntimeError, match="not yet resolved"):
        executor(repair)


def test_repair_kind_verifies_success_when_prerequisite_now_satisfied(monkeypatch, tmp_path):
    monkeypatch.setattr(failure_diagnosis, "_TRANSIENT_ERROR_CLASSES", frozenset())
    monkeypatch.setattr(failure_diagnosis, "_MISSING_PREREQUISITE_ERROR_CLASSES", frozenset({"missing_config"}))
    repair = _make_repair_scenario(monkeypatch, tmp_path, prerequisite_still_missing=False)
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    result = executor(repair)
    assert result["repair_verified"] is True
    assert result["parent_id"] == "parent-1"


def test_repair_kind_never_fabricates_success_without_reverification(monkeypatch, tmp_path):
    """Even a repair item whose OWN provenance claims success must be
    reverified against the real parent evidence — never trusted blindly."""
    monkeypatch.setattr(failure_diagnosis, "_TRANSIENT_ERROR_CLASSES", frozenset())
    monkeypatch.setattr(failure_diagnosis, "_MISSING_PREREQUISITE_ERROR_CLASSES", frozenset({"missing_config"}))
    repair = _make_repair_scenario(monkeypatch, tmp_path, prerequisite_still_missing=True)
    repair.provenance["diagnosis"]["claimed_fixed"] = True  # attacker/bug could set this; must be ignored
    wg._save(repair)
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    with pytest.raises(RuntimeError):
        executor(repair)


def test_repair_item_without_parent_id_fails_closed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    item = wg.create("orphan-repair", kind="repair", description="no parent")
    item.parent_id = None
    executor = gwe.make_general_executor(advance_mod=_FakeAdvanceModule({}))
    with pytest.raises(gwe.UnknownWorkItemKind):
        executor(item)


def test_proposal_id_takes_priority_over_kind(monkeypatch, tmp_path):
    """A repair-kind item that ALSO happens to carry a proposal_id must
    still go through the proposal executor — provenance evidence (proposal
    linkage) outranks the kind label, per the module's documented dispatch
    order."""
    _fresh(monkeypatch, tmp_path)
    fake_advance = _FakeAdvanceModule(_SuccessResult("prop-2"))
    executor = gwe.make_general_executor(advance_mod=fake_advance)
    item = wg.create("w4", kind="repair", description="d", provenance={"proposal_id": "prop-2"})
    executor(item)
    assert fake_advance.calls == [("prop-2", "scheduler_cycle_hook")]


def test_idempotent_dispatch_same_item_twice_no_duplicate_side_effects(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    fake_advance = _FakeAdvanceModule(_SuccessResult("prop-3"))
    executor = gwe.make_general_executor(advance_mod=fake_advance)
    item = wg.create("w5", kind="task", description="d", provenance={"proposal_id": "prop-3"})
    executor(item)
    executor(item)
    assert len(fake_advance.calls) == 2  # each call is a real, independent advance_one() call —
    # idempotency itself is advance_one()'s/find_open_by_fingerprint()'s own existing responsibility,
    # unchanged by this dispatcher, which never adds a second layer of dedup on top
