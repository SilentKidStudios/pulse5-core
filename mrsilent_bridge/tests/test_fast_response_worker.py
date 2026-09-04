#!/usr/bin/env python3
"""Regression test for fast_response_worker.py's paid-resource notification
heuristic (Founder Notification Delivery Certification, 2026-09-04).

Real incident: escalation 7cfd77f2b014e5d3 (the real production_promotion
gate for proposal 73e3812b-a27f-40c5-b18a-4586c0e08902) was flagged "Paid
resources requested: yes" by a plain substring check, even though its own
finding text explicitly says "...do not access credentials, do not use paid
resources...". Same negation-blind bug class authority_policy.py's
GATED_KEYWORDS scan already hit and fixed once (commit a4bae71); this test
proves the notifier's heuristic, now reusing that same
_keyword_pattern_escalates() helper, no longer repeats it.

Run: python3 tests/test_fast_response_worker.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mr_silent_spine" / "telegram_fast_layer"))

import fast_response_worker as frw  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def test_negated_mentions_do_not_false_positive() -> None:
    payload = {
        "capability_needed": "none",
        "recommended_action": "n/a",
        "finding": "This is certification-only. Do not modify production, do not promote anything, "
                   "do not access credentials, do not use paid resources, and do not create external side effects.",
    }
    check("negated 'paid resources' text does not flag as a paid-resource request",
          frw._looks_like_paid_resource_request(payload) is False)


def test_real_reproduced_incident_payload_no_longer_false_positives() -> None:
    """The exact real payload from escalation 7cfd77f2b014e5d3."""
    payload = {
        "capability_needed": "production_promotion",
        "recommended_action": "promote job c6139c97-3dc7-41e3-b145-2c0f85f91867 (implemented via omni_engineer) to a real path",
        "finding": ("[CT/ChatGPT submit_work] Perform a tiny sandbox-only engineering diagnostic. Produce a simple "
                    "diagnostic artifact containing the statement: CT_TO_MR_SILENT_GOVERNED_EXECUTION_CANARY=PASS. "
                    "This is certification-only. Do not modify production, do not promote anything, do not access "
                    "credentials, do not use paid resources, and do not create external side effects."),
    }
    check("the exact real escalation 7cfd77f2b014e5d3 payload no longer false-positives",
          frw._looks_like_paid_resource_request(payload) is False)


def test_genuine_paid_resource_request_still_flags() -> None:
    """Negation-awareness must never suppress a real, non-negated mention —
    conservative default preserved, matching authority_policy.py's own
    design intent."""
    payload = {
        "capability_needed": "provider_b GPU rental for rendering",
        "recommended_action": "spin up a paid GPU instance",
        "finding": "this task needs a paid compute resource to proceed",
    }
    check("a genuine, non-negated paid-resource mention still flags",
          frw._looks_like_paid_resource_request(payload) is True)


def test_credential_marker_also_negation_aware() -> None:
    payload = {
        "capability_needed": "none", "recommended_action": "n/a",
        "finding": "this proposal will never access credentials or secrets of any kind",
    }
    check("negated 'credentials' mention does not flag",
          frw._looks_like_paid_resource_request(payload) is False)

    payload_real = {
        "capability_needed": "credential rotation", "recommended_action": "rotate the API credential",
        "finding": "requires updating a stored credential",
    }
    check("a genuine, non-negated credential mention still flags",
          frw._looks_like_paid_resource_request(payload_real) is True)


if __name__ == "__main__":
    test_negated_mentions_do_not_false_positive()
    test_real_reproduced_incident_payload_no_longer_false_positives()
    test_genuine_paid_resource_request_still_flags()
    test_credential_marker_also_negation_aware()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
