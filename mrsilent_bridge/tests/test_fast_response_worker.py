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
from evolution import founder_request  # noqa: E402

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


def test_synthetic_escalation_never_pushed_to_telegram() -> None:
    """Real live leak, found and fixed 2026-09-08: campaign 34c0fbe0's own
    objective text says 'synthetic end-to-end test ...', and its two
    promotion_candidate proposals' escalations were being pushed straight
    to the real Founder Telegram channel by send_founder_escalations()
    because it read founder_request.list_pending_founder_requests() (raw,
    unfiltered) without ever consulting founder_view_filter.py (which
    already exists and already correctly classifies this exact case).
    This proves the fix: a synthetic escalation is skipped, sent_telegram
    is never called for it, and it is never marked notification_sent."""
    import tempfile
    import evolution.proposal as proposal_mod

    tmp_dir = Path(tempfile.mkdtemp())
    orig_proposals_dir = proposal_mod.PROPOSALS_DIR
    orig_list = founder_request.list_pending_founder_requests
    orig_send = frw.send_telegram
    orig_mark = founder_request.mark_notification_sent
    sent_calls: list = []
    marked: list = []
    try:
        # Patched for the FULL duration, including send_founder_escalations()
        # itself — founder_view_filter.classify_pending_item() resolves
        # proposal_id via evolution.proposal.load(), which reads
        # PROPOSALS_DIR at call time, not at proposal-creation time.
        proposal_mod.PROPOSALS_DIR = tmp_dir
        real_proposal = proposal_mod.create("real weakness", "real upgrade", "low")
        synthetic_proposal = proposal_mod.create(
            "Campaign 99999999 (synthetic end-to-end test abcdef: create file A)",
            "promote file A", "low",
        )
        real_req = {
            "escalation_id": "real-esc-1", "notification_sent": False,
            "payload": {"subject": f"proposal {real_proposal.proposal_id}",
                        "finding": "a real production_promotion need",
                        "affected": {"proposal_id": real_proposal.proposal_id}},
        }
        synthetic_req = {
            "escalation_id": "synthetic-esc-1", "notification_sent": False,
            "payload": {"subject": f"proposal {synthetic_proposal.proposal_id}",
                        "finding": "a real production_promotion need",
                        "affected": {"proposal_id": synthetic_proposal.proposal_id}},
        }
        founder_request.list_pending_founder_requests = lambda: [real_req, synthetic_req]
        frw.send_telegram = lambda *a, **k: sent_calls.append(a) or {"status": "SENT"}
        founder_request.mark_notification_sent = lambda eid: marked.append(eid)
        frw.send_founder_escalations()
    finally:
        proposal_mod.PROPOSALS_DIR = orig_proposals_dir
        founder_request.list_pending_founder_requests = orig_list
        frw.send_telegram = orig_send
        founder_request.mark_notification_sent = orig_mark

    check("exactly one Telegram send occurred (the real one only)", len(sent_calls) == 1)
    check("the synthetic escalation was never marked notification_sent",
          "synthetic-esc-1" not in marked, detail=str(marked))
    check("the real escalation WAS marked notification_sent",
          "real-esc-1" in marked, detail=str(marked))


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
    test_synthetic_escalation_never_pushed_to_telegram()
    test_credential_marker_also_negation_aware()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
