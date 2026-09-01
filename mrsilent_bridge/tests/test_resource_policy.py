#!/usr/bin/env python3
"""
Tests for the paid_resources_allowed HARD eligibility policy added to
studio_router.rank()/_evaluate() and threaded through
evolution/advance.py._implementation_router()/advance_one() and
evolution/proposal.py's Proposal.paid_resources_allowed field.

Root cause this closes: a live canary submitted with a "no paid resources"
constraint was routed to claude_code_engineer_v1 (metered_api) because no
such constraint existed anywhere in the pipeline -- studio_router.rank()
had no concept of it, evolution/advance.py's engineering-engine reordering
only ever REORDERS an already-accepted list (duty-cycle evidence, not cost),
and Proposal had no field to carry the constraint from submission to real
execution time.

All routing-decision tests here monkeypatch evolution.advance._ENGINE_RUNNERS
and organ_discovery.duty_check, same pattern as tests/test_evolution_advance.py,
so this file is fast/deterministic and spends no real Claude Code/Ollama call.

Run: python3 tests/test_resource_policy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capability_registry
import organ_discovery
import studio_router
from evolution import advance
from evolution import proposal as proposal_mod

# Reuse the existing test module's fixtures rather than duplicating them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_evolution_advance as _tea  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def test_rank_paid_false_excludes_metered_engineering_candidates() -> None:
    accepted_ids = {ev.capability_id for ev in studio_router.rank("code_edit", allow_founder_gated=False, allow_paid=False) if ev.accepted}
    check("omni_engineer_v1 (free/local) still accepted with allow_paid=False",
          "omni_engineer_v1" in accepted_ids, str(accepted_ids))
    check("claude_code_engineer_v1 (metered_api) excluded with allow_paid=False",
          "claude_code_engineer_v1" not in accepted_ids, str(accepted_ids))

    rejected = {ev.capability_id: ev.reason for ev in studio_router.rank("code_edit", allow_founder_gated=False, allow_paid=False) if not ev.accepted}
    check("claude_code's rejection reason explicitly names the resource policy, not a fabricated unrelated reason",
          "paid resource excluded by policy" in rejected.get("claude_code_engineer_v1", ""),
          rejected.get("claude_code_engineer_v1"))


def test_rank_paid_true_preserves_existing_behavior() -> None:
    accepted_ids = {ev.capability_id for ev in studio_router.rank("code_edit", allow_founder_gated=False) if ev.accepted}
    check("default allow_paid=True still accepts claude_code_engineer_v1 (no behavior change for existing callers)",
          "claude_code_engineer_v1" in accepted_ids, str(accepted_ids))
    check("default allow_paid=True still accepts omni_engineer_v1",
          "omni_engineer_v1" in accepted_ids, str(accepted_ids))


def test_proposal_paid_resources_allowed_field() -> None:
    p_default = proposal_mod.create("synthetic test: default resource field", "n/a", risk_score="low", origin="manual")
    check("Proposal.paid_resources_allowed defaults to True (backward-compatible for the existing proposal store)",
          p_default.paid_resources_allowed is True, p_default.paid_resources_allowed)
    _tea._cleanup(p_default.proposal_id, "synthetic resource-policy field test — cleaned up")

    p_restricted = proposal_mod.create("synthetic test: explicit resource field", "n/a", risk_score="low",
                                        origin="manual", paid_resources_allowed=False)
    check("Proposal.paid_resources_allowed persists an explicit False",
          p_restricted.paid_resources_allowed is False, p_restricted.paid_resources_allowed)
    reloaded = proposal_mod.load(p_restricted.proposal_id)
    check("paid_resources_allowed survives a real disk round-trip (load() after create())",
          reloaded.paid_resources_allowed is False, reloaded.paid_resources_allowed)
    _tea._cleanup(p_restricted.proposal_id, "synthetic resource-policy field test — cleaned up")


def _run_routed_paid(paid_resources_allowed: bool, duty_fn, *, claude_must_not_be_called: bool = False):
    """Same shape as test_evolution_advance._run_routed, but creates the
    proposal with an explicit paid_resources_allowed and always mocks BOTH
    engine runners (never a live call)."""
    p = proposal_mod.create("synthetic test: resource-policy routing", "prove paid_resources_allowed is a hard filter",
                             risk_score="low", origin="manual", paid_resources_allowed=paid_resources_allowed)
    original_duty = organ_discovery.duty_check
    original_claude = advance._ENGINE_RUNNERS["claude_code"]
    original_omni = advance._ENGINE_RUNNERS["omni_engineer"]

    def claude_runner(task_text, requested_by, proposal_id):
        if claude_must_not_be_called:
            raise AssertionError("claude_code runner should not have been called under paid_resources_allowed=False")
        job = _tea._fake_real_job(f"test-fake-claude-ok-{proposal_id[:8]}")
        proposal_mod.append_implementation_job(proposal_id, job.job_id, engine="claude_code", note="fake test job")
        return advance.EngineAttempt("claude_code", "ran_cleanly", "fake claude_code success", job.job_id, job.status, True), job

    def omni_runner(task_text, requested_by, proposal_id):
        job = _tea._fake_real_job(f"test-fake-omni-ok-{proposal_id[:8]}")
        proposal_mod.append_implementation_job(proposal_id, job.job_id, engine="omni_engineer", note="fake test job")
        return advance.EngineAttempt("omni_engineer", "ran_cleanly", "fake omni_engineer success", job.job_id, job.status, True), job

    organ_discovery.duty_check = duty_fn
    advance._ENGINE_RUNNERS["claude_code"] = claude_runner
    advance._ENGINE_RUNNERS["omni_engineer"] = omni_runner
    try:
        result = advance.advance_one(p.proposal_id)
    finally:
        organ_discovery.duty_check = original_duty
        advance._ENGINE_RUNNERS["claude_code"] = original_claude
        advance._ENGINE_RUNNERS["omni_engineer"] = original_omni
    _tea._cleanup(p.proposal_id, "synthetic resource-policy routing test — cleaned up")
    return result


def test_paid_resources_allowed_false_never_calls_claude_code_even_when_duty_prefers_it() -> None:
    """The real live incident this closes: duty_check() currently prefers
    claude_code as primary (see evolution/advance.py's own default-order
    reason string). Prove that a hard paid_resources_allowed=False on the
    PROPOSAL overrides that duty preference entirely -- claude_code's
    runner must never even be invoked, regardless of how favorably
    duty_check() rates it."""
    duty_strongly_prefers_claude = _tea._fake_duty(_tea._HEALTHY_CLAUDE, _tea._engine_stats(confidence="low", success_rate=0.1, sample_size=1))
    result = _run_routed_paid(False, duty_strongly_prefers_claude, claude_must_not_be_called=True)
    check("omni_engineer selected despite duty_check() strongly favoring claude_code, because paid_resources_allowed=False",
          result.selected_engine == "omni_engineer", str(result.selected_engine))
    check("reaches promotion_candidate via the free/local engine",
          result.final_status == "promotion_candidate", result.final_status)


def test_paid_resources_allowed_true_preserves_normal_duty_cycle_routing() -> None:
    """Regression guard: paid_resources_allowed=True (the default) must not
    change the existing evidence-based engineering-engine selection at all."""
    result = _run_routed_paid(True, _tea._fake_duty(_tea._HEALTHY_CLAUDE, _tea._HEALTHY_OMNI))
    check("claude_code (duty-preferred, healthy) still selected when paid_resources_allowed=True",
          result.selected_engine == "claude_code", str(result.selected_engine))
    check("reaches promotion_candidate normally",
          result.final_status == "promotion_candidate", result.final_status)


def test_no_eligible_free_local_route_does_not_fall_through_to_paid() -> None:
    """If the ONLY capable engineering candidate for a task is paid, and
    paid_resources_allowed=False, the router must NEVER silently fall
    through to it -- it must report no eligible engine, same as a genuine
    capability gap, and the proposal stays PROPOSED (bounded, retriable),
    never routed to the paid engine."""
    real_load = capability_registry.load

    def only_paid_registry():
        return [e for e in real_load() if e.get("capability_id") in ("claude_code_engineer_v1", "codex_engineer_v1")]

    capability_registry.load = only_paid_registry
    try:
        accepted = [ev for ev in studio_router.rank("code_edit", allow_founder_gated=False, allow_paid=False) if ev.accepted]
        check("zero accepted candidates when the only capable engines are paid and paid_resources_allowed=False",
              len(accepted) == 0, str(accepted))

        result = _run_routed_paid(False, _tea._fake_duty(_tea._HEALTHY_CLAUDE, _tea._HEALTHY_OMNI), claude_must_not_be_called=True)
        check("no engine selected (never silently falls through to the paid engine)",
              result.selected_engine is None, str(result.selected_engine))
        check("proposal is NOT rejected outright -- stays 'proposed' (retriable, same as any genuine capability gap), never routed to the paid engine",
              result.final_status == proposal_mod.ProposalStatus.PROPOSED, result.final_status)
    finally:
        capability_registry.load = real_load


if __name__ == "__main__":
    test_rank_paid_false_excludes_metered_engineering_candidates()
    test_rank_paid_true_preserves_existing_behavior()
    test_proposal_paid_resources_allowed_field()
    test_paid_resources_allowed_false_never_calls_claude_code_even_when_duty_prefers_it()
    test_paid_resources_allowed_true_preserves_normal_duty_cycle_routing()
    test_no_eligible_free_local_route_does_not_fall_through_to_paid()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
