#!/usr/bin/env python3
"""
Tests for Founder Communication Integration (Founder-authorized
2026-08-18): evolution/founder_request.py — a purely additive,
human-readable framing layer over the EXISTING durable escalation record
(no new notification channel; verified in Phase 0 that none exists on this
machine, and building one is explicitly its own founder-gated decision,
out of scope here).

Run: python3 tests/test_founder_communication.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evolution import founder_request as fr
from _test_isolation import isolated_test_state  # root-cause test-isolation guarantee, see tests/_test_isolation.py

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _cleanup(escalation_id: str) -> None:
    p = fr.ESCALATIONS_DIR / f"{escalation_id}.json"
    if p.exists():
        p.unlink()


def test_request_contains_all_required_elements() -> None:
    subject = f"synthetic subject {uuid.uuid4()}"
    record = fr.request_founder_decision(
        subject=subject, finding="a synthetic risky situation",
        capability_needed="credential_access", reason_required="touches a protected secret",
        recommended_action="read the credential to verify connectivity", risk="medium",
        affected={"files": ["synthetic.env"]}, rollback_recovery="no state changes if denied",
    )
    hr = record["payload"]["human_readable"]
    check("mentions the finding", "a synthetic risky situation" in hr)
    check("mentions the exact capability needed", "credential_access" in hr)
    check("mentions why authority is required", "touches a protected secret" in hr)
    check("mentions the recommended action", "read the credential to verify connectivity" in hr)
    check("mentions risk", "medium" in hr)
    check("offers approve/deny/ask, no fake urgency language", "Approve / deny / ask for details" in hr)
    check("no urgency/alarm words leaked in", not any(w in hr.lower() for w in ("urgent", "immediately", "asap", "critical!!!")))
    check("the machine-readable payload is preserved underneath the human text",
          record["payload"]["capability_needed"] == "credential_access" and record["payload"]["risk"] == "medium")
    check("rollback/recovery implications are included when given", "no state changes if denied" in hr)

    _cleanup(record["escalation_id"])


def test_repeated_request_dedupes_and_updates_in_place() -> None:
    subject = f"synthetic dedup subject {uuid.uuid4()}"
    r1 = fr.request_founder_decision(
        subject=subject, finding="finding v1", capability_needed="system_config_change",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    r2 = fr.request_founder_decision(
        subject=subject, finding="finding v2 (state changed)", capability_needed="system_config_change",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    check("the SAME escalation_id is reused, not a new one (no spam)", r1["escalation_id"] == r2["escalation_id"])
    check("update_count increments when the underlying state genuinely changed", r2["update_count"] == 1, r2["update_count"])
    check("the record now reflects the LATEST finding", r2["payload"]["finding"] == "finding v2 (state changed)")

    r3 = fr.request_founder_decision(
        subject=subject, finding="finding v2 (state changed)", capability_needed="system_config_change",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    check("an IDENTICAL repeat does not bump update_count (not noise, just a fresh 'still relevant' timestamp)",
          r3["update_count"] == 1, r3["update_count"])

    pending = fr.list_pending_founder_requests()
    check("exactly one pending record exists for this subject, not three",
          sum(1 for p in pending if p["payload"]["subject"] == subject) == 1)

    _cleanup(r1["escalation_id"])


def test_resolution_never_performs_the_gated_action_only_records_the_decision() -> None:
    subject = f"synthetic resolution subject {uuid.uuid4()}"
    r1 = fr.request_founder_decision(
        subject=subject, finding="f", capability_needed="production_promotion",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    check("starts pending", r1["status"] == "pending_founder_review", r1["status"])

    resolved = fr.resolve_founder_decision(r1["escalation_id"], "approved", note="synthetic approval")
    check("resolution records the decision", resolved["status"] == "approved", resolved["status"])
    check("resolution never claims an action was performed",
          "action_performed" not in resolved and "promoted" not in resolved)

    pending = fr.list_pending_founder_requests()
    check("a resolved request is no longer pending", not any(p["escalation_id"] == r1["escalation_id"] for p in pending))

    _cleanup(r1["escalation_id"])


def test_invalid_decision_value_is_rejected() -> None:
    subject = f"synthetic invalid decision subject {uuid.uuid4()}"
    r1 = fr.request_founder_decision(
        subject=subject, finding="f", capability_needed="x", reason_required="r",
        recommended_action="a", risk="low", affected={},
    )
    raised = False
    try:
        fr.resolve_founder_decision(r1["escalation_id"], "maybe later")
    except ValueError:
        raised = True
    check("an invalid decision value is rejected, not silently accepted", raised)
    _cleanup(r1["escalation_id"])


if __name__ == "__main__":
    with isolated_test_state():
        test_request_contains_all_required_elements()
        test_repeated_request_dedupes_and_updates_in_place()
        test_resolution_never_performs_the_gated_action_only_records_the_decision()
        test_invalid_decision_value_is_rejected()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
