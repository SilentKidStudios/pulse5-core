#!/usr/bin/env python3
"""
PROPOSAL COMPLETENESS GATE (2026-09-07) — direct tests of evolution/
proposal.py::proposal_completeness()/refine()/auto_execute_eligible() and
evolution/advance.py::_eligible()'s new founder_gated completeness check.

Real, live proposal 8d0d01cb-d2f5-4638-b5f5-3b3945de07bf is the motivating
example throughout: risk_score='founder_gated', observed_weakness/
proposed_upgrade set, but implementation_scope/validation_plan/canary_plan/
every protected-action flag all unset — exactly the shape this gate exists
to keep out of Founder review until refined.

Same plain-script style as tests/test_evolution_advance.py (advance.py
imports cleanly in this worktree — see that file's own docstring). Proposal
store writes go to evolution/proposal.py's real PROPOSALS_DIR (this
worktree's own local, untracked copy — never the live host's), cleaned up
via _cleanup() at the end of each test, per this project's standing
test-artifact policy.

Run: python3 tests/test_proposal_completeness_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution import advance
from evolution import founder_request
from evolution import proposal as proposal_mod

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    assert condition, f"{name}" + (f" — {detail}" if detail else "")


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)


_COMPLETE_KWARGS = dict(
    implementation_scope="bounded, self-contained change under test",
    non_file_scope=None,
    validation_plan="validation.validate() on the sandbox output",
    canary_plan="independent re-validation pass",
    paid_resources_required=False, credential_changes_required=False,
    production_promotion_required=False, destructive_action_required=False,
    model_change_required=False, isolation_change_required=False, campaign_collision=False,
)


def _make_complete(risk_score: str, *, source_paths=None, overrides=None) -> proposal_mod.Proposal:
    p = proposal_mod.create("weak spot under test", "fix it", risk_score=risk_score,
                             source_paths=source_paths or ["mrsilent_bridge/some_module.py"])
    kwargs = dict(_COMPLETE_KWARGS)
    kwargs.pop("non_file_scope")  # source_paths already satisfies the affected-components requirement
    if overrides:
        kwargs.update(overrides)
    return proposal_mod.refine(p.proposal_id, **kwargs)


# ---- 1. incomplete proposal rejected from decision-ready state -----------

def test_1_bare_proposal_is_not_decision_ready() -> None:
    p = proposal_mod.create("MR_SILENT_APP_COMPLETION needs scoping", "n/a", risk_score="founder_gated")
    try:
        complete, missing = proposal_mod.proposal_completeness(p)
        check("a bare proposal is not decision-ready", complete is False)
        check("missing includes implementation_scope", "implementation_scope" in missing, str(missing))
        check("missing includes validation_plan", "validation_plan" in missing, str(missing))
        check("missing includes canary_plan", "canary_plan" in missing, str(missing))
        check("missing includes affected_components_or_non_file_scope",
              "affected_components_or_non_file_scope" in missing, str(missing))
        check("missing includes every protected-action boolean",
              all(f in missing for f in (
                  "paid_resources_required", "credential_changes_required", "production_promotion_required",
                  "destructive_action_required", "model_change_required", "isolation_change_required",
                  "campaign_collision")), str(missing))
    finally:
        _cleanup(p.proposal_id, "test 1 cleanup")


def test_1b_real_8d0_shape_is_not_decision_ready() -> None:
    """Mirrors the EXACT real live shape of 8d0d01cb-d2f5-4638-b5f5-
    3b3945de07bf (paid_resources_allowed=True is a ceiling, never a
    completeness answer — that field is untouched by this gate)."""
    p = proposal_mod.create(
        "Founder Top-10 governing priority 'MR_SILENT_APP_COMPLETION' has no actionable or pending proposal",
        "Founder/human review required to scope this strategic priority into an actionable, bounded "
        "engineering delta — this proposal deliberately does not scope itself.",
        risk_score="founder_gated", origin="discovered",
        fingerprint="founder_priority_unproposed:MR_SILENT_APP_COMPLETION", paid_resources_allowed=True,
    )
    try:
        complete, missing = proposal_mod.proposal_completeness(p)
        check("the real 8d0 shape is not decision-ready", complete is False, str(missing))
    finally:
        _cleanup(p.proposal_id, "test 1b cleanup")


# ---- 2. incomplete proposal cannot execute --------------------------------

def test_2_incomplete_founder_gated_proposal_is_ineligible_even_with_approval() -> None:
    p = proposal_mod.create("risky, unscoped", "n/a", risk_score="founder_gated")
    try:
        escalation = founder_request.request_founder_decision(
            subject=p.proposal_id, finding="f", capability_needed="approve proposal",
            reason_required="r", recommended_action="advance", risk="founder_gated",
            affected={"proposal_id": p.proposal_id},
        )
        founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")
        ok, reason = advance._eligible(p)
        check("an incomplete founder_gated proposal is ineligible even once approved", ok is False)
        check("the reason names the completeness gate, not the approval state",
              "decision-ready" in reason, reason)
    finally:
        _cleanup(p.proposal_id, "test 2 cleanup")


def test_2b_incomplete_low_risk_proposal_is_unaffected_no_regression() -> None:
    """The founder_gated-only scoping of this gate: a bare low-risk
    proposal (the existing, proven-safe lane — see advance.py's own
    docstring) is completely unaffected — _eligible() never even consults
    proposal_completeness() for risk_score=='low'."""
    p = proposal_mod.create("ordinary low-risk finding", "fix it", risk_score="low")
    try:
        ok, reason = advance._eligible(p)
        check("a bare low-risk proposal is still eligible — zero regression of the existing lane",
              ok is True, reason)
    finally:
        _cleanup(p.proposal_id, "test 2b cleanup")


# ---- 3. incomplete proposal does not notify Founder -----------------------
# (covered live in tests/test_proposal_work_bridge.py::
#  test_incomplete_founder_gated_proposal_is_blocked_refinement_not_founder
#  — that's the actual Founder-visibility mechanism; duplicated here as a
#  pure-classifier check with no WorkGraph involved.)

def test_3_is_decision_ready_is_pure_and_never_mutates() -> None:
    p = proposal_mod.create("risky, unscoped", "n/a", risk_score="founder_gated")
    try:
        before = proposal_mod.load(p.proposal_id)
        proposal_mod.is_decision_ready(p)
        proposal_mod.proposal_completeness(p)
        after = proposal_mod.load(p.proposal_id)
        check("proposal_completeness()/is_decision_ready() never mutate the proposal",
              before == after)
    finally:
        _cleanup(p.proposal_id, "test 3 cleanup")


# ---- 4. refinement can make a legitimate incomplete proposal complete -----

def test_4_refine_makes_a_bare_proposal_decision_ready() -> None:
    p = proposal_mod.create("risky, unscoped", "n/a", risk_score="founder_gated")
    try:
        complete_before, _ = proposal_mod.proposal_completeness(p)
        check("starts incomplete", complete_before is False)
        refined = proposal_mod.refine(p.proposal_id, **{k: v for k, v in _COMPLETE_KWARGS.items() if k != "non_file_scope"},
                                       non_file_scope="deliberately no canonical files touched")
        complete_after, missing = proposal_mod.proposal_completeness(refined)
        check("refine() makes it decision-ready", complete_after is True, str(missing))
        check("refine() records a 'refined' history event",
              any(h.get("event") == "refined" for h in refined.history), str(refined.history))
    finally:
        _cleanup(p.proposal_id, "test 4 cleanup")


def test_4b_refine_rejects_unknown_fields() -> None:
    p = proposal_mod.create("x", "y", risk_score="low")
    try:
        try:
            proposal_mod.refine(p.proposal_id, risk_score="founder_gated")
            check("refine() rejects a non-completeness-gate field", False)
        except ValueError:
            check("refine() rejects a non-completeness-gate field", True)
    finally:
        _cleanup(p.proposal_id, "test 4b cleanup")


# ---- 5. complete ordinary safe proposal can become autonomous-eligible ----

def test_5_complete_low_risk_ordinary_proposal_is_auto_execute_eligible() -> None:
    p = _make_complete("low")
    try:
        eligible, reasons = proposal_mod.auto_execute_eligible(p)
        check("a complete, ordinary, low-risk proposal is auto-execute eligible", eligible is True, str(reasons))
    finally:
        _cleanup(p.proposal_id, "test 5 cleanup")


# ---- 6-12. each protected flag independently keeps it Founder-gated/held --

def _protected_flag_case(field: str) -> None:
    p = _make_complete("low", overrides={field: True})
    try:
        eligible, reasons = proposal_mod.auto_execute_eligible(p)
        check(f"a complete low-risk proposal with {field}=True is NOT auto-execute eligible",
              eligible is False, str(reasons))
        check(f"the {field} flag itself is named in the reasons",
              any(field in r for r in reasons), str(reasons))
    finally:
        _cleanup(p.proposal_id, f"protected flag case {field} cleanup")


def test_6_paid_resource_proposal_remains_held() -> None:
    _protected_flag_case("paid_resources_required")


def test_7_credential_change_proposal_remains_held() -> None:
    _protected_flag_case("credential_changes_required")


def test_8_production_promotion_proposal_remains_held() -> None:
    _protected_flag_case("production_promotion_required")


def test_9_destructive_proposal_remains_held() -> None:
    _protected_flag_case("destructive_action_required")


def test_10_model_delete_replace_remains_held() -> None:
    _protected_flag_case("model_change_required")


def test_11_isolation_change_remains_held() -> None:
    _protected_flag_case("isolation_change_required")


def test_12_active_campaign_collision_remains_held() -> None:
    _protected_flag_case("campaign_collision")


# ---- 13. validation/canary omission blocks eligibility --------------------

def test_13a_missing_validation_plan_blocks_eligibility() -> None:
    p = _make_complete("low", overrides={"validation_plan": None})
    try:
        eligible, reasons = proposal_mod.auto_execute_eligible(p)
        check("missing validation_plan blocks auto-execute eligibility", eligible is False, str(reasons))
    finally:
        _cleanup(p.proposal_id, "test 13a cleanup")


def test_13b_missing_canary_plan_blocks_eligibility() -> None:
    p = _make_complete("low", overrides={"canary_plan": None})
    try:
        eligible, reasons = proposal_mod.auto_execute_eligible(p)
        check("missing canary_plan blocks auto-execute eligibility", eligible is False, str(reasons))
    finally:
        _cleanup(p.proposal_id, "test 13b cleanup")


# ---- 14. founder_gated semantics remain unchanged (for a COMPLETE one) ----

def test_14a_complete_founder_gated_without_approval_still_blocked() -> None:
    p = _make_complete("founder_gated")
    try:
        ok, reason = advance._eligible(p)
        check("a complete founder_gated proposal without approval is still ineligible", ok is False)
        check("the reason is the ORIGINAL awaiting-Founder-review message, unchanged",
              "awaiting explicit Founder review" in reason, reason)
    finally:
        _cleanup(p.proposal_id, "test 14a cleanup")


def test_14b_complete_founder_gated_with_approval_is_eligible_exactly_as_before() -> None:
    p = _make_complete("founder_gated")
    try:
        escalation = founder_request.request_founder_decision(
            subject=p.proposal_id, finding="f", capability_needed="approve proposal",
            reason_required="r", recommended_action="advance", risk="founder_gated",
            affected={"proposal_id": p.proposal_id},
        )
        founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")
        ok, reason = advance._eligible(p)
        check("a complete, approved founder_gated proposal is eligible — unchanged semantics", ok is True, reason)
    finally:
        _cleanup(p.proposal_id, "test 14b cleanup")


def test_14c_complete_founder_gated_denied_is_ineligible() -> None:
    p = _make_complete("founder_gated")
    try:
        escalation = founder_request.request_founder_decision(
            subject=p.proposal_id, finding="f", capability_needed="approve proposal",
            reason_required="r", recommended_action="advance", risk="founder_gated",
            affected={"proposal_id": p.proposal_id},
        )
        founder_request.resolve_founder_decision(escalation["escalation_id"], "denied")
        ok, reason = advance._eligible(p)
        check("a complete, denied founder_gated proposal remains ineligible — unchanged semantics",
              ok is False)
        check("the denial reason is the original one, unchanged", "'denied'" in reason, reason)
    finally:
        _cleanup(p.proposal_id, "test 14c cleanup")


# ---- 15. exact Founder approvals remain proposal-specific -----------------

def test_15_approval_for_one_complete_proposal_never_leaks_to_another() -> None:
    a = _make_complete("founder_gated")
    b = _make_complete("founder_gated")
    try:
        escalation = founder_request.request_founder_decision(
            subject=a.proposal_id, finding="f", capability_needed="approve proposal",
            reason_required="r", recommended_action="advance", risk="founder_gated",
            affected={"proposal_id": a.proposal_id},
        )
        founder_request.resolve_founder_decision(escalation["escalation_id"], "approved")
        ok_a, _ = advance._eligible(proposal_mod.load(a.proposal_id))
        ok_b, reason_b = advance._eligible(proposal_mod.load(b.proposal_id))
        check("proposal A (approved) is eligible", ok_a is True)
        check("proposal B (never approved) remains ineligible despite A's approval", ok_b is False, reason_b)
    finally:
        _cleanup(a.proposal_id, "test 15 cleanup a")
        _cleanup(b.proposal_id, "test 15 cleanup b")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")
