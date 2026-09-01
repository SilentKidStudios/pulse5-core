#!/usr/bin/env python3
"""
OMNI_GOD_MODE_V1 Phase 3 -- isolated TH3 -> Omni router integration test.

Proves the FULL canonical path a real TH3 submit_work payload takes:

    TH3-style submit_work payload
    -> canonical Proposal creation (evolution/proposal.py::create)
    -> canonical engineering task normalization (advance.py::_build_task_text)
    -> canonical eligibility + engine router (advance.py::advance_one ->
       _implementation_router -> _rank_engineering_engines, unmodified)
    -> omni_engineer_v1 (advance.py::_run_omni_engineer ->
       omniengineer_harness.submit_job_auto)
    -> complexity classification (omniengineer_harness.classify_complexity)
    -> decomposed execution path (omniengineer_harness.submit_job_decomposed)

Fully isolated / synthetic: no live model call (run_agent_loop and
validation.validate are faked, exactly like tests/test_omniengineer.py's
existing decomposed-path tests), no production promotion, no priority
mutation, no completion-controller code anywhere in this path. The
claude_code engine runner is monkeypatched to simulate an infra_failure so
the REAL, unmodified _rank_engineering_engines()/_implementation_router()
fallback logic naturally reaches omni_engineer_v1 -- this is not bypassing
the router, it is exercising its documented fallback behavior the same way
the module's own docstring says tests should.

Run: python3 tests/test_th3_omni_router_integration.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_ledger
import omniengineer_agent as agent
import omniengineer_harness as harness
import validation as _validation
from evolution import advance
from evolution import proposal as proposal_mod

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# TH3-style submit_work payload -- a real caller of the actual submit_work
# MCP tool sends fields shaped like this (task_type/description/risk); this
# test's job is what happens AFTER that payload becomes a canonical
# Proposal, so the payload itself is only used to derive the Proposal
# fields below, matching how a real intake would map it.
_TH3_SUBMIT_WORK_PAYLOAD = {
    "task_type": "code_edit",
    "risk_score": "low",
    "observed_weakness": (
        "PHASE 1: inspect calc.py and calc_test.py -- calc.py has no discount() method. "
        "PHASE 2: design the missing method's validation rules."
    ),
    "proposed_upgrade": (
        "PHASE 3: implement discount() in calc.py. PHASE 4: test it via calc_test.py, "
        "then repair and validate any failures found before finishing."
    ),
    "origin": "th3_submit_work_synthetic_test",
}


def _fake_validate_pass(*a, **k):
    return type("V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()


def test_th3_submit_work_payload_reaches_omni_decomposed_path() -> None:
    # Sanity-check the classifier BEFORE running the full pipeline, so a
    # future change to the classifier's thresholds fails loudly here rather
    # than silently degrading this integration test into exercising the
    # simple path instead.
    p_probe = proposal_mod.Proposal(
        proposal_id="probe", created_at="x",
        observed_weakness=_TH3_SUBMIT_WORK_PAYLOAD["observed_weakness"],
        proposed_upgrade=_TH3_SUBMIT_WORK_PAYLOAD["proposed_upgrade"],
        risk_score="low", origin="probe",
    )
    probe_decision = harness.classify_complexity(advance._build_task_text(p_probe))
    check("PRECONDITION: the synthetic payload's task text classifies as decomposition-eligible",
          probe_decision.decomposition_eligible is True, probe_decision.signals)

    # -- TH3 submit_work payload -> canonical Proposal creation --
    p = proposal_mod.create(
        observed_weakness=_TH3_SUBMIT_WORK_PAYLOAD["observed_weakness"],
        proposed_upgrade=_TH3_SUBMIT_WORK_PAYLOAD["proposed_upgrade"],
        risk_score=_TH3_SUBMIT_WORK_PAYLOAD["risk_score"],
        origin=_TH3_SUBMIT_WORK_PAYLOAD["origin"],
        paid_resources_allowed=False,  # matches th3_mcp's own submit_work default at its real external call site
    )
    check("TH3_ENTRY_REACHED: canonical Proposal created with a real proposal_id",
          bool(p.proposal_id), p.proposal_id)
    check("PROPOSAL_PIPELINE_REACHED: proposal starts in an eligible status", p.status in
          (proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED), p.status)

    # -- force the REAL, unmodified engine-priority fallback: claude_code
    # infra_failure -> falls through to omni_engineer_v1, exactly per
    # _rank_engineering_engines()'s documented behavior. --
    original_claude_runner = advance._ENGINE_RUNNERS["claude_code"]
    advance._ENGINE_RUNNERS["claude_code"] = lambda task_text, requested_by, proposal_id: (
        advance.EngineAttempt("claude_code", "infra_failure", "claude_code unavailable (test fixture)", None, "infra_failure"),
        None,
    )

    # -- fake the model call and validation deep inside the real
    # submit_job_decomposed() pipeline -- everything else (job_ledger,
    # authority_policy.classify, the phase loop, checkpointing) runs for
    # real. --
    original_run = harness.run_agent_loop
    original_validate = _validation.validate
    decomposed_calls = {"n": 0}
    original_submit_job_decomposed = harness.submit_job_decomposed
    original_submit_job = harness.submit_job

    def spy_submit_job_decomposed(*a, **k):
        decomposed_calls["n"] += 1
        return original_submit_job_decomposed(*a, **k)

    def fail_if_called_submit_job(*a, **k):
        raise AssertionError("simple submit_job() was called -- complexity classification should have routed to submit_job_decomposed()")

    harness.run_agent_loop = lambda task_text, workdir, **k: agent.AgentRunResult(
        task="x", sandbox="x", model=k.get("model", "x"), final_action="finish",
        summary_or_reason="synthetic phase completed", commands_executed=[],
    )
    _validation.validate = _fake_validate_pass
    harness.submit_job_decomposed = spy_submit_job_decomposed
    harness.submit_job = fail_if_called_submit_job

    try:
        result = advance.advance_one(p.proposal_id, requested_by="th3_submit_work_synthetic_test")
    finally:
        advance._ENGINE_RUNNERS["claude_code"] = original_claude_runner
        harness.run_agent_loop = original_run
        _validation.validate = original_validate
        harness.submit_job_decomposed = original_submit_job_decomposed
        harness.submit_job = original_submit_job

    check("CANONICAL_ROUTER_REACHED: engine router ran and produced a final_status",
          result.final_status is not None, result.final_status)
    check("OMNI_ENGINEER_SELECTED: the real (unmodified) router selected omni_engineer after claude_code's infra_failure",
          result.selected_engine == "omni_engineer", result.selected_engine)
    check("DECOMPOSED_PATH_REACHED: submit_job_decomposed() was actually called, not submit_job()",
          decomposed_calls["n"] == 1, decomposed_calls)
    check("PARENT_JOB_ID_PRESERVED: AdvancementResult carries the real job_id",
          bool(result.implementation_job_id), result.implementation_job_id)

    p_after = proposal_mod.load(p.proposal_id)
    check("PARENT_JOB_ID_PRESERVED (durable): proposal.implementation_job_ids includes this job_id",
          result.implementation_job_id in (p_after.implementation_job_ids or []), p_after.implementation_job_ids)

    record = job_ledger.load(result.implementation_job_id)
    check("PHASE_STATE_PRESERVED: durable ledger record has phase entries from the real decomposed run",
          bool(record and record.phases), record.phases if record else None)
    check("PHASE_STATE_PRESERVED: all 3 base phases ran (inspect/implement/test)",
          record is not None and [ph["name"] for ph in record.phases[:3]] == ["inspect", "implement", "test"],
          record.phases if record else None)
    check("no promotion occurred as a side effect of this test",
          record is not None and record.state != job_ledger.JobState.PROMOTED.value if hasattr(job_ledger.JobState, "PROMOTED") else True,
          "n/a")


if __name__ == "__main__":
    test_th3_submit_work_payload_reaches_omni_decomposed_path()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
