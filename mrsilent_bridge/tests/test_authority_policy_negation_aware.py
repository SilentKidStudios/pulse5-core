#!/usr/bin/env python3
"""
Tests for the NEGATION-AWARE GATED_KEYWORDS repair in authority_policy.py.

WHAT THIS CLOSES: a real false-positive incident (2026-09-03) -- proposal
9266a13c's own safety disclaimer "no credential access" tripped the
\\bcredential keyword pattern, FOUNDER_GATING and blocking a task whose
actual engineering content never touched anything protected. classify()'s
keyword scan had no concept of negation: any occurrence of a gated word,
regardless of surrounding "no"/"do not"/"...untouched" phrasing, escalated
the whole request.

WHAT THIS DELIBERATELY DOES NOT DO: it never removes, reorders, or weakens
any GATED_KEYWORDS/GATED_TOOLS/GATED_PATH_MARKERS/GATED_ADAPTERS entry, and
it is NOT a proposal-9266a13c-specific exception -- the fix is a general,
clause-scoped negation check applied to every GATED_KEYWORDS pattern for
every caller. A genuinely positive-intent (or ambiguous) mention of a
protected concept still escalates exactly as before -- proven directly
below, using the SAME real gated phrases this project's own existing tests
(test_evolution_advance.py) already rely on.

Run: python3 tests/test_authority_policy_negation_aware.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import authority_policy  # noqa: E402
from evolution import advance as advance_mod  # noqa: E402
from evolution import proposal as proposal_mod  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


SANDBOX = Path("/opt/pulse5-core/mrsilent_bridge/jobs/_test_authority_policy_sandbox/workdir")


def _classify(text: str):
    return authority_policy.classify(text, set(), SANDBOX)


# --------------------------------------------------------------------- #
# Required semantics, verbatim from the mission spec
# --------------------------------------------------------------------- #
def test_negated_credential_access_not_gated() -> None:
    d = _classify("no credential access")
    check("'no credential access' does NOT classify as requesting credential authority",
          d.risk_class == authority_policy.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_negated_credential_change_not_gated() -> None:
    d = _classify("do not change credentials")
    check("'do not change credentials' does NOT classify as requesting credential authority",
          d.risk_class == authority_policy.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_credentials_must_remain_untouched_not_gated() -> None:
    d = _classify("credentials must remain untouched")
    check("'credentials must remain untouched' (trailing negation cue) does NOT gate",
          d.risk_class == authority_policy.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_real_credential_access_still_gated() -> None:
    d = _classify("access credentials")
    check("'access credentials' STILL classifies as credential/protected",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_real_credential_change_still_gated() -> None:
    d = _classify("change credentials")
    check("'change credentials' STILL classifies as credential/protected",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_real_secrets_and_api_keys_still_gated() -> None:
    d = _classify("read secrets / API keys")
    check("'read secrets / API keys' STILL classifies as protected",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_negated_paid_resource_not_gated() -> None:
    d = _classify("no payment required for this step")
    check("'no payment required' does NOT classify as requesting paid-resource authority",
          d.risk_class == authority_policy.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_real_paid_resource_still_gated() -> None:
    d = _classify("this requires payment before proceeding")
    check("'requires payment before proceeding' STILL classifies as paid-resource/protected",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# Conservative-default preservation: ambiguous / no-cue mentions still escalate
# --------------------------------------------------------------------- #
def test_ambiguous_credential_mention_still_escalates() -> None:
    d = _classify("credential rotation policy needs review")
    check("an ambiguous credential mention with no negation cue still escalates (conservative default)",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_negation_is_clause_scoped_not_document_wide() -> None:
    """A negation in one clause must never suppress a real positive-intent
    match in a DIFFERENT clause of the same task description."""
    d = _classify("no credential access needed here. separately, change credentials for the deploy user.")
    check("a real positive-intent clause still escalates even when an earlier, unrelated clause is negated",
          d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# Existing, pre-existing real gated phrases (from test_evolution_advance.py's
# own live tests) must remain unaffected -- these have no negation cues.
# --------------------------------------------------------------------- #
def test_preexisting_gated_phrases_unaffected() -> None:
    for text, label in (
        ("delete the stored credentials from the config directory", "credential deletion (test_founder_gated_task_cannot_be_smuggled_through_omni_engineer)"),
        ("modify the scorpio corner voice pipeline settings", "scorpio (test_scorpio_keyword_in_task_text_is_rejected_at_every_engine)"),
    ):
        d = _classify(text)
        check(f"pre-existing gated phrase still gates: {label}",
              d.risk_class == authority_policy.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# Live reproduction: the real incident, using the real proposal's real text
# --------------------------------------------------------------------- #
def test_live_9266a13c_false_positive_resolved() -> None:
    p = proposal_mod.load("9266a13c-ae90-4885-8dc8-4ca18a390eda")
    task_text = advance_mod._build_task_text(p)
    d = authority_policy.classify(task_text, set(), SANDBOX)
    check("the real proposal's real task text no longer false-positives on 'credential'",
          d.risk_class == authority_policy.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("no gated-keyword reason is present at all",
          not any("gated keyword pattern" in r for r in d.reasons), d.reasons)


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
