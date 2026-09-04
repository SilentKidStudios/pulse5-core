"""Pulse5-core-01 <-> Render-forge-01 federation bridge (App/Command
Orchestration milestone, Phase 5, Founder-authorized 2026-08-20).

MR_SILENT_LOGICAL_ENTITY_COUNT=1: this module does NOT create a second
approval database. It reads Pulse's real, existing, authoritative unified
approval inbox (mr_silent_spine/master_approval_unification_v1's
unified_approval_router.py and the arc209/arc210 renderer already used by
the real Telegram bot's /pending, /approve, /deny commands) and routes
decisions back to that SAME real mechanism. Render's own
evolution/founder_request.py escalations remain a completely separate,
equally real source -- this module only normalizes both into one
Founder-facing view; founder_conversation.py decides which owning
authority to call for a given decision.

Transport note (Phase 5 scope, per Founder direction): SSH over the
already-verified Tailscale link to pulse5-core-01 is used here as a
bounded bridge, not as the intended permanent cross-node architecture.
PHASE5_CURRENT_TRANSPORT=SSH_OVER_VERIFIED_TAILSCALE
PHASE5_TARGET_TRANSPORT=GOVERNED_AUTHENTICATED_INTERNAL_SERVICE_CONTRACT
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

PULSE_HOST = "root@100.125.251.12"
PULSE_IDENTITY = "/root/.ssh/id_ed25519_pulse5_core_01"
PULSE_RENDERER = "/opt/pulse5-core/_arc209_unified_inbox/arc210_unified_pending_renderer.py"
PULSE_DETAILS_RENDERER = "/opt/pulse5-core/_arc211_unified_details/arc211A_unified_details_renderer.py"
# Bounded tightly for Founder-facing READ calls (list/detail): real
# observed round-trip is ~1-2s; a real regression run found the previous
# 20-25s ceiling could make a synchronous /pending-requests or /studio-
# status call outlast a caller's own reasonable timeout (found live,
# 2026-08-20 -- test_mrsilent_app_api.py's default 15s client timeout hit
# a real socket.TimeoutError against this endpoint). A Founder-facing
# endpoint should fail fast and honestly (PulseUnreachableError) rather
# than let the Founder stare at a spinner for 20+ seconds. Decision-
# routing (approve/deny) is a deliberate, less-frequent action and keeps
# its own longer, separate timeout below.
_SSH_TIMEOUT_S = 8

# Exactly the same real prefix -> adapter dispatch table
# mrsilent_telegram_v2_single_command/bot.py's own approve()/deny() use
# (read directly from its real source, not guessed) -- never a second,
# divergent routing table.
DISPATCH_BY_TAG = {
    "RG": "/opt/pulse5-core/mr_silent_spine/studio_conductor_v1/telegram_approval_bridge/bin/se26d_rg_approval_adapter.py",
    "OS": "/opt/pulse5-core/studio_telegram_layer/bin/se26f_os_approval_adapter.py",
    "FA": "/opt/pulse5-core/_arc216_fa_adapter/arc216B_fa_approval_adapter.py",
}
DEFAULT_DISPATCH = "/opt/pulse5-core/mr_silent_spine/master_approval_unification_v1/bin/unified_approval_router.py"


def dispatch_script_for_tag(tag: str) -> str:
    return DISPATCH_BY_TAG.get(tag.upper(), DEFAULT_DISPATCH)

# V10.5 real-incident fix (ANDROID WALK-AWAY CLOSURE directive,
# Founder-authorized 2026-08-26, section 5/7): the prior pattern required
# group 3 (the item's displayed id/title) to be a single whitespace-free
# token (\S+) -- real audit this pass found the renderer (arc210) actually
# prints each item's real `title` field verbatim, which for several real,
# genuinely-pending items (e.g. a PRIME proposal titled "Creates new
# execution routing authority.") legitimately contains spaces. The old
# pattern silently failed to match those lines at all, meaning
# list_pulse_pending() silently DROPPED those items from the Founder's
# Approvals list entirely -- not shown with a generic fallback, just never
# present. `(.+)` captures the full remainder of the line.
_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+\[(\w+)\]\s+(.+)$")
_RISK_RE = re.compile(r"^\s*Risk:\s*(\S+)\s*$")


class PulseUnreachableError(Exception):
    """Raised when the SSH bridge to pulse5-core-01 fails or times out --
    never silently treated as 'no pending items', so a Founder query never
    confuses 'nothing pending' with 'couldn't reach Pulse right now'."""


def _build_remote_command(argv: list[str]) -> str:
    """Real bug found and fixed (Founder Notification Delivery
    Certification, 2026-09-04, reproduced live against escalation
    0d2e162be10f83be's resolution_note): `ssh host a b c` does NOT preserve
    argument boundaries -- ssh joins its trailing argv with spaces and hands
    that single string to the remote shell to re-tokenize, so any argument
    containing a space (e.g. a free-text audit note) was silently
    word-split, and anything containing shell metacharacters would be
    interpreted by the remote shell rather than passed through literally.
    shlex.quote() per argument, joined with spaces, produces ONE already-
    quoted command string that a POSIX remote shell reconstructs back into
    the exact original argv -- this is the same class of remote-shell
    argv-mangling bug this module's own history already describes fixing
    once for cdp_driver.py's webview flag, reproduced fresh here in this
    module's own _ssh() and fixed at its one shared call boundary rather
    than per call site."""
    return " ".join(shlex.quote(a) for a in argv)


def _ssh(argv: list[str], *, timeout_s: int = _SSH_TIMEOUT_S) -> str:
    cmd = ["timeout", str(timeout_s), "ssh", "-i", PULSE_IDENTITY, "-o", "BatchMode=yes", PULSE_HOST, _build_remote_command(argv)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 5)
    except subprocess.TimeoutExpired as e:
        raise PulseUnreachableError(f"SSH to pulse5-core-01 timed out: {e}") from e
    if result.returncode != 0:
        raise PulseUnreachableError(f"SSH to pulse5-core-01 failed (exit {result.returncode}): {result.stderr[:300]}")
    return result.stdout


def _parse_renderer_output(raw: str) -> list[dict[str, Any]]:
    """Pure parser, split out from list_pulse_pending() so it can be tested
    offline against a captured real sample without needing a live SSH
    round-trip for every test run."""
    items: list[dict[str, Any]] = []
    pending_item: dict[str, Any] | None = None
    for line in raw.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            if pending_item is not None:
                items.append(pending_item)
            position, tag, item_id = m.group(1), m.group(2), m.group(3)
            pending_item = {
                "source_authority": "pulse",
                "source_node": "pulse5-core-01",
                "approval_id": f"{tag}-{item_id}",
                "tag": tag,
                "pulse_id": item_id,
                "position": int(position),
                "status": "AWAITING_APPROVAL",
            }
            continue
        rm = _RISK_RE.match(line)
        if rm and pending_item is not None:
            pending_item["risk"] = rm.group(1)
    if pending_item is not None:
        items.append(pending_item)
    return items


# Bulk, single-round-trip enrichment (ANDROID WALK-AWAY CLOSURE directive,
# Founder-authorized 2026-08-26, section 7): a real Founder-device audit
# this pass found every Pulse-sourced Approvals card was rendering as
# nothing but "Pending review — risk Tier2 / Ref: <hash>" -- the exact
# "random-looking identifiers" the Founder flagged -- because
# list_pulse_pending() only ever parsed the compressed renderer TEXT
# (position/tag/id/risk), never each item's own real, already-existing,
# much richer source JSON (a real Founder-submitted Instagram link + note
# for OS items; gap_detected/recommended_system/priority for FA items).
# arc217_enrich_pending_payloads.py (deployed to Pulse, not transmitted
# inline -- see its own docstring for why: the identical real remote-shell
# argv-mangling bug already found and fixed once this pass for
# cdp_driver.py's webview debug flag) rebuilds the same real index the
# renderer itself already rebuilds every call, then reads each item's own
# small source file in the SAME remote process -- one additional SSH
# round-trip total, not one per item.
_ENRICH_SCRIPT = "/opt/pulse5-core/_arc209_unified_inbox/arc217_enrich_pending_payloads.py"


def _payload_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalizes the real, already-existing (never fabricated) per-source
    field shapes this pass found on Pulse into the SAME payload shape the
    JS client's isRender card already knows how to draw (subject/finding/
    recommended_action/risk/created_at) -- one real mapping per source
    shape actually observed this pass, honest fallback for anything else."""
    if raw.get("source") == "omniscraper_telegram":
        return {
            "subject": raw.get("project") or "Founder content signal",
            "finding": raw.get("reason") or raw.get("description") or "",
            "recommended_action": "Review/process this Founder-submitted content",
            "risk": raw.get("priority"),
            "created_at": raw.get("created_at_utc"),
        }
    if raw.get("type") == "auto_expansion":
        return {
            "subject": raw.get("recommended_system") or "System expansion proposal",
            "finding": f"Detected gap: {raw.get('gap_detected', 'unknown')}",
            "recommended_action": f"Build/enable: {raw.get('recommended_system', 'unspecified system')}",
            "risk": raw.get("priority"),
            "created_at": raw.get("created_at"),
        }
    if raw.get("type") in ("prime_founder_approval", "founder_gate_required"):
        return {
            "subject": raw.get("recommended_system") or raw.get("action") or "Founder authority request",
            "finding": raw.get("reason") or raw.get("message") or "",
            "recommended_action": raw.get("recommended_system") or raw.get("action") or "",
            "risk": raw.get("priority") or "high",
            "created_at": raw.get("created_at"),
        }
    if raw.get("title"):
        return {
            "subject": raw.get("title"),
            "finding": raw.get("reason") or "",
            "recommended_action": raw.get("requested_action") or "",
            "risk": raw.get("risk_tier"),
            "created_at": raw.get("time"),
        }
    return {}


def list_pulse_pending() -> list[dict[str, Any]]:
    """Real, live read of Pulse's authoritative unified pending inbox --
    parses the SAME human-readable renderer output the real Telegram bot's
    /pending command already sends, never a second data source. Each item
    keeps its real source tag (OS/FA/RG/...) and Pulse's own id, so a
    decision can be routed back to the exact real owning adapter. Also
    enriches each item with a real `payload` dict (see _payload_from_raw)
    so the Founder-facing card never has to fall back to a bare risk/ref
    line when richer real data already exists -- enrichment is
    best-effort: if it fails for any reason, the base list (still fully
    real and functional for approve/deny) is returned unenriched rather
    than failing the whole call."""
    raw = _ssh(["timeout", "8", "python3", PULSE_RENDERER])
    items = _parse_renderer_output(raw)
    try:
        enrich_raw = _ssh(["timeout", "8", "python3", _ENRICH_SCRIPT], timeout_s=10)
        enrichment = {e["approval_id"]: e for e in json.loads(enrich_raw)}
        for item in items:
            e = enrichment.get(item["approval_id"])
            if not e:
                continue
            payload = _payload_from_raw(e["raw"])
            if payload:
                item["payload"] = payload
            # V10.5 real-incident fix (Founder decision, ANDROID WALK-AWAY
            # CLOSURE directive section 4, 2026-08-27): replace the
            # title-derived approval_id (never actually resolvable by the
            # real dispatch adapters -- see arc217_enrich_pending_
            # payloads.py's own comment) with the real, stable, position-
            # based machine_id the adapters actually require. The
            # original value is kept as legacy_approval_id -- never
            # silently discarded -- so any historical reference to it
            # (audit trail, an old cached card) remains inspectable, even
            # though it was never a working dispatch target itself.
            if e.get("machine_id"):
                item["legacy_approval_id"] = item["approval_id"]
                item["approval_id"] = e["machine_id"]
    except Exception:  # noqa: BLE001 -- enrichment is best-effort, never blocks the base real list
        pass
    return items


def get_pulse_health() -> dict[str, Any]:
    """Omni/God Mode Consolidation, P3 (Founder-authorized 2026-08-21):
    bounded, read-only resource/health snapshot of pulse5-core-01 for the
    Studio Intelligence panel — same honest-failure posture as
    list_pulse_pending(): unreachable raises PulseUnreachableError (via
    _ssh()), never silently reported as healthy. One remote command,
    same _SSH_TIMEOUT_S bound as every other call in this module."""
    raw = _ssh(["bash", "-c",
                "echo FAILED_UNITS=$(systemctl --failed --no-legend | wc -l); "
                "echo LOADAVG=$(cut -d' ' -f1-3 /proc/loadavg); "
                "echo MEM=$(free -m | awk '/^Mem:/{print $2\":\"$3\":\"$7}'); "
                "echo DISK=$(df -h / | awk 'NR==2{print $2\":\"$3\":\"$5}')"])
    fields: dict[str, str] = {}
    for line in raw.strip().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    try:
        failed_units = int(fields.get("FAILED_UNITS", "0") or 0)
    except ValueError:
        failed_units = None
    return {
        "reachable": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "failed_units": failed_units,
        "load_avg_1_5_15": fields.get("LOADAVG"),
        "mem_total_used_avail_mb": fields.get("MEM"),
        "disk_size_used_pct": fields.get("DISK"),
    }


def get_pulse_detail(position: int) -> str:
    """Real, read-only detail lookup (App/Command Orchestration Phase 2,
    2026-08-20) -- the same real arc211 renderer bot.py's own /details
    command uses. `position` is the 1-indexed slot from the most recent
    list_pulse_pending() call (Pulse's own real /approve N convention);
    since the underlying list can shift between calls, callers should
    re-list immediately before resolving a position a Founder just named."""
    return _ssh(["timeout", "15", "python3", PULSE_DETAILS_RENDERER, str(position)], timeout_s=20).strip()


def get_pulse_autonomy_status(*, history: bool = False) -> dict[str, Any]:
    """App Convergence milestone (Founder-authorized 2026-08-24): real,
    live MISSION/autonomous-work-visibility/Projects data for the
    canonical app -- same honest-failure posture as every other call in
    this module (PulseUnreachableError on unreachable, never silently
    blank). Runs Pulse's own autonomy_status_snapshot.py (read-only,
    computed fresh from the real cycles/campaigns/job_ledger each call,
    same sources /studio-status already trusts) and returns its JSON
    verbatim. This is also the fix for /projects previously reading
    Render's own LOCAL, disconnected, stale campaigns/ dir instead of
    Pulse's real, live one -- found during this milestone's audit
    (Render's local copy was missing every campaign created since
    Permanent Studio Stewardship began)."""
    cmd = ["python3", "/opt/pulse5-core/mrsilent_bridge/autonomy_status_snapshot.py"]
    if history:
        cmd.append("--history")
    raw = _ssh(cmd, timeout_s=10)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise PulseUnreachableError(f"autonomy status snapshot returned invalid JSON: {e}") from e


PULSE_FOUNDER_REQUEST_CLI = "/opt/pulse5-core/mrsilent_bridge/evolution/founder_request.py"
# Disambiguates Pulse's evolution/founder_request.py escalation ids (also
# 16 lowercase hex, same fingerprint scheme Render's OWN local
# founder_request.py uses -- see that module's _fingerprint()) from
# Render's own local escalation ids, which resolve_any_decision() below
# already routes by is_render_id = fullmatch(16 hex). Must stay inside
# live_sensor_api.py's _DECISION_PATH_RE character class ([0-9a-fA-F_-])
# so a tap-through POST /approve/<id> on this exact id still routes here
# at all -- "ea_" (e, a are valid hex chars) satisfies that, and the
# combined length (3 + 16 = 19) never collides with Render's own bare
# 16-char ids.
PULSE_ESCALATION_ID_PREFIX = "ea_"


def list_pulse_escalations() -> list[dict[str, Any]]:
    """Real, live read of Pulse's OWN durable Founder decision store
    (evolution/founder_request.py's ESCALATIONS_DIR on pulse5-core-01) --
    the exact same store evolution/advance.py's proposal-eligibility gate
    reads locally on Pulse. Not the same thing as list_pulse_pending()
    above (Pulse's separate unified_approval_router inbox) -- kept as a
    distinct, clearly-tagged third source in combined_pending() rather
    than merged/renamed into either existing one, since neither of them
    is this store's real owner. Same honest-failure posture as every
    other call in this module."""
    raw = _ssh(["python3", PULSE_FOUNDER_REQUEST_CLI, "list"])
    records = json.loads(raw)
    items = []
    for r in records:
        item = dict(r)
        item["source_authority"] = "pulse_escalation"
        item["approval_id"] = f"{PULSE_ESCALATION_ID_PREFIX}{r['escalation_id']}"
        items.append(item)
    return items


def resolve_pulse_escalation(approval_id: str, decision_word: str, *, note: str = "") -> dict[str, Any]:
    """Routes an approve/deny for a list_pulse_escalations() item back to
    the SAME real founder_request.resolve_founder_decision() on Pulse that
    already owns this record -- never a second/local resolution, never
    written to Render's own escalations directory. `approval_id` is the
    prefixed id list_pulse_escalations() produced (PULSE_ESCALATION_ID_
    PREFIX + escalation_id)."""
    if decision_word not in ("approve", "deny"):
        raise ValueError(f"decision_word must be 'approve' or 'deny', got {decision_word!r}")
    if not approval_id.startswith(PULSE_ESCALATION_ID_PREFIX):
        raise ValueError(f"not a Pulse escalation id: {approval_id!r}")
    escalation_id = approval_id[len(PULSE_ESCALATION_ID_PREFIX):]
    decision = "approved" if decision_word == "approve" else "denied"
    raw = _ssh(["python3", PULSE_FOUNDER_REQUEST_CLI, "resolve", escalation_id, decision, note], timeout_s=15)
    record = json.loads(raw)
    return {"approval_id": approval_id, "decision": decision_word, "source_authority": "pulse_escalation",
            "escalation_id": escalation_id, "record": record}


def resolve_pulse_decision(approval_id: str, decision: str) -> dict[str, Any]:
    """Routes a decision to the REAL Pulse approval mechanism via the exact
    same dispatch a human Founder using Telegram would trigger -- resolves
    the real adapter script from approval_id's own tag prefix (the same
    lookup bot.py's approve()/deny() do), never a Render-side guess.
    decision: 'approve' | 'deny'."""
    if decision not in ("approve", "deny"):
        raise ValueError(f"decision must be 'approve' or 'deny', got {decision!r}")
    tag = approval_id.split("-", 1)[0] if "-" in approval_id else ""
    dispatch_script = dispatch_script_for_tag(tag)
    out = _ssh(["timeout", "25", "python3", dispatch_script, decision, approval_id], timeout_s=30)
    return {"approval_id": approval_id, "decision": decision, "source_authority": "pulse",
            "dispatch_script": dispatch_script, "response": out.strip()}
