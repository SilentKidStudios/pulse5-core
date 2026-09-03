#!/usr/bin/env python3
"""
Tests for the FOUNDER-APPROVAL-TO-IMPLEMENTATION-AUTHORITY wiring
(evolution/advance.py::_eligible(), extended to accept an exact, canonical,
resolved Founder approval for a founder_gated proposal — evolution/
founder_request.py::exact_proposal_decision(), consuming the SAME durable
escalation records evolution/founder_request.py already uses live).

WHAT THIS CLOSES: founder_request.resolve_founder_decision() durably records
a Founder decision but, before this change, had ZERO wired effect on
evolution/advance.py::_eligible() — which hard-required risk_score=="low".
A real Founder approval of a real founder_gated proposal (937a60b8,
2026-09-03) was mechanically proven to leave the natural
mrsilent-autonomous-cycle.timer cycle still reporting the SAME authority
block, before and after the approval was recorded.

WHAT THIS DELIBERATELY DOES NOT DO:
  - risk_score is NEVER mutated by approval — 937a60b8 stays "founder_gated"
    forever; only _eligible()'s DECISION changes, proven directly below.
  - approval is proposal-specific: matched only by an exact
    payload.affected.proposal_id equality, never by subject/finding
    similarity — proposal A's approval can never leak to proposal B.
  - production promotion, paid-resource activation, and content-level
    protected-action gates (authority_policy.classify(), GATED_ADAPTERS,
    GATED_PATH_MARKERS, GATED_KEYWORDS, studio_router's
    allow_founder_gated=False) are completely untouched by this diff —
    proven by source inspection below, since none of that code was edited.

These tests never call autonomous_cycle.run_cycle() or advance_eligible()
against the real live proposal store's implementation router (which would
dispatch a REAL job) — per this project's doctrine of never manually
triggering the live pipeline. Final live acceptance
(NATURAL_AUTONOMOUS_CYCLE_CAN_CONSUME_APPROVED_FOUNDER_GATED_PROPOSAL) is
proven separately by inspecting a real, naturally-timer-fired cycle record.

Run: python3 tests/test_founder_approval_to_implementation_authority.py
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution import advance  # noqa: E402
from evolution import founder_request  # noqa: E402
from evolution import proposal as proposal_mod  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)
        proposal_mod.record_lesson(proposal_id, note)


def _code_only(fn) -> str:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(tree)


def _make_founder_gated_proposal(tag: str) -> proposal_mod.Proposal:
    return proposal_mod.create(
        observed_weakness=f"synthetic founder_gated delta for approval-authority test {tag}",
        proposed_upgrade="n/a", risk_score="founder_gated", origin="manual")


def _approve(proposal_id: str, tag: str) -> str:
    rec = founder_request.request_founder_decision(
        subject=f"proposal {proposal_id}", finding=f"test finding {tag}",
        capability_needed="advance a founder_gated proposal", reason_required="test",
        recommended_action="test", risk="founder_gated",
        affected={"proposal_id": proposal_id}, requested_by="test")
    founder_request.resolve_founder_decision(rec["escalation_id"], "approved", note=f"test approval {tag}")
    return rec["escalation_id"]


def _deny(proposal_id: str, tag: str) -> str:
    rec = founder_request.request_founder_decision(
        subject=f"proposal {proposal_id}", finding=f"test finding {tag}",
        capability_needed="advance a founder_gated proposal", reason_required="test",
        recommended_action="test", risk="founder_gated",
        affected={"proposal_id": proposal_id}, requested_by="test")
    founder_request.resolve_founder_decision(rec["escalation_id"], "denied", note=f"test denial {tag}")
    return rec["escalation_id"]


# --------------------------------------------------------------------- #
# 1. FOUNDER_GATED_UNAPPROVED_INELIGIBLE
# --------------------------------------------------------------------- #
def test_founder_gated_unapproved_ineligible() -> None:
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        check("no decision recorded yet for a fresh founder_gated proposal",
              founder_request.exact_proposal_decision(p.proposal_id) is None)
        ok, reason = advance._eligible(p)
        check("an unapproved founder_gated proposal is never eligible", ok is False, reason)
        check("the reason names the exact awaiting-review state", "awaiting explicit Founder review" in reason, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic unapproved test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 2. FOUNDER_GATED_DENIED_INELIGIBLE
# --------------------------------------------------------------------- #
def test_founder_gated_denied_ineligible() -> None:
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        _deny(p.proposal_id, tag)
        check("the recorded decision reads back as 'denied'",
              founder_request.exact_proposal_decision(p.proposal_id) == "denied")
        ok, reason = advance._eligible(p)
        check("a denied founder_gated proposal is never eligible", ok is False, reason)
        check("the reason explicitly names the denial (not a generic block)", "'denied'" in reason, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic denied test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 3. FOUNDER_GATED_EXACT_APPROVAL_ENABLES_IMPLEMENTATION
# --------------------------------------------------------------------- #
def test_founder_gated_exact_approval_enables_implementation() -> None:
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        _approve(p.proposal_id, tag)
        check("the recorded decision reads back as 'approved'",
              founder_request.exact_proposal_decision(p.proposal_id) == "approved")
        ok, reason = advance._eligible(p)
        check("an exactly Founder-approved founder_gated proposal becomes eligible", ok is True, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic approval test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 4. APPROVAL_DOES_NOT_MUTATE_RISK_SCORE
# --------------------------------------------------------------------- #
def test_approval_does_not_mutate_risk_score() -> None:
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        before = (p.status, p.risk_score)
        _approve(p.proposal_id, tag)
        advance._eligible(proposal_mod.load(p.proposal_id))
        after = proposal_mod.load(p.proposal_id)
        check("risk_score/status are byte-for-byte unchanged by approval + eligibility check",
              (after.status, after.risk_score) == before, f"{before} -> {(after.status, after.risk_score)}")
        check("risk_score is still literally 'founder_gated', never silently downgraded to 'low'",
              after.risk_score == "founder_gated", after.risk_score)
    finally:
        _cleanup(p.proposal_id, "synthetic no-mutation test — cleaned up per test-artifact policy")

    import re
    for fn in (founder_request.exact_proposal_decision, advance._eligible):
        code = _code_only(fn)
        check(f"{fn.__name__} never assigns .risk_score on a proposal (comparison '==' excluded)",
              re.search(r"\.risk_score\s*=(?!=)", code) is None, code)
        check(f"{fn.__name__} never calls proposal_mod.advance()/save() (read-only)",
              "proposal_mod.advance(" not in code and ".save(" not in code, code)


# --------------------------------------------------------------------- #
# 5/6. APPROVAL_SCOPE_IS_PROPOSAL_SPECIFIC / APPROVAL_FOR_A_DOES_NOT_AUTHORIZE_B
# --------------------------------------------------------------------- #
def test_approval_scope_is_proposal_specific_a_does_not_authorize_b() -> None:
    tag = uuid.uuid4().hex[:12]
    a = _make_founder_gated_proposal(f"A-{tag}")
    b = _make_founder_gated_proposal(f"B-{tag}")
    try:
        _approve(a.proposal_id, tag)
        check("A's decision is 'approved'", founder_request.exact_proposal_decision(a.proposal_id) == "approved")
        check("B has no decision at all -- A's approval never leaked to B",
              founder_request.exact_proposal_decision(b.proposal_id) is None)
        ok_a, _ = advance._eligible(a)
        ok_b, reason_b = advance._eligible(b)
        check("A (approved) is eligible", ok_a is True)
        check("B (never approved) is NOT eligible, despite A being approved moments earlier",
              ok_b is False, reason_b)
    finally:
        _cleanup(a.proposal_id, "synthetic scope test A — cleaned up per test-artifact policy")
        _cleanup(b.proposal_id, "synthetic scope test B — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 7. LOW_RISK_ELIGIBILITY_UNCHANGED
# --------------------------------------------------------------------- #
def test_low_risk_eligibility_unchanged() -> None:
    p = proposal_mod.create(
        observed_weakness="synthetic ordinary low-risk delta, unaffected by this diff",
        proposed_upgrade="n/a", risk_score="low", origin="manual")
    try:
        ok, reason = advance._eligible(p)
        check("an ordinary low-risk proposal remains eligible exactly as before, no Founder decision needed",
              ok is True, reason)
        check("founder_request has no opinion on a low-risk proposal (never consulted)",
              founder_request.exact_proposal_decision(p.proposal_id) is None)
    finally:
        _cleanup(p.proposal_id, "synthetic low-risk-unchanged test — cleaned up per test-artifact policy")


def test_medium_risk_still_ineligible() -> None:
    """Not explicitly named in the required list, but the required semantics
    ('medium' stays ineligible) deserve direct proof, not just inference."""
    p = proposal_mod.create(
        observed_weakness="synthetic medium-risk delta", proposed_upgrade="n/a",
        risk_score="medium", origin="manual")
    try:
        ok, reason = advance._eligible(p)
        check("'medium' risk_score remains ineligible (only 'low', or approved 'founder_gated')",
              ok is False, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic medium-risk test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 8/9. PRODUCTION_PROMOTION_REMAINS_GATED / PAID_RESOURCE_GATE_PRESERVED /
# PROTECTED_ACTION_GATES_PRESERVED -- all proven by source inspection: none
# of this diff's code touches promotion, paid-resource routing, or the
# content-level authority_policy gates at all.
# --------------------------------------------------------------------- #
def test_production_promotion_remains_gated() -> None:
    src = Path(advance.__file__).read_text()
    check("evolution/advance.py never imports or calls promotion.promote(...)",
          "promotion.promote(" not in src, "found a promotion.promote( call")
    # AST-based, not substring: the module's own docstring legitimately
    # CONTAINS the phrase "founder_approved=True" while asserting this exact
    # invariant in prose ("this module never sets founder_approved=True for
    # itself, ever") -- a raw substring check would false-positive on that
    # sentence. This instead verifies no actual Call node in the real code
    # ever passes the keyword argument founder_approved=True.
    tree = ast.parse(src)
    violations = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "founder_approved" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    ]
    check("no real code in evolution/advance.py ever passes founder_approved=True",
          violations == [], f"{len(violations)} violation(s) at line(s) {[v.lineno for v in violations]}")
    eligible_code = _code_only(advance._eligible)
    check("_eligible() itself never references promotion at all",
          "promotion" not in eligible_code.lower(), eligible_code)


def test_paid_resource_gate_preserved() -> None:
    src = Path(advance.__file__).read_text()
    check("advance_one() still threads the proposal's OWN paid_resources_allowed field unconditionally "
          "(unchanged by this diff -- not made conditional on risk_score or approval)",
          "allow_paid=getattr(p, \"paid_resources_allowed\", True)" in src, "expected thread not found")
    eligible_code = _code_only(advance._eligible)
    check("_eligible() itself never references paid_resources_allowed (unrelated gate, untouched)",
          "paid_resources_allowed" not in eligible_code, eligible_code)


def test_protected_action_gates_preserved() -> None:
    src = Path(advance.__file__).read_text()
    check("the implementation router still hardcodes allow_founder_gated=False regardless of proposal "
          "risk_score or approval (an approved founder_gated PROPOSAL still cannot reach a founder-gated "
          "ADAPTER like Codex)",
          "allow_founder_gated=False" in src, "expected hardcoded allow_founder_gated=False not found")
    eligible_code = _code_only(advance._eligible)
    check("_eligible() never references authority_policy's gate constants directly "
          "(that independent, content-based gate runs unchanged at real dispatch time)",
          "GATED_TOOLS" not in eligible_code and "GATED_PATH_MARKERS" not in eligible_code
          and "GATED_KEYWORDS" not in eligible_code and "GATED_ADAPTERS" not in eligible_code, eligible_code)


# --------------------------------------------------------------------- #
# 10. RESTART_REENTRY_PRESERVES_APPROVAL_AUTHORITY
# --------------------------------------------------------------------- #
def test_restart_reentry_preserves_approval_authority() -> None:
    """exact_proposal_decision() is purely file-based (no in-process cache),
    so a fresh call after a simulated 'restart' (a brand-new function call,
    same durable ESCALATIONS_DIR) reads back the identical decision."""
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        _approve(p.proposal_id, tag)
        first = founder_request.exact_proposal_decision(p.proposal_id)
        second = founder_request.exact_proposal_decision(p.proposal_id)  # simulates a fresh process re-reading
        check("the decision is identical across repeated/re-entrant reads", first == second == "approved")
        ok1, _ = advance._eligible(proposal_mod.load(p.proposal_id))
        ok2, _ = advance._eligible(proposal_mod.load(p.proposal_id))
        check("eligibility is identical across repeated/re-entrant reads (deterministic, no drift)",
              ok1 is True and ok2 is True)
    finally:
        _cleanup(p.proposal_id, "synthetic reentry test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 11. NO_DUPLICATE_IMPLEMENTATION_JOB_ON_REENTRY (the approval-specific
# slice: repeated approval-recording/eligibility calls create no new
# escalation or proposal records themselves -- job-level dedup for an
# eligible proposal is pre-existing, untouched machinery, already covered
# by tests/test_evolution_ledger_integration.py's lineage/dedup tests).
# --------------------------------------------------------------------- #
def test_no_duplicate_records_on_repeated_eligibility_checks() -> None:
    tag = uuid.uuid4().hex[:12]
    p = _make_founder_gated_proposal(tag)
    try:
        esc_id = _approve(p.proposal_id, tag)
        before_escalation_count = len(list(founder_request.ESCALATIONS_DIR.glob("*.json")))
        for _ in range(5):
            advance._eligible(proposal_mod.load(p.proposal_id))
            founder_request.exact_proposal_decision(p.proposal_id)
        after_escalation_count = len(list(founder_request.ESCALATIONS_DIR.glob("*.json")))
        check("repeated eligibility/decision checks create zero new escalation records",
              after_escalation_count == before_escalation_count,
              f"{before_escalation_count} -> {after_escalation_count}")
        check("the same escalation_id still holds the resolved decision (not duplicated under a new id)",
              (founder_request.ESCALATIONS_DIR / f"{esc_id}.json").exists())
    finally:
        _cleanup(p.proposal_id, "synthetic no-duplicate-records test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 12. Live proof against the REAL approved proposal from this session
# --------------------------------------------------------------------- #
def test_live_937a60b8_now_eligible_risk_score_unchanged() -> None:
    p = proposal_mod.load("937a60b8-43d7-489c-b20a-645bc9879f10")
    check("the real proposal's risk_score is still literally 'founder_gated'",
          p.risk_score == "founder_gated", p.risk_score)
    check("the real proposal's status is still 'observed' (untouched)",
          p.status == "observed", p.status)
    decision = founder_request.exact_proposal_decision(p.proposal_id)
    check("the real recorded Founder decision reads back as 'approved'", decision == "approved", decision)
    ok, reason = advance._eligible(p)
    check("the real, previously-blocked proposal is now eligible for governed implementation advancement",
          ok is True, reason)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"raised {e!r}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nALL {len(tests)} TESTS PASSED")
