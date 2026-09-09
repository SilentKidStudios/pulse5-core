#!/usr/bin/env python3
"""Tests for QUOTED_EXAMPLE_META_REFERENCE_FILTER in authority_policy.py.

WHAT THIS CLOSES: Rank 7 job 508048fc's task text asked the agent to write
a TEST proving a module blocks auto-completion of any item "whose
id/note/action_type matches one of walkaway_advance.PROTECTED_GATE_KEYWORDS
(e.g. contains \"production_promotion\" or \"credential\")" -- \bcredential
FOUNDER_GATED the whole job even though "credential" there is a quoted
EXAMPLE gate-name string inside a spec describing a protection mechanism,
never an instruction to touch a real credential. The existing negation-
aware filter (2026-09-03) does not apply here -- there is no negation cue
at all, only a different, previously-unhandled signal: a directly-quoted
literal named as an example/keyword-reference.

WHAT THIS DELIBERATELY DOES NOT DO: it never removes or reorders any
GATED_KEYWORDS/GATED_TOOLS/GATED_PATH_MARKERS/GATED_ADAPTERS entry, never
touches the sandbox path-jail check, never weakens the existing negation-
aware filter, and requires BOTH direct quoting AND an explicit meta-
reference cue nearby -- a bare quoted mention with no such framing, or an
unquoted real instruction, still escalates exactly as before.

Run: python3 tests/test_authority_policy_quoted_meta_reference.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

import authority_policy as ap  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


SANDBOX = Path("/opt/pulse5-core/mrsilent_bridge/jobs/_test_authority_policy_sandbox/workdir")


def _classify(text: str):
    return ap.classify(text, set(), SANDBOX)


# --------------------------------------------------------------------- #
# 1. The exact Rank 7 quoted "credential" example no longer gates
# --------------------------------------------------------------------- #
RANK7_LEDGER_PATH = Path("/opt/pulse5-core/mrsilent_bridge/jobs/508048fc-70d1-4c12-a323-77ef0dcdd626/ledger.json")


def test_real_rank7_task_text_no_longer_gates() -> None:
    task = json.loads(RANK7_LEDGER_PATH.read_text())["task"]
    d = _classify(task)
    check("the real Rank 7 job 508048fc task text is no longer founder_gated",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("no gated-keyword reason is present at all", not any("gated keyword pattern" in r for r in d.reasons), d.reasons)


def test_rank7_exact_fragment_in_isolation() -> None:
    d = _classify(
        "an item whose id/note/action_type matches one of "
        "walkaway_advance.PROTECTED_GATE_KEYWORDS (e.g. contains "
        "\"production_promotion\" or \"credential\") -> outcome=\"BLOCKED_PROTECTED_GATE\""
    )
    check("the Rank 7 fragment, standalone, does not founder-gate",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# 2. Quoted protected-keyword examples in explicit meta/test context are
#    ignored -- general-purpose, not credential-specific
# --------------------------------------------------------------------- #
def test_quoted_scorpio_keyword_example_in_test_context_ignored() -> None:
    d = _classify('write a test asserting the gate keyword list contains the example entry "scorpio" for matching')
    check("a quoted 'scorpio' named as a test/keyword-list example does not gate",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_quoted_secret_keyword_spec_reference_ignored() -> None:
    d = _classify('the spec says PROTECTED_GATE_KEYWORDS contains "secret" as a reference keyword')
    check("a quoted 'secret' framed as a spec/keyword reference does not gate",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_meta_cue_after_the_quote_also_counts() -> None:
    d = _classify('the identifier "credential" is just an example gate name used for matching in this test case')
    check("a meta-reference cue appearing AFTER the quoted word also exempts it",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# 3. Genuine credential actions still gate (required real-instruction list)
# --------------------------------------------------------------------- #
def test_real_credential_actions_still_gate() -> None:
    for phrase in (
        "access credentials",
        "read credentials",
        "change credentials",
        "use credentials",
        "retrieve credentials",
    ):
        d = _classify(phrase)
        check(f"'{phrase}' STILL founder-gates (real, unquoted instruction)",
              d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# 4. Quoted credential wording WITHOUT clear meta/example framing still
#    gates -- quoting alone is never sufficient
# --------------------------------------------------------------------- #
def test_quoted_credential_without_meta_framing_still_gates() -> None:
    d = _classify('go read the "credential" file and print its contents')
    check("a quoted 'credential' with no example/keyword/spec framing nearby still founder-gates",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_quoted_scorpio_without_meta_framing_still_gates() -> None:
    d = _classify('please modify the "scorpio" voice pipeline configuration now')
    check("a quoted 'scorpio' with no example/keyword/spec framing nearby still founder-gates",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_distant_meta_cue_does_not_launder_a_nearby_real_instruction() -> None:
    """A meta-reference cue far outside the bounded window must never
    retroactively exempt an unrelated, nearby real instruction."""
    filler = "x" * 400
    d = _classify(f'for example this is unrelated. {filler} now actually access "credential" data directly')
    check("a distant 'for example' cue (well outside the bounded window) does not exempt a real nearby instruction",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# 5. No Scorpio protections regress
# --------------------------------------------------------------------- #
def test_scorpio_path_marker_completely_unaffected() -> None:
    """This filter only touches the task_description keyword scan --
    GATED_PATH_MARKERS (source_paths) must remain completely untouched."""
    d = ap.classify(
        "an ordinary task", set(), SANDBOX,
        source_paths=[Path("/opt/pulse5-core/scorpios-corner-voice/config.json")],
    )
    check("a source path touching the Scorpio marker still founder-gates, unaffected by this filter",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_preexisting_scorpio_keyword_phrase_still_gates() -> None:
    """Byte-for-byte the same real gated phrase the negation-aware test
    suite already relies on -- must remain unaffected by this addition."""
    d = _classify("modify the scorpio corner voice pipeline settings")
    check("the pre-existing, unquoted 'scorpio' gated phrase still founder-gates",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


# --------------------------------------------------------------------- #
# Negation-aware filter (2026-09-03) unaffected by this addition
# --------------------------------------------------------------------- #
def test_existing_negation_aware_behavior_unaffected() -> None:
    d = _classify("no credential access")
    check("the pre-existing negation-aware exemption ('no credential access') still works",
          d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    d2 = _classify("access credentials")
    check("the pre-existing real-instruction escalation ('access credentials') still works",
          d2.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d2.risk_class} {d2.reasons}")


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
