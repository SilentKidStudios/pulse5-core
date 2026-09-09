#!/usr/bin/env python3
import json, os, re, sys, time, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/opt/pulse5-core")
FAST = ROOT / "mr_silent_spine" / "telegram_fast_layer"
BRIDGE = ROOT / "mr_silent_spine" / "founder_approval_bridge"
STATE = FAST / "state"
LOGS = FAST / "logs"
QUEUE = FAST / "queue"
DONE = FAST / "done"
OUTBOX = BRIDGE / "outbox"
SENT = BRIDGE / "sent"

for p in [STATE, LOGS, QUEUE, DONE, OUTBOX, SENT]:
    p.mkdir(parents=True, exist_ok=True)

# Founder Notification Delivery Certification (2026-09-03): reuses THIS
# already-running, already-credentialed sender for evolution/founder_
# request.py's own escalation store -- the real store evolution/advance.py
# gates proposal eligibility on, which had zero external notification path
# (its own notification_note said so). Deliberately NOT a new sidecar/
# scheduler/Telegram sender -- same send_telegram() below, same 20s cycle()
# loop already polling OUTBOX. "ea_" mirrors the exact prefix render-forge-
# 01's pulse_bridge.py now uses so a tapped deep link and the phone's own
# approvals card list agree on the same id for the same record.
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))
from evolution import founder_request  # noqa: E402
from evolution import founder_view_filter  # noqa: E402
import authority_policy  # noqa: E402
ESCALATION_ID_PREFIX = "ea_"
ESCALATION_DEEPLINK_SCHEME = "mrsilent://approval/"
# Notification tap-through repair (2026-09-04): stateless HTTPS landing
# page on render-forge-01's existing tailscale-serve HTTPS endpoint (same
# transport already used for /app and the APK download route) -- Render
# remains transport-only here, this URL records/resolves nothing, it only
# redirects to ESCALATION_DEEPLINK_SCHEME + <approval_id>.
ESCALATION_HTTPS_OPEN_BASE = "https://render-forge-01.tail7a19b8.ts.net:8443/open/"

def now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def load_env_file(path):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(errors="ignore").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,v=line.split("=",1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data

def discover_env():
    env = {}
    files = [
        ROOT / ".env",
        ROOT / "mr_silent_spine/.env",
        ROOT / "mr_silent_spine/telegram.env",
        ROOT / "telegram.env",
        ROOT / "studio_telegram_layer/.env",
        ROOT / "studio_telegram_layer/telegram.env",
        Path("/etc/pulse5.env"),
        Path("/etc/mr_silent.env"),
        Path("/etc/telegram.env"),
    ]
    for f in files:
        env.update(load_env_file(f))
    return env

ENV = discover_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("MR_SILENT_TELEGRAM_BOT_TOKEN") or ENV.get("TELEGRAM_BOT_TOKEN") or ENV.get("MR_SILENT_TELEGRAM_BOT_TOKEN") or ENV.get("BOT_TOKEN")
CHAT_ID = os.environ.get("FOUNDER_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_FOUNDER_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or ENV.get("FOUNDER_TELEGRAM_CHAT_ID") or ENV.get("TELEGRAM_FOUNDER_CHAT_ID") or ENV.get("TELEGRAM_CHAT_ID") or ENV.get("CHAT_ID")

def send_telegram(text, *, button_text=None, button_url=None):
    if not TOKEN or not CHAT_ID:
        return {"status": "NOT_CONFIGURED", "reason": "Missing bot token or founder chat id"}
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    fields = {"chat_id": CHAT_ID, "text": text}
    if button_text and button_url:
        # Notification tap-through repair (2026-09-04): a real physical S25
        # test found Telegram does not linkify an arbitrary custom URI
        # scheme in plain text (TELEGRAM_TAP_THROUGH=FAIL) -- an inline-
        # keyboard button with a real https:// url IS rendered as a
        # tappable action by Telegram; button_url must be that kind of
        # url (the caller is responsible for it being a real https://
        # landing page, never a raw mrsilent:// string here).
        fields["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": button_text, "url": button_url}]]})
    payload = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(url, data=payload, timeout=15) as r:
        body = r.read().decode(errors="ignore")
    return {"status": "SENT", "telegram_response_preview": body[:300]}

def send_founder_requests():
    results = []
    for req_file in sorted(OUTBOX.glob("FA-*.json")):
        marker = SENT / req_file.name
        if marker.exists():
            continue
        req = json.loads(req_file.read_text())
        msg = (
            "MR. SILENT:\n"
            f"{req.get('message_to_founder','Founder, I need your assistance.')}\n\n"
            f"Approval ID: {req.get('approval_id')}\n"
            f"Risk: {req.get('risk')}\n"
            f"Reason: {req.get('reason')}\n\n"
            f"Requested action: {req.get('requested_action')}"
        )
        result = send_telegram(msg)
        result["approval_file"] = str(req_file)
        result["timestamp_utc"] = now()
        results.append(result)
        if result.get("status") == "SENT":
            marker.write_text(json.dumps(result, indent=2))
    return results

# Same real HIGH-RISK/paid-resource keyword convention the Android app's
# own mrsApprovalIsHighRisk() already uses (golden_founder_os_candidate.html,
# MRS_HIGH_RISK_MARKERS) -- never a second, divergent classification.
#
# Real false positive found and fixed (2026-09-04, real proposal
# 73e3812b-a27f-40c5-b18a-4586c0e08902's own production_promotion
# escalation): a plain substring check flagged "Paid resources requested:
# yes" against text that literally says "...do not access credentials, do
# not use paid resources..." -- the exact same negation-blind bug class
# authority_policy.py's GATED_KEYWORDS scan already hit and fixed once
# (commit a4bae71, 2026-09-03) via a clause-scoped negation check. Reused
# directly here rather than re-implemented, so this notifier and the real
# risk classifier never drift into two divergent definitions of "mentions a
# gated capability" over time.
_PAID_RESOURCE_MARKER_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(m) + r"\b", re.IGNORECASE)
    for m in ("paid", "provider_b", "gpu", "credential")
)


def _looks_like_paid_resource_request(payload: dict) -> bool:
    blob = " ".join(str(payload.get(k, "")) for k in ("capability_needed", "recommended_action", "finding"))
    return any(authority_policy._keyword_pattern_escalates(p, blob) for p in _PAID_RESOURCE_MARKER_PATTERNS)


def send_founder_escalations():
    """Scans evolution/founder_request.py's OWN durable escalation store
    (not the OUTBOX above -- a completely separate, real source; see that
    module's own docstring) for pending, not-yet-notified records and
    pushes each through the SAME send_telegram() this file already uses.
    Dedup is the store's own notification_sent field -- set exactly once,
    via founder_request.mark_notification_sent(), only after a real SENT
    result, so a delivery failure naturally retries next cycle instead of
    being marked done, and a real send is never repeated on later cycles."""
    results = []
    for req in founder_request.list_pending_founder_requests():
        if req.get("notification_sent"):
            continue
        # Real live leak found and fixed 2026-09-08: this loop pushed EVERY
        # pending escalation straight to the real Founder Telegram channel,
        # including explicitly synthetic/test-only ones (e.g. a campaign
        # whose own objective text says "synthetic end-to-end test ...").
        # founder_view_filter.py already implements this exact
        # classification for the App's Approvals/Projects views (built
        # 2026-08-20 after a real device finding of the same problem) but
        # was never consulted here — reused, not reimplemented, so
        # Telegram and the App agree on what counts as real Founder work.
        # Not marked notification_sent: no message was actually sent, and
        # that field's contract (see founder_request.py) is "a real SENT
        # result", so a suppressed test item must not claim one.
        if founder_view_filter.classify_pending_item(req) != founder_view_filter.CATEGORY_REAL_ACTIONABLE:
            continue
        escalation_id = req["escalation_id"]
        payload = req.get("payload", {})
        approval_id = f"{ESCALATION_ID_PREFIX}{escalation_id}"
        # Notification tap-through repair (2026-09-04): real physical S25
        # test found Telegram never renders a tap action for a bare
        # mrsilent://... scheme in message text. HTTPS_OPEN_URL is a
        # stateless landing page on the SAME render-forge-01 transport
        # (live_sensor_api.py's /open/<id> -- records nothing, resolves
        # nothing) that Telegram DOES render as a real tappable inline-
        # keyboard button; that page then navigates to the exact same
        # deep link the app's manifest already handles.
        https_open_url = f"{ESCALATION_HTTPS_OPEN_BASE}{approval_id}"
        msg = (
            "MR. SILENT — Founder decision needed:\n"
            f"{payload.get('human_readable') or payload.get('finding', '')}\n\n"
            f"Subject: {payload.get('subject', '(none)')}\n"
            f"Risk: {payload.get('risk', 'unknown')}\n"
            f"Paid resources requested: {'yes (heuristic — verify in app)' if _looks_like_paid_resource_request(payload) else 'no'}\n"
            f"Escalation ID: {escalation_id}\n\n"
            "Tap the button below to open this exact decision in MR. SILENT."
        )
        result = send_telegram(msg, button_text="OPEN IN MR. SILENT", button_url=https_open_url)
        result["escalation_id"] = escalation_id
        result["timestamp_utc"] = now()
        results.append(result)
        if result.get("status") == "SENT":
            try:
                founder_request.mark_notification_sent(escalation_id)
            except FileNotFoundError:
                pass  # resolved/removed between list and mark — nothing left to flag
    return results


def process_background_jobs():
    results = []
    for job in sorted(QUEUE.glob("*.json"))[:10]:
        data = json.loads(job.read_text())
        result = {
            "job": str(job),
            "status": "ACKNOWLEDGED_BACKGROUND_PROCESSING",
            "message": "Fast layer acknowledged job; deep MR. SILENT execution should attach here.",
            "timestamp_utc": now()
        }
        done = DONE / job.name
        done.write_text(json.dumps(result, indent=2))
        job.unlink(missing_ok=True)
        results.append(result)
    return results

def cycle():
    founder_results = send_founder_requests()
    escalation_results = send_founder_escalations()
    job_results = process_background_jobs()
    state = {
        "phase": "MR_SILENT_TELEGRAM_FAST_LAYER_CYCLE",
        "status": "PASS",
        "timestamp_utc": now(),
        "telegram_configured": bool(TOKEN and CHAT_ID),
        "founder_notifications": founder_results,
        "founder_escalation_notifications": escalation_results,
        "background_jobs_processed": job_results,
        "important_truth": "This sidecar can push founder approvals if token/chat id are configured. Existing Telegram bot handler still needs integration for instant replies to incoming user messages."
    }
    (STATE / "last_cycle.json").write_text(json.dumps(state, indent=2))
    with (LOGS / "fast_layer.log").open("a") as f:
        f.write(json.dumps(state) + "\n")
    return state

def main():
    while True:
        try:
            cycle()
        except Exception as e:
            err = {"status": "ERROR", "error": repr(e), "timestamp_utc": now()}
            (STATE / "last_error.json").write_text(json.dumps(err, indent=2))
        time.sleep(20)

if __name__ == "__main__":
    main()
