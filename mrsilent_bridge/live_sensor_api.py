"""Live Sensor API — MR. SILENT Real Device Sensor Bridge milestone
(Founder-authorized 2026-08-18).

DISCOVERY (before building): this Studio already has a proven, working
pattern for exactly this need — Scorpio's Corner's own app_api binds to
127.0.0.1 (SCORPIO_APP_API_HOST=127.0.0.1, confirmed by reading its
systemd unit) and `tailscale serve` proxies it to a real, tailnet-only
HTTPS endpoint (https://render-forge-01.tail7a19b8.ts.net) with a real,
automatically-issued TLS cert — no public internet exposure, no new
infrastructure, no paid dependency. The Founder's own Android phone
(`amoss-s25-ultra`) is ALREADY enrolled on this same tailnet (confirmed
via `tailscale status`). This module follows the EXACT SAME pattern for
MR. SILENT's own, separate service, on its own port — `tailscale serve`
mappings are additive per-port (confirmed: the existing config already
has two independent ports without clearing each other), so adding one
here never touches Scorpio's Corner's own mapping.

Binds to 127.0.0.1 ONLY — never 0.0.0.0, never directly to the public
internet. Real network-facing exposure is handled entirely by `tailscale
serve` (tailnet devices only), a security layer this module does not
implement itself and must never bypass by binding wider.

Authentication (defense in depth, on top of Tailscale's own device-level
tailnet membership control): a single, long, random bearer token
generated once via secrets.token_urlsafe() and stored in a 0600-permission
file, never logged, never printed except once at generation time for the
Founder to copy. Every session-affecting endpoint requires it; only
/health is open (liveness only, no session data). Possessing a valid
token IS this API's real, only mechanism for founder_approved=True — no
endpoint can set that flag any other way.

Pure stdlib (http.server) — no new pip dependency for a bounded API
surface like this one; consistent with "no unnecessary infrastructure."

MR. SILENT APP — UNIFIED FOUNDER INTERFACE, first vertical slice
(Founder-authorized 2026-08-19): this same service now also serves
mrsilent_app.html at /app and a POST /chat endpoint. Real reality audit
before building (per this milestone's own directive): NO MR. SILENT app/
conversational UI existed anywhere on this machine — the only prior HTML
surface was live_sensor_client.html (sensor-only, no conversation), and
the real conversational intelligence (evolution/founder_conversation.py)
was only ever reachable via cli.py. /chat is a THIN new layer: it does
not add any new intelligence — it calls the exact same founder_conversation.
handle_intent()/format_response() pair cli.py already uses, over the
same bearer-token auth as every other endpoint here. Voice input reuses
the EXISTING audio governance/bridge session mechanism (no second
microphone backend) — the app's own JS starts a real audio-only session,
finalizes it to get a real transcript via the unmodified local_audio_
bridge pipeline, then feeds that transcript into the same /chat endpoint
a typed message would use. live_sensor_client.html and its own tested
paths (mic/camera/switching/revoke) are completely untouched by this
addition.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.error
import urllib.request

import live_sensor_bridge as bridge
import live_sensor_governance as gov
from evolution import founder_conversation
from evolution import founder_request

BRIDGE_ROOT = Path(__file__).resolve().parent
# Real operational gap found and fixed (2026-08-24 App Convergence repair
# slice): a source-code fix to founder_conversation.py sat un-deployed for
# over two hours because this is a long-running process (systemd service)
# and Python does not hot-reload an already-imported module -- editing the
# .py file on disk has zero effect until the process restarts. That silent
# gap cost a full real S25 Ultra retest cycle before being traced. PROCESS_
# STARTED_AT + the drift check in /health below make that class of bug
# detectable in one curl call instead of a multi-round guessing exercise.
PROCESS_STARTED_AT = datetime.now(timezone.utc)
RUNTIME_DIR = BRIDGE_ROOT / "live_sensor_runtime"
TOKEN_FILE = RUNTIME_DIR / "api_token.secret"
STATIC_CLIENT_FILE = BRIDGE_ROOT / "live_sensor_client.html"
STATIC_APP_FILE = BRIDGE_ROOT / "mrsilent_app.html"

# Bounded, temporary last-mile APK delivery (Founder-authorized 2026-08-24,
# APK delivery boundary milestone). Serves EXACTLY one fixed file at one
# fixed path -- no path parameter, no directory listing, no traversal
# surface exists because the route match is a literal string compare, not
# a filesystem lookup driven by request input. Unauthenticated (like /app
# and /health on this same server) because Tailscale's own tailnet
# membership is this server's real access boundary, documented in this
# module's own header -- not a weakening of that model, an application of
# it. Remove APK_DELIVERY_DIR and this route once the Founder confirms
# the real device install; it is not meant to be permanent distribution
# infrastructure.
APK_DELIVERY_DIR = BRIDGE_ROOT / "apk_delivery"
# Real filename clarity fix (Founder-authorized 2026-08-24, v9 native-
# experience repair): the served filename had stayed "v2-reconciled"
# across five real content refreshes (v3/v4/v5/v7/v8), no longer honestly
# describing what the route actually delivers. Renamed to track the real
# current canonical build; the delivery ROUTE mechanism itself (bounded,
# Tailscale-only, one fixed literal path) is unchanged.
APK_DELIVERY_FILE = APK_DELIVERY_DIR / "mrsilent-v10.6-founder-notification-delivery.apk"
APK_DELIVERY_ROUTE = "/download/mrsilent-v10.6-founder-notification-delivery.apk"
# Founder-authorized 2026-08-26 (APK_BROWSER_DOWNLOAD=FAIL, reported
# twice against this exact version-specific route): direct re-testing
# from this host proved the versioned route above returns a clean,
# unauthenticated 200 every time -- ruling out a server-side auth bug.
# The route above is renamed on every real build, so a browser bookmark/
# history entry to any PREVIOUS version's URL now correctly 401s (it
# falls through to the same authenticated-routes gate every other
# endpoint here uses) -- the most plausible real explanation for a
# repeated identical failure report despite server-side testing showing
# no problem. This STABLE alias always serves whichever build
# APK_DELIVERY_FILE currently points at, so a bookmark to it can never
# go stale across a future rename -- same bounded, fixed literal path,
# no filesystem input taken from the request, as the versioned route.
APK_DELIVERY_LATEST_ROUTE = "/download/latest.apk"
# V10.4 delivery-finalization repair (Founder-authorized 2026-08-26):
# real physical S25 evidence showed Chrome receiving 100% of the APK
# bytes (44.32MB/44.32MB) yet never finalizing the download. Root cause
# (see _serve_apk_delivery): this route never advertised Accept-Ranges
# or honored Range requests, and Android Chrome's DownloadManager issues
# Range-based GETs for large files -- a 200-with-full-body reply to a
# Range request breaks its completion bookkeeping even though the byte
# COUNT looks complete. Fixing Range support on the SAME URL fixes the
# real bug without requiring a new URL, but the Founder explicitly asked
# for one fresh cache-busted URL to avoid any client-side history/cache
# state left over from the proven-broken prior attempt. Same bytes,
# same file, no APK rebuild.
APK_DELIVERY_TEST_ROUTE = "/download/s25-v104-final.apk"

# PERSONAL_FOUNDER_APP silent auth (Founder-authorized 2026-08-24).
# Preference #1 from the Founder's own ordering: "existing trusted device/
# Tailscale identity + backend authorization, where sufficient." Verified
# sufficient here by direct, live testing (2026-08-24): `tailscale serve`
# itself -- not this Python process, not anything request-controlled --
# injects Tailscale-User-Login and X-Forwarded-For (the real tailnet
# source IP) into every request it proxies, for a genuinely
# WireGuard-authenticated tailnet peer. A request that reaches this
# process any other way (e.g. a direct connection to 127.0.0.1:8794,
# which is not reachable from outside this host at all -- BIND_HOST above
# is 127.0.0.1, never 0.0.0.0) cannot forge these headers, because they
# are set by tailscaled's own proxy, not read from client-supplied input
# passed straight through. This is an application of the existing trust
# boundary this whole server already documents (tailnet membership IS the
# access control), not a new or weaker one -- and it does NOT replace
# bearer-token auth on any operational endpoint (/chat, /mission,
# /approve, /session, /revoke-all, etc. all still require the real
# token, unchanged). It only replaces the Founder having to manually
# copy/paste that token once per install.
#
# These are identity LABELS (a login name, a device's tailnet IP), not
# secrets -- both are already visible to anyone with `tailscale status`
# access on this tailnet, exactly like a username. Never printed/logged
# alongside the real token; the token itself is read fresh from
# TOKEN_FILE at request time and returned exactly once, over the same
# encrypted tailnet connection every other call on this server already
# relies on -- never written to this source file, an APK asset, a test,
# or a log line anywhere.
FOUNDER_TAILNET_LOGIN = "th3s1l3ntk1d@gmail.com"
FOUNDER_DEVICE_TAILNET_IP = "100.99.152.96"  # amoss-s25-ultra, per `tailscale status`

# Spoken MR. SILENT voice output (Founder-authorized 2026-08-24, App
# Convergence voice-output milestone): reuses Pulse's EXISTING, real,
# already-configured canonical voice (an ElevenLabs clone, voice_id
# mktZnLJ07vrlUk4OUgGn, found live on mrsilent-command-spoken-reply.service
# -- never invented here). A new, additive-only /synthesize-speech-only
# route was added to that SAME service (main.py on Pulse) that reuses its
# exact voice_id/model/voice_settings but skips its legacy /speak-command
# division-routing reasoning entirely -- this module never re-derives its
# own answer to a Founder's question (MR_SILENT_LOGICAL_ENTITY_COUNT=1);
# it only asks Pulse to speak text founder_conversation.py already decided.
# Reachable directly over the tailnet (confirmed live, no SSH needed at
# request time) -- Pulse's own bearer token for that service is fetched
# once into a 0600 runtime secret file, mirroring this module's own
# TOKEN_FILE pattern, never embedded in source.
PULSE_TAILNET_IP = "100.125.251.12"
PULSE_SPOKEN_REPLY_PORT = 9301
PULSE_SPOKEN_REPLY_TOKEN_FILE = RUNTIME_DIR / "pulse_spoken_reply_token.secret"

BIND_HOST = "127.0.0.1"  # never 0.0.0.0 — tailscale serve handles real network exposure
DEFAULT_PORT = 8794  # verified free via `ss -ltnp` before choosing — 8793 is already pulse-api's uvicorn

_SESSION_PATH_RE = re.compile(r"^/session/([0-9a-f-]{36})(/(chunk/(audio|video|image)/(\d+)|finalize|end|revoke|status))?$")
_DECISION_PATH_RE = re.compile(r"^/(approve|deny)/([0-9a-fA-F_\-]{3,80})$")  # Render escalation_id (16 hex) or Pulse's TAG-id approval_id -- shape checked in founder_conversation.resolve_any_decision()
# V10.3R4 (Founder-authorized 2026-08-26): widened from uuid.uuid4().hex
# (32 lowercase hex chars) to also accept a real client-generated id
# (mrsGenerateLocalMessageId(), e.g. "m_l3x9k2_a8f3d1") now that
# founder_conversation.append_thread_message() honors a client-supplied
# id verbatim -- see its own docstring for why. A server-generated
# uuid4().hex is already a subset of this broader, still-bounded charset.
_CONVERSATION_MESSAGE_PATH_RE = re.compile(r"^/conversation/message/([A-Za-z0-9_-]{1,64})$")
# Notification tap-through repair (Founder Notification Delivery
# Certification, 2026-09-04): Telegram does not linkify an arbitrary custom
# URI scheme (mrsilent://...) in plain message text -- confirmed on the real
# physical S25 (TELEGRAM_TAP_THROUGH=FAIL, no tappable action rendered).
# This route is a stateless HTTPS landing page ONLY -- it reads and writes
# no state anywhere, records no decision, and calls no approval/resolution
# code; it exists purely so Telegram has a real https:// URL to render as a
# tappable inline-keyboard button, which then navigates (a real top-level
# browser navigation, not a fetch/XHR, so Android's intent resolution for
# the app's own registered mrsilent:// scheme actually fires) to the exact
# same mrsilent://approval/<id> deep link the app's manifest already
# handles. Same charset as _DECISION_PATH_RE's id group plus uppercase (to
# also cover a future TAG-id shape) -- never a filesystem/lookup key, purely
# echoed into the redirect target after validation.
_OPEN_PATH_RE = re.compile(r"^/open/([A-Za-z0-9_\-]{1,80})$")

# V10.4 delivery-finalization repair (Founder-authorized 2026-08-26): a
# single-range "bytes=start-end" / "bytes=start-" / "bytes=-suffixlen"
# parser, matching the subset of RFC 7233 that a real download manager
# actually sends for a resumable single-file GET.
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_APK_ETAG_CACHE: dict[str, str] = {}


def _apk_etag(path: Path, size: int, mtime_ns: int) -> str:
    """Real sha256-derived ETag, computed once per (path, size, mtime)
    and cached — never a fabricated/placeholder value. Cleared whenever
    the underlying file changes (new build published) so a stale ETag can
    never be served against new bytes."""
    key = f"{path}:{size}:{mtime_ns}"
    cached = _APK_ETAG_CACHE.get(key)
    if cached:
        return cached
    _APK_ETAG_CACHE.clear()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    etag = f'"{h.hexdigest()}"'
    _APK_ETAG_CACHE[key] = etag
    return etag


def get_or_create_token() -> str:
    """Generated once, reused thereafter — never regenerated silently
    out from under an already-configured client."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    # V10.3R3 delivery-surface fix (Founder-authorized 2026-08-26,
    # PHYSICAL_S25_PREVIOUS_DOWNLOAD=FAIL investigation): every server-side
    # test of the download route was already a clean 200 with a matching
    # SHA256, including with a real Android Chrome User-Agent -- the most
    # plausible remaining explanation for a real device seeing a stale
    # 401 on a route that server-side testing can never reproduce is the
    # device's OWN HTTP cache still holding an EARLIER real 401 response
    # from before this route existed/was fixed, since no response from
    # this server (this one included) ever set Cache-Control before now.
    # Explicit no-store on every JSON response (this function is the sole
    # chokepoint every route already uses) closes that off permanently,
    # without touching auth logic itself.
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _authenticated(handler: BaseHTTPRequestHandler, token: str) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return secrets.compare_digest(auth[len("Bearer "):], token)


class Handler(BaseHTTPRequestHandler):
    token: str = ""  # set by serve_forever() below before the server starts

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # V10.3R3 delivery-surface fix (Founder-authorized 2026-08-26,
        # PHYSICAL_S25_PREVIOUS_DOWNLOAD=FAIL investigation, section 1:
        # "Inspect live access logs while making an external-equivalent
        # request"). Was previously a hard no-op ("quiet by default;
        # audit.py is the real trail") -- true for job-level actions, but
        # audit.py never sees a raw HTTP request like a phone's browser
        # hitting /download/latest.apk, so there was NO way to ever see
        # what a real device's request actually looked like. This logs
        # only the request line, response status, and User-Agent -- via
        # journalctl (the same tool already used throughout this
        # campaign) -- never headers, never the Authorization value.
        ua = self.headers.get("User-Agent", "-") if hasattr(self, "headers") else "-"
        print(f'{self.client_address[0]} "{self.requestline}" {args[0] if args else "-"} UA={ua!r}', flush=True)

    def _serve_apk_delivery(self, *, body: bool) -> None:
        """Real file-size/Content-Length response for the bounded,
        fixed-path APK download route(s) -- no filesystem path is ever
        built from request input, so there is no traversal surface.
        Honest 404 if the staged file is ever missing, never a fabricated
        success.

        V10.4 delivery-finalization repair (Founder-authorized
        2026-08-26): real physical S25 evidence showed Chrome receiving
        100% of the APK bytes (44.32MB/44.32MB) but never finalizing the
        download. Root cause: this route always answered 200 with the
        full body regardless of any Range header and never advertised
        Accept-Ranges -- Android Chrome's DownloadManager issues
        Range-based GETs for large files as part of its own completion
        bookkeeping, and a 200-with-full-body reply to a Range request
        never gives it the partial-content signal it is waiting for, even
        though every byte physically arrived once. Real Range/206 support
        (plus ETag/Last-Modified so a resumed request can be validated
        against the exact same file) closes that gap."""
        if not APK_DELIVERY_FILE.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        stat = APK_DELIVERY_FILE.stat()
        size = stat.st_size
        etag = _apk_etag(APK_DELIVERY_FILE, size, stat.st_mtime_ns)
        last_modified = formatdate(stat.st_mtime, usegmt=True)

        range_header = self.headers.get("Range", "") if hasattr(self, "headers") else ""
        start, end, partial = 0, size - 1, False
        if range_header:
            match = _RANGE_RE.match(range_header.strip())
            if not match or (match.group(1) == "" and match.group(2) == ""):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw_start, raw_end = match.group(1), match.group(2)
            if raw_start == "":
                start = max(size - int(raw_end), 0)
                end = size - 1
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end != "" else size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        content_length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Disposition", f'attachment; filename="{APK_DELIVERY_FILE.name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if body:
            with open(APK_DELIVERY_FILE, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def _provision_token(self) -> None:
        """PERSONAL_FOUNDER_APP silent auth: issues the real, existing
        bearer token to the app on first launch, IF AND ONLY IF this
        specific request was proxied by `tailscale serve` for the
        Founder's own already-enrolled device -- verified via the real
        identity headers tailscaled itself injects (see the constants'
        docstring above), never a client-supplied claim. Any mismatch is
        a hard 403, no partial/degraded success. The token is read fresh
        from TOKEN_FILE and returned in the JSON body exactly once per
        call -- never logged (log_message is already a no-op on this
        class) and never written anywhere but this one response."""
        login = self.headers.get("Tailscale-User-Login", "")
        forwarded_ip = self.headers.get("X-Forwarded-For", "")
        if not secrets.compare_digest(login, FOUNDER_TAILNET_LOGIN) or not secrets.compare_digest(forwarded_ip, FOUNDER_DEVICE_TAILNET_IP):
            _json_response(self, 403, {"error": "founder device authentication required"})
            return
        _json_response(self, 200, {"token": self.token})

    def do_HEAD(self) -> None:  # noqa: N802 — http.server's required method name
        # Only the bounded APK delivery route(s) support HEAD; every other
        # path keeps BaseHTTPRequestHandler's existing default (501), an
        # unchanged behavior for the rest of this already-proven server.
        if urlparse(self.path).path in (APK_DELIVERY_ROUTE, APK_DELIVERY_LATEST_ROUTE, APK_DELIVERY_TEST_ROUTE):
            self._serve_apk_delivery(body=False)
            return
        self.send_error(501, "Unsupported method (HEAD)")

    def do_GET(self) -> None:  # noqa: N802 — http.server's required method name
        path = urlparse(self.path).path
        if path in (APK_DELIVERY_ROUTE, APK_DELIVERY_LATEST_ROUTE, APK_DELIVERY_TEST_ROUTE):
            self._serve_apk_delivery(body=True)
            return
        if path == "/health":
            # Deployment-drift check (2026-08-24): flags when a source file
            # this process actually imported has been modified ON DISK
            # since this process started -- the exact, cheap, one-call
            # signal that would have caught the stale-service bug above
            # immediately instead of after a full real-device retest cycle.
            # Read-only (os.path.getmtime), never restarts anything itself.
            stale_modules = []
            for mod_name, mod in (("founder_conversation", founder_conversation), ("live_sensor_api", None)):
                try:
                    src_path = Path(mod.__file__) if mod is not None else Path(__file__)
                    mtime = datetime.fromtimestamp(src_path.stat().st_mtime, tz=timezone.utc)
                    if mtime > PROCESS_STARTED_AT:
                        stale_modules.append(mod_name)
                except OSError:
                    continue
            _json_response(self, 200, {
                "status": "ok",
                "process_started_at": PROCESS_STARTED_AT.isoformat(),
                "stale_code_detected": bool(stale_modules),
                "stale_modules": stale_modules,
            })
            return
        if path in ("/", "/app"):
            static_file = STATIC_CLIENT_FILE if path == "/" else STATIC_APP_FILE
            if static_file.exists():
                body = static_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # Real-device debugging (2026-08-19): explicit no-store rules
                # out a stale cached copy as a confound on a mobile browser
                # while a page is actively being iterated on.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                _json_response(self, 404, {"error": "page not found"})
            return

        m_open = _OPEN_PATH_RE.match(path)
        if m_open:
            approval_id = m_open.group(1)
            deep_link = f"mrsilent://approval/{approval_id}"
            body = (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>Open in MR. SILENT</title>"
                f"<meta http-equiv=\"refresh\" content=\"0; url={deep_link}\">"
                "<style>body{font-family:sans-serif;background:#111;color:#eee;display:flex;"
                "flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0}"
                "a{margin-top:20px;padding:14px 28px;background:#ffb020;color:#111;"
                "text-decoration:none;border-radius:8px;font-weight:bold}</style>"
                f"<script>location.replace({json.dumps(deep_link)});</script>"
                "</head><body>"
                "<div>Opening MR. SILENT&hellip;</div>"
                f"<a href=\"{deep_link}\">OPEN IN MR. SILENT</a>"
                "</body></html>"
            )
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            return

        if not _authenticated(self, self.token):
            _json_response(self, 401, {"error": "missing or invalid bearer token"})
            return

        if path == "/sessions/active":
            _json_response(self, 200, {"active_sessions": [asdict(s) for s in gov.list_active_sessions()]})
            return

        if path == "/conversation/active":
            # ONGOING CHAT PERSISTENCE (V10.3R2, Founder-authorized
            # 2026-08-26): the real, durable active thread -- extends the
            # EXISTING single conversation_context.json store (see
            # founder_conversation.get_active_thread()'s own comment),
            # never a second conversation authority.
            _json_response(self, 200, founder_conversation.get_active_thread())
            return

        if path == "/pending-requests":
            # Founder Approvals UI (2026-08-19, MR. SILENT App Priority D;
            # federated 2026-08-20, App/Command Orchestration Phase 2;
            # Normal/History split 2026-08-20, real S25 Ultra device
            # finding): the SAME real data /chat's QUERY_PENDING_REQUESTS
            # intent already returns. Now includes Pulse's real
            # authoritative unified inbox alongside Render's own, via
            # founder_conversation.combined_pending(). Default view is
            # NORMAL only (real, actionable items) -- real device testing
            # found 52 pending items dominated by this project's own
            # regression-test artifacts, unusable as a daily Founder
            # queue. founder_view_filter.split_normal_and_history() NEVER
            # deletes or resolves anything; ?history=true returns the full
            # unfiltered list (still real, still authoritative) for an
            # Admin/debug view.
            from evolution import founder_view_filter
            pending, pulse_reachable, pulse_error = founder_conversation.combined_pending()
            normal, history = founder_view_filter.split_normal_and_history(pending)
            qs = parse_qs(urlparse(self.path).query)
            show_history = qs.get("history", ["false"])[0].lower() == "true"
            payload = {
                "pending_requests": (normal + history) if show_history else normal,
                "normal_count": len(normal), "history_count": len(history),
                "pulse_reachable": pulse_reachable,
            }
            if pulse_error:
                payload["pulse_error"] = pulse_error
            _json_response(self, 200, payload)
            return

        if path == "/projects":
            # Projects view (App/Command Orchestration milestone,
            # 2026-08-20; refederated to Pulse, App Convergence milestone,
            # 2026-08-24): the same real campaign.py Campaign data the
            # conversational MISSION_STATUS/MISSION_FINISHED/MISSION_FAILED
            # intents already expose, in one call, for the app's own
            # dedicated Projects panel — no new data shape, no second
            # mission store.
            #
            # Real defect found during App Convergence audit (2026-08-24):
            # this endpoint used to `import campaign as campaign_mod` and
            # call campaign_mod.list_all() LOCALLY -- against Render's own
            # campaigns/ directory, which is a stale, disconnected copy
            # (confirmed: missing every campaign created since Permanent
            # Studio Stewardship began, e.g. the real OmniScraper Cognitive
            # Wiring and Proposal Task-Scoping Fidelity campaigns). Fixed
            # to federate to Pulse (the sole canonical authority) over the
            # SAME pulse_bridge SSH pattern already used for Approvals and
            # Studio Intelligence, honest-failure posture included.
            #
            # Normal/History split (real S25 Ultra device finding,
            # 2026-08-20): the real accumulated campaign history contained
            # 1066+ records, the vast majority this project's own
            # regression-test fixtures -- unusable as a daily Founder
            # project list. founder_view_filter never deletes/mutates a
            # campaign; ?history=true returns everything, computed on
            # Pulse the same way.
            from evolution import pulse_bridge
            qs = parse_qs(urlparse(self.path).query)
            show_history = qs.get("history", ["false"])[0].lower() == "true"
            try:
                snapshot = pulse_bridge.get_pulse_autonomy_status(history=show_history)
            except pulse_bridge.PulseUnreachableError as e:
                _json_response(self, 200, {
                    "active_projects": [], "recently_completed": [], "recently_abandoned": [],
                    "normal_count": 0, "history_count": 0,
                    "pulse_reachable": False, "pulse_error": str(e),
                })
                return
            payload = dict(snapshot.get("projects") or {
                "active_projects": [], "recently_completed": [], "recently_abandoned": [],
                "normal_count": 0, "history_count": 0,
            })
            payload["pulse_reachable"] = True
            _json_response(self, 200, payload)
            return

        if path == "/mission":
            # MISSION view (App Convergence milestone, Founder-authorized
            # 2026-08-24): real, live autonomous-work visibility -- what
            # MR. SILENT is doing right now and why, without a terminal.
            # Same federated pulse_bridge.get_pulse_autonomy_status() call
            # /projects now uses, same honest-failure posture. Never a
            # fabricated percentage: progress_percent inside "mission" is
            # only ever completed_steps/total_steps*100 from a real
            # campaign, or null when there is no current campaign to
            # measure.
            from evolution import pulse_bridge
            try:
                snapshot = pulse_bridge.get_pulse_autonomy_status()
            except pulse_bridge.PulseUnreachableError as e:
                _json_response(self, 200, {
                    "pulse_reachable": False, "pulse_error": str(e),
                    "mission": None, "waiting_reason": None, "aggregate": None,
                    "home_summary": "Studio status unavailable right now.",
                })
                return
            # home_summary (Founder-authorized 2026-08-24, Home-UX polish
            # pass): the SAME real Pulse snapshot already fetched above,
            # condensed by founder_conversation.home_status_summary() into
            # the one short (<=2 phone-width lines) sentence Home itself
            # displays -- never a second/invented status source, and
            # never the raw cycle/recovery statistics the fuller
            # conversational answer is allowed to include as secondary
            # context.
            _json_response(self, 200, {
                "pulse_reachable": True,
                "generated_at": snapshot.get("generated_at"),
                "latest_cycle": snapshot.get("latest_cycle"),
                "next_autonomous_cycle": snapshot.get("next_autonomous_cycle"),
                "mission": snapshot.get("mission"),
                "waiting_reason": snapshot.get("waiting_reason"),
                "founder_approval_required": snapshot.get("founder_approval_required"),
                "founder_approvals_pending_count": snapshot.get("founder_approvals_pending_count"),
                "aggregate": snapshot.get("aggregate"),
                "home_summary": founder_conversation.home_status_summary(snapshot),
            })
            return

        if path == "/studio-status":
            # Studio Status view (2026-08-19, MR. SILENT App Priority B;
            # federated 2026-08-20, App/Command Orchestration Phase 2):
            # real-time, computed fresh from the SAME real sources the
            # daily-autonomy-reality artifact and /chat's QUERY_RECENT_WORK
            # intent already use — never a static/potentially-stale file
            # read, and no new evidence source. pending count now spans
            # both real authorities; pulse_reachable surfaces degraded
            # state honestly rather than silently under-reporting.
            import autonomous_cycle
            from evolution import founder_view_filter
            history = autonomous_cycle.cycle_history_summary(limit=20)
            latest = autonomous_cycle.latest_cycle()
            pending, pulse_reachable, pulse_error = founder_conversation.combined_pending()
            normal, hist = founder_view_filter.split_normal_and_history(pending)
            _json_response(self, 200, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "latest_cycle_status": latest.final_status if latest else "no cycles recorded yet",
                "latest_cycle_started_at": latest.started_at if latest else None,
                "recent_cycle_summary": history,
                # Real device finding (2026-08-20): the raw federated total
                # ("Needs your approval: 52") did not look like a clean
                # everyday Founder-action queue -- this count now reflects
                # only real, actionable items; history_requests_count is
                # the honest complement.
                "pending_founder_requests_count": len(normal),
                "history_requests_count": len(hist),
                "pulse_reachable": pulse_reachable,
                "pulse_error": pulse_error,
            })
            return

        if path == "/studio-intelligence":
            # P3 — Unified Studio Intelligence (Omni/God Mode Consolidation,
            # Founder-authorized 2026-08-21). Additive, separate from the
            # certified /studio-status endpoint (which is left byte-for-
            # byte unchanged) so this new, heavier, SSH-touching call can
            # never add latency/risk to the already-proven status path.
            # Fetched lazily by the app only when the Founder actually
            # opens the panel, same pattern as Approvals/Projects/Status.
            # Real signal only: organ_discovery.studio_health_summary()
            # (existing, certified, real Ollama/Provider B/engine health)
            # plus a real local resource snapshot; Pulse's snapshot is
            # attempted and honestly marked unreachable rather than
            # silently omitted, matching pulse_bridge's own posture.
            import organ_discovery
            from evolution import pulse_bridge

            render_health = organ_discovery.studio_health_summary()
            render_pressure = organ_discovery.render_resource_pressure()
            try:
                pulse_health = pulse_bridge.get_pulse_health()
                pulse_intel_reachable, pulse_intel_error = True, None
            except pulse_bridge.PulseUnreachableError as e:
                pulse_health, pulse_intel_reachable, pulse_intel_error = None, False, str(e)

            # Founder-attention items: real degraded/unavailable organ
            # entries, in plain language, never a raw service/queue name.
            attention_items = [
                f"{e['name']} is {e['status'].lower()}" + (f" — {e['reason']}" if e.get("reason") else "")
                for e in render_health.get("entries", [])
                if e.get("status") in ("DEGRADED", "UNAVAILABLE", "PARTIAL")
            ]

            _json_response(self, 200, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "render": {"organ_health": render_health, "resource_pressure": render_pressure},
                "pulse": pulse_health,
                "pulse_reachable": pulse_intel_reachable,
                "pulse_error": pulse_intel_error,
                "attention_items": attention_items,
            })
            return

        m = _SESSION_PATH_RE.match(path)
        if m and m.group(2) == "/status":
            session_id = m.group(1)
            s = gov.load(session_id)
            if s is None:
                _json_response(self, 404, {"error": "no such session"})
                return
            _json_response(self, 200, {
                "session_id": session_id, "active": gov.is_session_active(session_id),
                "scope": s.scope, "chunks_received": s.chunks_received, "bytes_received": s.bytes_received,
                "expires_at": s.expires_at,
            })
            return

        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/provision-token":
            self._provision_token()
            return

        if not _authenticated(self, self.token):
            _json_response(self, 401, {"error": "missing or invalid bearer token"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if path == "/chat":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            text = (fields.get("text") or "").strip()
            if not text:
                _json_response(self, 400, {"error": "'text' is required and cannot be empty"})
                return
            resolved = founder_conversation.handle_intent(text, requested_by="mrsilent_app")
            _json_response(self, 200, {
                "intent": resolved.intent, "raw_text": resolved.raw_text, "target": resolved.target,
                "ambiguous": resolved.ambiguous, "clarification_needed": resolved.clarification_needed,
                "answer": resolved.answer, "response_text": founder_conversation.format_response(resolved),
            })
            return

        if path == "/speak":
            # Spoken MR. SILENT voice output -- see PULSE_SPOKEN_REPLY_*
            # constants' comment above for the real architecture. The app
            # passes the SAME response_text /chat just returned; this never
            # re-reasons about the Founder's question, only synthesizes
            # already-decided text into audio in the real canonical voice.
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            text = (fields.get("text") or "").strip()
            if not text:
                _json_response(self, 400, {"error": "'text' is required and cannot be empty"})
                return
            try:
                pulse_token = PULSE_SPOKEN_REPLY_TOKEN_FILE.read_text().strip()
                req = urllib.request.Request(
                    f"http://{PULSE_TAILNET_IP}:{PULSE_SPOKEN_REPLY_PORT}/synthesize-speech-only",
                    data=json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {pulse_token}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    audio_bytes = resp.read()
            except urllib.error.HTTPError as e:
                # V10.3R5 diagnostic fix (Founder-authorized 2026-08-26,
                # CANONICAL_SPOKEN_REPLY=FAIL/INTERMITTENT section 5):
                # Pulse's own /synthesize-speech-only already classifies
                # its real upstream ElevenLabs status inside its own 502
                # detail body (e.g. "TTS provider error 401") after a real
                # retry -- this used to be silently discarded, reporting
                # only the generic wrapper 502 here every time regardless
                # of the actual cause. Forwarded verbatim now (Pulse's own
                # detail message never includes the API key). Read-only
                # diagnostic change -- never touches ElevenLabs, the
                # voice, or any credential.
                try:
                    pulse_detail = json.loads(e.read().decode()).get("detail")
                except Exception:  # noqa: BLE001 — best-effort; a malformed/empty body must not mask the real 502
                    pulse_detail = None
                _json_response(self, 502, {
                    "error": f"speech synthesis failed (provider status {e.code})",
                    "pulse_detail": pulse_detail,
                })
                return
            except Exception as e:  # noqa: BLE001 — Pulse/ElevenLabs reachability is best-effort; the Founder needs an honest, bounded error
                _json_response(self, 502, {"error": f"speech synthesis unreachable: {e}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio_bytes)
            return

        m_decision = _DECISION_PATH_RE.match(path)
        if m_decision:
            # Founder Approvals UI (2026-08-19, MR. SILENT App Priority D;
            # federated 2026-08-20, App/Command Orchestration Phase 2):
            # unlike the conversational APPROVE/DENY intent (which only
            # resolves when EXACTLY ONE request is pending — it correctly
            # refuses to guess which one from words alone), a button tap
            # in the app carries an explicit, unambiguous approval_id from
            # the specific card the Founder tapped. This is MORE precise
            # than the conversational path, never less safe: founder_
            # conversation.resolve_any_decision() routes by the id's own
            # real shape to the SAME real resolver every other approval
            # path already uses (Render's founder_request.py or Pulse's
            # real unified_approval_router.py/adapters) — never a new
            # authority mechanism, never a second approval database, and
            # can only ever resolve the ONE explicit id given.
            decision_word, approval_id = m_decision.group(1), m_decision.group(2)
            try:
                result = founder_conversation.resolve_any_decision(
                    approval_id, decision_word, note="resolved via MR. SILENT App approvals UI")
            except FileNotFoundError:
                _json_response(self, 404, {"error": f"no such pending request {approval_id!r}"})
                return
            except Exception as e:  # noqa: BLE001 — Pulse may be unreachable; the Founder needs an honest, bounded error, not a raw 500 traceback
                _json_response(self, 502, {"error": f"could not resolve {approval_id!r}: {e}"})
                return
            _json_response(self, 200, result)
            return

        if path == "/session":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            try:
                s = gov.create_session(
                    requested_by=fields.get("requested_by", "device_client"),
                    reason=fields.get("reason", ""), device_id=fields.get("device_id", ""),
                    scope=fields.get("scope", "both"), duration_s=fields.get("duration_s"),
                    founder_approved=True,  # a valid bearer token IS this API's real authorization signal — see module docstring
                )
            except gov.SensorGovernanceError as e:
                _json_response(self, 400, {"error": str(e)})
                return
            _json_response(self, 200, asdict(s))
            return

        if path == "/revoke-all":
            count = gov.revoke_all_sessions(requested_by="device_client_api", reason="revoke-all requested via API")
            _json_response(self, 200, {"revoked_count": count})
            return

        # ONGOING CHAT PERSISTENCE (V10.3R2, Founder-authorized
        # 2026-08-26): real durable message create/update, backed by the
        # SAME extended founder_conversation.py store as GET
        # /conversation/active above -- see that function's own comment.
        # Placed BEFORE the session-path guard below, which otherwise
        # 404s any path that isn't /session/... first (a real routing bug
        # caught by an actual end-to-end HTTP test against this exact
        # route, not assumed correct from reading the diff alone).
        if path == "/conversation/message":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            role = (fields.get("role") or "").strip()
            text = fields.get("text")
            if role not in ("founder", "silent") or not isinstance(text, str) or not text:
                _json_response(self, 400, {"error": "'role' (founder|silent) and non-empty 'text' are required"})
                return
            attachment_type = fields.get("attachment_type")
            status = fields.get("status") or "complete"
            client_message_id = fields.get("message_id")
            message = founder_conversation.append_thread_message(
                role, text, attachment_type=attachment_type, status=status,
                message_id=client_message_id if isinstance(client_message_id, str) else None,
            )
            _json_response(self, 200, message)
            return

        if path == "/conversation/new":
            _json_response(self, 200, founder_conversation.start_new_thread())
            return

        # V10.3R5 real-incident fix (Founder-authorized 2026-08-26,
        # MEDIA_SINGLE_FLIGHT_PHYSICAL=FAIL): durable, backend-enforced
        # media-transaction idempotency -- see founder_conversation.
        # claim_media_transaction()'s own comment for the real evidence
        # this closes. Same route-ordering discipline as the block above
        # (before the /session/... guard).
        if path == "/conversation/media-transaction/claim":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            transaction_id = fields.get("transaction_id")
            kind = fields.get("kind") or "image"
            if not isinstance(transaction_id, str) or not transaction_id:
                _json_response(self, 400, {"error": "'transaction_id' is required"})
                return
            try:
                result = founder_conversation.claim_media_transaction(transaction_id, kind=kind)
            except ValueError as e:
                _json_response(self, 400, {"error": str(e)})
                return
            _json_response(self, 200, result)
            return

        if path == "/conversation/media-transaction/release":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            transaction_id = fields.get("transaction_id")
            status = fields.get("status") or "complete"
            if not isinstance(transaction_id, str) or not transaction_id:
                _json_response(self, 400, {"error": "'transaction_id' is required"})
                return
            released = founder_conversation.release_media_transaction(transaction_id, status=status)
            _json_response(self, 200, {"released": released, "transaction_id": transaction_id})
            return

        cm = _CONVERSATION_MESSAGE_PATH_RE.match(path)
        if cm:
            message_id = cm.group(1)
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid JSON body"})
                return
            text = fields.get("text")
            status = fields.get("status")
            if text is None and status is None:
                _json_response(self, 400, {"error": "at least one of 'text' or 'status' is required"})
                return
            ok = founder_conversation.update_thread_message(message_id, text=text, status=status)
            if not ok:
                _json_response(self, 404, {"error": f"no such message {message_id!r} in the active thread"})
                return
            _json_response(self, 200, {"updated": True, "message_id": message_id})
            return

        m = _SESSION_PATH_RE.match(path)
        if not m:
            _json_response(self, 404, {"error": "not found"})
            return
        session_id = m.group(1)

        if m.group(3) and m.group(3).startswith("chunk/"):
            modality, index = m.group(4), int(m.group(5))
            try:
                result = bridge.ingest_chunk(session_id, modality=modality, chunk_bytes=raw_body, chunk_index=index)
            except bridge.LiveSensorIngestError as e:
                _json_response(self, 409, {"error": str(e)})
                return
            _json_response(self, 200, result)
            return

        if m.group(2) == "/finalize":
            try:
                fields = json.loads(raw_body.decode()) if raw_body else {}
            except json.JSONDecodeError:
                fields = {}
            try:
                result = bridge.finalize_session(session_id, question=fields.get("question"))
            except bridge.LiveSensorIngestError as e:
                _json_response(self, 404, {"error": str(e)})
                return
            # finalize_session()'s "results" values are real AudioResult/
            # VideoResult DATACLASS instances (by design — tests/test_live_
            # sensor_governance.py correctly relies on attribute access
            # against them). json.dumps(default=str) would otherwise
            # silently stringify them via repr() instead of properly
            # serializing — found via a real live API test, not assumed —
            # so this boundary explicitly converts to dicts, only at the
            # HTTP response edge, never changing finalize_session()'s own
            # real return contract.
            serializable = dict(result)
            serializable["results"] = {
                k: (asdict(v) if is_dataclass(v) else v)
                for k, v in result["results"].items()
            }
            # Camera-in-conversation (2026-08-19): when a real Founder
            # question was answered from a real vision analysis, feed that
            # real Q&A into the SAME conversational memory /chat's own
            # natural-language fallback reads — so "Why do you think
            # that?" typed afterward (a plain /chat call, no camera
            # involved) resolves against the real vision answer, exactly
            # like a text-only follow-up already does. Never fabricates
            # anything: only fires when analyze_video() itself produced a
            # real, non-empty answer for a real question.
            question = fields.get("question")
            video_answer = (serializable["results"].get("video") or {}).get("answer")
            if question and video_answer:
                # NOTE: deliberately NOT a local `from evolution import
                # founder_conversation` here — that shadowed the real
                # module-level import (line ~67) for THIS FUNCTION'S ENTIRE
                # scope, including the completely separate /chat branch,
                # causing a real UnboundLocalError there (found via a real
                # live HTTP test, not assumed). founder_conversation is
                # already imported at module level; just use it directly.
                try:
                    founder_conversation.remember_external_turn(question, video_answer)
                except Exception:  # noqa: BLE001 — conversational memory is a convenience, never fatal to the real analysis result
                    pass
            _json_response(self, 200, serializable)
            return

        if m.group(2) == "/end":
            try:
                s = gov.end_session(session_id, reason="ended via API")
            except gov.SensorGovernanceError as e:
                _json_response(self, 404, {"error": str(e)})
                return
            _json_response(self, 200, asdict(s))
            return

        if m.group(2) == "/revoke":
            revoked = gov.revoke_session(session_id, requested_by="device_client_api", reason="revoked via API")
            _json_response(self, 200, {"revoked": revoked})
            return

        _json_response(self, 404, {"error": "not found"})


def run(*, port: int = DEFAULT_PORT) -> None:
    token = get_or_create_token()
    Handler.token = token
    server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    print(f"live_sensor_api listening on {BIND_HOST}:{port} (bearer token in {TOKEN_FILE})")
    server.serve_forever()


if __name__ == "__main__":
    run()
