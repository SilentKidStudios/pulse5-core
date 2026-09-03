"""
Founder Request Formatting — human-readable framing over the EXISTING
durable escalation record (adapters/human_escalation_adapter.py's
ESCALATIONS_DIR), NOT a new notification channel (Founder-authorized
2026-08-18).

Phase 0 of this project already verified no Telegram bot or other
notification channel exists on this machine, and human_escalation_adapter.py
already correctly treats building one as its own founder-gated decision
(a new service, possibly paid) — out of scope here, and this module does
not attempt one. What it adds instead is purely additive: a concise,
human-readable framing of a Founder-required decision, with the full
machine-readable payload preserved underneath, written to the SAME durable
storage that already exists.

    "Founder, I found X. I can perform Y, but Z crosses your
    production/deletion/credential/system authority gate. I recommend A.
    Approve / deny / ask for details."

Dedup + update-in-place: the escalation_id is DETERMINISTIC (a fingerprint
of subject+capability_needed, not a random uuid) — a repeated request for
the SAME underlying decision updates the SAME record rather than spamming a
new one, and `update_count` tracks how many times MR. SILENT re-raised it
(useful Founder-side signal: "this keeps coming up").

This module NEVER performs the gated action itself and NEVER weakens any
existing gate — resolve_founder_decision() only records what the Founder
decided; the actual authority-gated call (e.g.
promotion.promote(..., founder_approved=True)) remains a separate,
explicit, already-existing call a human/operator makes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ESCALATIONS_DIR = Path(__file__).resolve().parent.parent / "evolution" / "escalations"


def _fingerprint(subject: str, capability_needed: str) -> str:
    return hashlib.sha256(f"{subject}|{capability_needed}".encode()).hexdigest()[:16]


def request_founder_decision(
    *, subject: str, finding: str, capability_needed: str, reason_required: str,
    recommended_action: str, risk: str, affected: dict[str, Any],
    rollback_recovery: str | None = None, requested_by: str = "mrsilent",
) -> dict[str, Any]:
    """Writes (or updates, if the same underlying decision is still
    pending) a durable escalation record with both a concise human-
    readable summary and the full structured payload. No urgency framing,
    no spam — a repeated call for the same fingerprint updates the
    existing record in place."""
    fingerprint = _fingerprint(subject, capability_needed)
    ESCALATIONS_DIR.mkdir(parents=True, exist_ok=True)

    human_readable = (
        f"Founder, I found {finding}. I can perform {recommended_action}, but that requires "
        f"{capability_needed}, which crosses your authority gate ({reason_required}). "
        f"Risk: {risk}. Affected: {affected}."
        + (f" Rollback/recovery: {rollback_recovery}." if rollback_recovery else "")
        + " Approve / deny / ask for details."
    )

    payload = {
        "fingerprint": fingerprint, "subject": subject, "finding": finding,
        "capability_needed": capability_needed, "reason_required": reason_required,
        "recommended_action": recommended_action, "risk": risk, "affected": affected,
        "rollback_recovery": rollback_recovery, "human_readable": human_readable,
    }

    existing_path = None
    if ESCALATIONS_DIR.exists():
        for p in ESCALATIONS_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if d.get("payload", {}).get("fingerprint") == fingerprint and d.get("status") == "pending_founder_review":
                existing_path = p
                break

    now = datetime.now(timezone.utc).isoformat()
    if existing_path is not None:
        record = json.loads(existing_path.read_text())
        state_changed = record.get("payload") != payload
        record["payload"] = payload
        record["last_seen_at"] = now
        if state_changed:
            record["updated_at"] = now
            record["update_count"] = record.get("update_count", 0) + 1
        existing_path.write_text(json.dumps(record, indent=2))
        return record

    escalation_id = fingerprint
    record = {
        "escalation_id": escalation_id, "created_at": now, "updated_at": now, "last_seen_at": now,
        "update_count": 0, "requested_by": requested_by, "status": "pending_founder_review",
        "notification_sent": False,
        "notification_note": "no external notification channel exists (verified Phase 0) — durable record only",
        "payload": payload,
    }
    (ESCALATIONS_DIR / f"{escalation_id}.json").write_text(json.dumps(record, indent=2))
    return record


def resolve_founder_decision(escalation_id: str, decision: str, *, note: str = "") -> dict[str, Any]:
    """decision: 'approved' | 'denied'. Records the Founder's decision
    durably. Never performs the gated action itself."""
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")
    path = ESCALATIONS_DIR / f"{escalation_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no escalation record {escalation_id}")
    record = json.loads(path.read_text())
    record["status"] = decision
    record["resolved_at"] = datetime.now(timezone.utc).isoformat()
    record["resolution_note"] = note
    path.write_text(json.dumps(record, indent=2))
    return record


def exact_proposal_decision(proposal_id: str) -> str | None:
    """Returns the most recently RESOLVED canonical Founder decision
    ('approved' | 'denied') for this EXACT proposal_id, or None when no
    resolved decision exists yet (never escalated, or still pending review).

    Approval is proposal-specific by construction: this only ever matches an
    escalation whose payload.affected.proposal_id equals the given
    proposal_id exactly -- an approval recorded for one proposal can never
    be mistaken for approval of a different one, and there is no fuzzy
    subject/finding matching involved. Scans ALL escalation records (not
    just list_pending_founder_requests()'s pending-only view), since a
    RESOLVED decision is exactly what this looks for. Reads the same durable
    ESCALATIONS_DIR JSON files used everywhere else in this module, so the
    decision survives a restart/re-entry deterministically -- there is no
    separate approval store."""
    if not ESCALATIONS_DIR.exists():
        return None
    latest_decision: str | None = None
    latest_at = ""
    for p in ESCALATIONS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("status") not in ("approved", "denied"):
            continue
        if d.get("payload", {}).get("affected", {}).get("proposal_id") != proposal_id:
            continue
        resolved_at = d.get("resolved_at") or ""
        if resolved_at >= latest_at:
            latest_at = resolved_at
            latest_decision = d.get("status")
    return latest_decision


def list_pending_founder_requests() -> list[dict[str, Any]]:
    if not ESCALATIONS_DIR.exists():
        return []
    out = []
    for p in sorted(ESCALATIONS_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("status") == "pending_founder_review" and "payload" in d:
            out.append(d)
    return out
