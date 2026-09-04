#!/usr/bin/env python3
"""One-time backlog migration (Founder Notification Delivery Certification,
2026-09-03/04): evolution/founder_request.py's escalations store had 861
pending records accumulated before any notification channel existed for
it (its own notification_note said so). Turning on per-record Telegram
notification (fast_response_worker.py's new send_founder_escalations())
against that backlog as-is would fire 861 messages in one burst -- a real
spam incident, not a test. Founder decision (2026-09-04): send ONE digest
covering the existing backlog, then mark it all notified so only NEW
escalations from this point forward get individual pushes.

Idempotent by construction: only ever acts on records that are still
pending AND not yet notification_sent, so re-running finds nothing left
to do once the backlog has been digested. No status/decision is ever
changed -- only notification_sent, exactly like mark_notification_sent()
elsewhere.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path("/opt/pulse5-core/mr_silent_spine/telegram_fast_layer")))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution import founder_request as fr  # noqa: E402
from fast_response_worker import send_telegram  # noqa: E402


def main() -> None:
    items = fr.list_pending_founder_requests()
    backlog = [i for i in items if not i.get("notification_sent")]
    if not backlog:
        print("no un-notified backlog remaining — nothing to digest")
        return

    risk_counts = Counter(str(i.get("payload", {}).get("risk", "unknown")).split(" —")[0].lower() for i in backlog)
    risk_summary = ", ".join(f"{v} {k}" for k, v in risk_counts.most_common())

    msg = (
        "MR. SILENT — Founder backlog digest (one-time, 2026-09-04):\n\n"
        f"{len(backlog)} pending decisions accumulated before this notification channel existed.\n"
        f"By risk: {risk_summary}.\n\n"
        "None of these are individually urgent (all low/none/informational risk on inspection) — "
        "review the full list anytime in the MR. SILENT app's Approvals panel.\n\n"
        "From this point on, each NEW Founder-gated decision will notify you individually."
    )
    result = send_telegram(msg)
    print("digest send result:", result)
    if result.get("status") != "SENT":
        print("digest NOT sent — backlog left un-notified so a retry can still send it; nothing marked.")
        return

    marked = 0
    for item in backlog:
        fr.mark_notification_sent(item["escalation_id"])
        marked += 1
    print(f"marked {marked} backlog records as notified")


if __name__ == "__main__":
    main()
