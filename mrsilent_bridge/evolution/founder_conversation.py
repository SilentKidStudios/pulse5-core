"""
Natural Founder Conversation — bounded, deterministic pattern matching over
free text, resolving to a FIXED set of explicit governed intents
(Founder-authorized 2026-08-18, Milestone D). Not an LLM call: every
mapping here is a plain, auditable rule, so behavior is fully predictable
and testable — matching this project's standing preference for
deterministic, explainable logic over another model call wherever one
isn't genuinely needed.

Every read-only intent is answered from REAL, existing data (autonomous_
cycle.py's cycle history, evolution/proposal.py's proposals, evolution/
founder_request.py's pending escalations, organ_discovery.duty_check(),
evolution/proposal.py's lessons) — never fabricated. Every authority-
changing intent (approve/deny) requires an UNAMBIGUOUS target: if exactly
one Founder request is pending, "approve that" resolves to it; if more
than one is pending, or none, the request is classified AMBIGUOUS and
resolve_intent() returns a clarification question instead of guessing —
"Never infer Founder approval from casual conversation" is enforced by
construction, not by convention.

Existing explicit commands (cli.py subcommands, founder_request.
resolve_founder_decision() called directly) remain the deterministic
fallback/admin path — this module is a convenience layer ON TOP, never a
replacement.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INTENT_QUERY_RECENT_WORK = "QUERY_RECENT_WORK"
INTENT_QUERY_PENDING_REQUESTS = "QUERY_PENDING_REQUESTS"
INTENT_QUERY_EVIDENCE = "QUERY_EVIDENCE"
INTENT_QUERY_ORGAN_HEALTH = "QUERY_ORGAN_HEALTH"
INTENT_QUERY_LEARNING = "QUERY_LEARNING"
INTENT_APPROVE = "APPROVE"
INTENT_DENY = "DENY"
INTENT_RETRY_ALTERNATIVE = "RETRY_ALTERNATIVE"
INTENT_UNRECOGNIZED = "UNRECOGNIZED"

# Conversational Mission Control intents (Founder-authorized 2026-08-18) —
# act on the SAME canonical campaign.py Campaign every CLI/autonomous-cycle
# path already uses. No parallel mission store.
INTENT_MISSION_STATUS = "MISSION_STATUS"
INTENT_MISSION_FINISHED = "MISSION_FINISHED"
INTENT_MISSION_FAILED = "MISSION_FAILED"
INTENT_MISSION_BLOCKED_REASON = "MISSION_BLOCKED_REASON"
INTENT_MISSION_PAUSE = "MISSION_PAUSE"
INTENT_MISSION_RESUME = "MISSION_RESUME"
INTENT_MISSION_CANCEL = "MISSION_CANCEL"
INTENT_MISSION_PRIORITY_UP = "MISSION_PRIORITY_UP"
INTENT_MISSION_PRIORITY_DOWN = "MISSION_PRIORITY_DOWN"
INTENT_MISSION_OBJECTIVE_CHANGE = "MISSION_OBJECTIVE_CHANGE"
INTENT_HANDLE_EVERYTHING = "HANDLE_EVERYTHING"
INTENT_PLANNING_NEXT = "PLANNING_NEXT"

# Mission origination (Founder-authorized 2026-08-20, App/Command
# Orchestration milestone): the missing INPUT layer that lets a Founder
# START new governed work from ordinary conversation, not just manage
# missions that already exist. Reuses evolution/mission_decomposition.
# decompose_objective() entirely — that module already turns free text
# into a real, capability-grounded Campaign via campaign.create(); it was
# previously reachable only from cli.py's `mission` subcommand. This adds
# no new intelligence and no new authority: decompose_objective() itself
# only ever creates a Campaign (bookkeeping) — the real engineering work
# it schedules still goes through the SAME existing proposal/advance_
# eligible/job_ledger pipeline with every authority/Founder-promotion gate
# intact, unchanged by this milestone.
INTENT_MISSION_CREATE = "MISSION_CREATE"

# Natural-language fallback (Founder-authorized 2026-08-19, "Honest
# Closure" real-device conversation test): reached ONLY when no
# deterministic pattern above matched. Never an authority-changing path —
# APPROVE/DENY and every mutating mission intent are matched deterministic-
# ally, above, before this is ever reached; see _natural_language_fallback().
INTENT_NATURAL_LANGUAGE = "NATURAL_LANGUAGE"

# Plain small talk (real S25 Ultra defect, 2026-08-24: "Hello mr silent" was
# falling through to the NATURAL_LANGUAGE/LLM path with no deterministic
# match, which then unconditionally injected whatever stale question/answer
# happened to be sitting in conversation_context.json — see
# _natural_language_fallback()'s history_block). A bare greeting never
# needs continuity or reasoning; resolving it deterministically, with no
# LLM call and no context read at all, is the direct fix for that specific
# real-device scenario. Checked FIRST — greeting words share no vocabulary
# with any other pattern below, so there is no real contention risk.
INTENT_GREETING = "GREETING"

# Ordered: first matching pattern wins. Deliberately simple substring/regex
# rules, not a grammar — bounded scope, easy to audit, easy to extend.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (INTENT_GREETING, re.compile(
        r"^\s*(hi|hey|hello|yo|sup)\b[\s,!.]*(mr\.?\s*silent)?[\s!.]*$|"
        r"^\s*good (morning|afternoon|evening)\b[\s,!.]*(mr\.?\s*silent)?[\s!.]*$", re.I)),
    # QUERY_LEARNING checked before QUERY_RECENT_WORK — "what have you
    # learned this week" must not be intercepted by the more generic
    # "what have you..." recent-work pattern.
    (INTENT_QUERY_LEARNING, re.compile(r"\b(what.*learn(ed|t)?|lessons?)\b", re.I)),
    # Checked before MISSION_OBJECTIVE_CHANGE/MISSION_STATUS: "new
    # mission"/"new objective"/"new project" is a distinct trigger phrase
    # from "change the objective" or "active objectives" (status query) —
    # no wording overlap, but kept first for auditability since this is
    # the one MUTATING pattern that creates real new work.
    (INTENT_MISSION_CREATE, re.compile(
        r"\b(?:new mission|new objective|new project|"
        r"start (?:a |the )?(?:new )?(?:mission|project|objective)|"
        r"create (?:a |the )?(?:new )?(?:mission|project|objective)|"
        r"give (?:you|mr\.?\s*silent) a new (?:mission|objective|project))\b", re.I)),
    (INTENT_MISSION_OBJECTIVE_CHANGE, re.compile(r"\bchange the objective\b", re.I)),
    (INTENT_MISSION_PRIORITY_UP, re.compile(r"\b(priority one|top priority|highest priority|make.*priority|prioriti[sz]e)\b", re.I)),
    (INTENT_MISSION_PRIORITY_DOWN, re.compile(r"\blower the priority|deprioriti[sz]e|lowest priority\b", re.I)),
    (INTENT_MISSION_CANCEL, re.compile(r"\bcancel\b", re.I)),
    (INTENT_MISSION_PAUSE, re.compile(r"^\s*pause\b|\bpause (that|it|the)\b", re.I)),
    (INTENT_MISSION_RESUME, re.compile(r"^\s*resume\b|\bresume (that|it|the)\b", re.I)),
    (INTENT_MISSION_BLOCKED_REASON, re.compile(r"\bwhat'?s blocking|why.*(waiting|blocked)\b", re.I)),
    (INTENT_MISSION_FINISHED, re.compile(r"\bwhat finished|what'?s (done|complete)\b", re.I)),
    (INTENT_MISSION_FAILED, re.compile(r"\bwhat failed\b", re.I)),
    (INTENT_HANDLE_EVERYTHING, re.compile(r"\bhandle everything|continue until you need me\b", re.I)),
    (INTENT_PLANNING_NEXT, re.compile(r"\bwhat are you planning|what'?s next\b", re.I)),
    (INTENT_MISSION_STATUS, re.compile(r"\bwhat missions|active (missions|campaigns|objectives)\b", re.I)),
    # Widened 2026-08-19 (Founder real-device conversation test): the
    # ORIGINAL pattern only caught "what have you done/been doing" —
    # "what is MR. SILENT doing autonomously right now" (the Founder's own
    # literal test sentence) fell through to UNRECOGNIZED. Deliberately
    # narrow to a SELF-reference (mr. silent/he/you) so it doesn't shadow
    # an organ-specific "what is Provider B doing" (below) — checked
    # BEFORE organ health precisely so ordering resolves that correctly.
    (INTENT_QUERY_RECENT_WORK, re.compile(
        r"\b(what (did|have) you (do|done|been doing)|what are you working on|"
        r"what are you doing( right now)?|what'?s running|"
        r"what('s| is) (mr\.?\s*silent|he)\s+doing\b|"
        r"are you (?:actually |really )?working autonomously|overnight)\b", re.I)),
    (INTENT_QUERY_EVIDENCE, re.compile(
        r"\b(show me the evidence|evidence|proof|why.*approval|why.*need|"
        r"tell me about (the )?\w+ one|details? (on|about|for)\b)\b", re.I)),
    # Widened 2026-08-19: "what's Provider B doing" / "how's Ollama doing"
    # are natural rephrasings of the same real organ-health question the
    # original "wrong with/health of/status of" phrasings already answer.
    (INTENT_QUERY_ORGAN_HEALTH, re.compile(
        r"\bwhat'?s wrong with\b|\bhealth of\b|\bstatus of\b|"
        r"\b(?:what'?s|what is|how'?s|how is)\s+[a-zA-Z0-9_.\-]+(?:\s+[a-zA-Z0-9_.\-]+)?\s+doing\b", re.I)),
    # Widened 2026-08-19: "anything need me?" is the same real question as
    # "what needs my approval" — the LLM fallback answered it but not
    # reliably enough to trust over the existing deterministic, guaranteed-
    # correct evidence path this project already has for it.
    (INTENT_QUERY_PENDING_REQUESTS, re.compile(
        r"\b(pending|waiting on me|need(s)? (my )?approval|can you handle this yourself|"
        r"anything need me|what needs me)\b", re.I)),
    (INTENT_APPROVE, re.compile(r"^\s*(approve(d)?|yes|go ahead|do it|approve that)\b", re.I)),
    (INTENT_DENY, re.compile(r"^\s*(deny|denied|no\b|don'?t do that|stop|reject)\b", re.I)),
    (INTENT_RETRY_ALTERNATIVE, re.compile(r"\btry (another|a different)\b", re.I)),
]

_ORGAN_TARGET_RE = re.compile(r"(?:wrong with|health of|status of)\s+([a-zA-Z0-9_.\-]+)", re.I)
_ORGAN_TARGET_DOING_RE = re.compile(r"(?:what'?s|what is|how'?s|how is)\s+([a-zA-Z0-9_.\-]+(?:\s+[a-zA-Z0-9_.\-]+)?)\s+doing\b", re.I)
# Strips the trigger phrase itself, leaving only the actual objective text
# — mirrors MISSION_OBJECTIVE_CHANGE's own inline extraction convention
# (handle_intent() below) rather than adding a second extraction style.
_MISSION_CREATE_OBJECTIVE_RE = re.compile(
    r"\b(?:new mission|new objective|new project|"
    r"start (?:a |the )?(?:new )?(?:mission|project|objective)|"
    r"create (?:a |the )?(?:new )?(?:mission|project|objective)|"
    r"give (?:you|mr\.?\s*silent) a new (?:mission|objective|project))"
    r"\s*(?:to|:|-|—|is to|for)?\s*(.+)$", re.I)

_CONTEXT_PATH = Path(__file__).resolve().parent.parent / "conversation_context.json"
_ORDINAL_WORDS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}


def _load_context() -> dict[str, Any]:
    if _CONTEXT_PATH.exists():
        try:
            return json.loads(_CONTEXT_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_context(ctx: dict[str, Any]) -> None:
    _CONTEXT_PATH.write_text(json.dumps(ctx, indent=2))


def _remember(campaign_ids: list[str] | None = None, referenced: str | None = None) -> None:
    """State-backed conversational continuity (Goal 4): a small, durable,
    single-JSON cache of what was just shown/referenced — NOT a mission
    store (campaign.py owns all real mission state); this only lets
    ordinal ('the second one') and anaphoric ('it'/'that') references
    resolve naturally across turns."""
    ctx = _load_context()
    if campaign_ids is not None:
        ctx["last_shown_missions"] = campaign_ids
        ctx["last_shown_at"] = datetime.now(timezone.utc).isoformat()
    if referenced is not None:
        ctx["last_referenced_mission"] = referenced
    _save_context(ctx)


def _resolve_mission_reference(text: str) -> tuple[str | None, bool, str | None]:
    """Resolves a mission reference from text — explicit campaign_id
    prefix, ordinal ('the second one') against the last-shown list,
    anaphoric ('it'/'that') against the last-referenced mission (or the
    sole active/paused one), or a substring match against a real
    objective's text. NEVER guesses: returns (None, True, question) when
    genuinely ambiguous rather than picking one."""
    import campaign as campaign_mod
    all_campaigns = campaign_mod.list_all()
    active = [c for c in all_campaigns if c.status in (campaign_mod.STATUS_ACTIVE, campaign_mod.STATUS_PAUSED)]

    id_match = re.search(r"\b([0-9a-f]{8})[0-9a-f-]*\b", text, re.I)
    if id_match:
        prefix = id_match.group(1).lower()
        matches = [c for c in all_campaigns if c.campaign_id.lower().startswith(prefix)]
        if len(matches) == 1:
            return matches[0].campaign_id, False, None

    ctx = _load_context()
    last_shown = ctx.get("last_shown_missions") or []
    for word, idx in _ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.I) and idx < len(last_shown):
            cid = last_shown[idx]
            if campaign_mod.load(cid) is not None:
                return cid, False, None

    if re.search(r"\b(it|that|this)\b", text, re.I):
        last_ref = ctx.get("last_referenced_mission")
        if last_ref and campaign_mod.load(last_ref) is not None:
            return last_ref, False, None
        if len(active) == 1:
            return active[0].campaign_id, False, None

    if len(text.strip()) > 8:
        # Real bug found via testing (2026-08-18): comparing the WHOLE raw
        # utterance against the objective (`text.lower() in c.objective.
        # lower()`) can never match, since command words ("cancel mission
        # X", "why is mission X waiting on me") are never themselves a
        # substring of the stored objective text. It was masked in earlier
        # rounds only because a clean environment usually had exactly one
        # active/paused campaign, so resolution fell through to the `len(
        # active) == 1` fallback below without ever exercising this branch.
        # Correct direction: pull the CONTENT words out of the utterance
        # (dropping command/filler words) and check those against each
        # objective — this is what actually lets "cancel mission
        # very-distinctive-cancel-target-xyz" find the campaign whose
        # objective contains that distinctive token.
        _STOPWORDS = {
            "mission", "missions", "cancel", "abandon", "kill", "stop",
            "pause", "resume", "make", "priority", "one", "top", "highest",
            "lowest", "lower", "raise", "increase", "decrease", "the", "of",
            "is", "why", "what", "waiting", "blocked", "blocking", "on",
            "me", "that", "this", "it", "please", "could", "you", "for",
            "change", "objective", "to", "a", "new", "synthetic", "test",
            "prioritize", "prioritise", "deprioritize", "deprioritise",
        }
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9-]{2,}", text.lower()) if w not in _STOPWORDS]
        if words:
            scored = sorted(
                ((c, sum(1 for w in words if w in c.objective.lower())) for c in active),
                key=lambda pair: -pair[1],
            )
            scored = [pair for pair in scored if pair[1] > 0]
            if scored:
                top_score = scored[0][1]
                name_candidates = [c for c, score in scored if score == top_score]
                if len(name_candidates) == 1:
                    return name_candidates[0].campaign_id, False, None

    if len(active) == 1:
        return active[0].campaign_id, False, None
    if not active:
        return None, True, "There are no active or paused missions right now."
    listing = "; ".join(f"{c.objective[:60]!r} ({c.campaign_id[:8]})" for c in active)
    return None, True, f"Which mission did you mean? Active/paused right now: {listing}"


@dataclass
class ResolvedIntent:
    intent: str
    raw_text: str
    target: str | None = None
    ambiguous: bool = False
    clarification_needed: str | None = None
    answer: dict[str, Any] | None = None


def _match_pattern(text: str) -> str:
    for intent, pattern in _PATTERNS:
        if pattern.search(text):
            return intent
    return INTENT_UNRECOGNIZED


def resolve_intent(text: str) -> ResolvedIntent:
    """Classifies free text into an explicit intent. Does NOT execute
    anything by itself for authority-changing intents unless the target is
    unambiguous — see handle_intent() for execution."""
    text = text.strip()
    intent = _match_pattern(text)
    resolved = ResolvedIntent(intent=intent, raw_text=text)

    if intent == INTENT_QUERY_ORGAN_HEALTH:
        m = _ORGAN_TARGET_RE.search(text) or _ORGAN_TARGET_DOING_RE.search(text)
        resolved.target = m.group(1) if m else None
        if resolved.target is None:
            resolved.ambiguous = True
            resolved.clarification_needed = "Which organ/capability did you mean? Please name it (e.g. 'what's wrong with omniforge')."

    return resolved


_NL_CONTEXT_KEY_QUESTION = "last_nl_question"
_NL_CONTEXT_KEY_ANSWER = "last_nl_answer"
# Real S25 Ultra defect (2026-08-24): conversation_context.json is ONE
# global, unscoped file shared by every caller of this module — /chat has
# no session/device identifier at all (live_sensor_api.py passes a fixed
# literal requested_by="mrsilent_app" for every request), and this file
# previously had no timestamp on what it stored. A bare greeting could
# therefore surface an engineering test turn, or a camera/vision answer,
# from arbitrarily far in the past or from an entirely different
# conversation. _NL_CONTEXT_KEY_AT bounds how long a stored turn is still
# eligible to be quoted back (see _natural_language_fallback());
# _NL_CONTEXT_KEY_SOURCE records what kind of turn produced it (a plain
# text NL answer, a deterministic intent, or a camera/vision answer) so
# it's disclosed honestly rather than blended into an undifferentiated
# "earlier in this conversation".
_NL_CONTEXT_KEY_AT = "last_nl_at"
_NL_CONTEXT_KEY_SOURCE = "last_nl_source"
_NL_CONTEXT_STALE_AFTER_S = 600  # 10 min — long enough for a genuine pause mid-conversation, short enough to exclude old dev/test or unrelated-session debris
_SCORPIO_RE = re.compile(r"\bscorpio", re.I)
_REASONING_TIMEOUT_S = 60


def remember_external_turn(question: str, answer: str) -> None:
    """Camera-in-conversation (Founder-authorized 2026-08-19): lets another
    real, already-tested subsystem (live_sensor_api.py's /finalize, when a
    real vision question was answered) feed its own real Q&A into the SAME
    conversational memory _natural_language_fallback() reads — so a plain
    text follow-up like 'Why do you think that?' after a camera answer
    resolves against it exactly the same way a text-only follow-up already
    does, without vision analysis needing to know anything about
    founder_conversation's own internals beyond this one call. Reuses the
    EXISTING _load_context()/_save_context() persistence — no new memory
    store, no new file."""
    if not question or not answer:
        return
    ctx = _load_context()
    ctx[_NL_CONTEXT_KEY_QUESTION] = question
    ctx[_NL_CONTEXT_KEY_ANSWER] = answer
    ctx[_NL_CONTEXT_KEY_AT] = datetime.now(timezone.utc).isoformat()
    ctx[_NL_CONTEXT_KEY_SOURCE] = "camera_vision"
    _save_context(ctx)


# ============================================================
# ONGOING CHAT PERSISTENCE (V10.3R2, Founder-authorized 2026-08-26):
# real S25 physical evidence showed the Founder OS app's Conversation
# panel had no durable state at all -- closing/backgrounding the app lost
# the entire thread, with no way to reopen or restore it. Audited first
# (per the Founder's own explicit instruction): the ONLY existing
# conversation-adjacent persistence in this Studio is this exact file's
# _load_context()/_save_context() single-JSON cache (conversation_
# context.json), which until now held only a single-turn NL-continuity
# hint (last_shown_missions/last_referenced_mission/one camera-vision
# Q&A pair) -- never a real multi-turn message ledger, never a
# conversation_id. This EXTENDS that same, single, already-canonical
# store (MR_SILENT_LOGICAL_ENTITY_COUNT=1 -- never a second competing
# conversation authority) rather than creating a new one. Bounded to
# MAX_THREAD_MESSAGES so the file can never grow unbounded; a Founder-
# explicit "new chat" archives (never deletes) the prior thread, bounded
# to MAX_ARCHIVED_THREADS.
MAX_THREAD_MESSAGES = 200
MAX_ARCHIVED_THREADS = 10
_ACTIVE_THREAD_KEY = "active_thread"
_ARCHIVED_THREADS_KEY = "archived_threads"


def _new_thread(conversation_id: str | None = None) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id or uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "messages": [],
    }


def get_active_thread() -> dict[str, Any]:
    """Returns the real, durable active conversation thread -- creating
    one (empty) the first time this is ever called, so callers never have
    to special-case "no thread yet" themselves."""
    ctx = _load_context()
    thread = ctx.get(_ACTIVE_THREAD_KEY)
    if not isinstance(thread, dict) or "conversation_id" not in thread:
        thread = _new_thread()
        ctx[_ACTIVE_THREAD_KEY] = thread
        _save_context(ctx)
    return thread


_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def append_thread_message(
    role: str, text: str, *,
    attachment_type: str | None = None,
    status: str = "complete",
    message_id: str | None = None,
) -> dict[str, Any]:
    """Appends one real message to the active thread and persists it
    immediately -- role is "founder" or "silent", matching the app's own
    existing bubble roles exactly, so no translation layer is needed
    between the transcript UI and durable storage. status is
    "pending" | "complete" | "failed" | "timed_out" (see
    update_thread_message() for how a "pending" message becomes
    terminal).

    V10.3R4 real-incident fix (Founder-authorized 2026-08-26,
    DUPLICATE_MEDIA_TRANSACTION=FAIL investigation): message_id, when
    given a real client-generated id (see mrsGenerateLocalMessageId() /
    mrsAppendBubble() client-side), is honored verbatim instead of always
    minting a fresh server-side uuid4. Before this, the client's local
    placeholder id and the server's real id were ALWAYS different, and
    the client had to asynchronously rename its own local tracking to
    match once the POST resolved -- a real race window existed between
    "message durably exists server-side under its real id" and "the
    client's local id-map knows that id", during which a concurrent
    mrsRestoreActiveThread() (bootstrap-only, but real) could already see
    the message in the backend ledger yet fail to recognize it as
    already-rendered locally, producing a genuine duplicate DOM bubble --
    confirmed by a real regression test reproducing this exact window.
    Honoring the client's id end-to-end removes the rename step (and the
    race it created) entirely. Bounded/validated (not blindly trusted as
    a free-form string) since it's client-supplied input reaching a
    durable file; an invalid/missing id falls back to a fresh uuid4,
    exactly like before this fix."""
    ctx = _load_context()
    thread = ctx.get(_ACTIVE_THREAD_KEY)
    if not isinstance(thread, dict) or "conversation_id" not in thread:
        thread = _new_thread()
    real_message_id = message_id if message_id and _CLIENT_MESSAGE_ID_RE.match(message_id) else uuid.uuid4().hex

    # V10.5 real idempotency fix (Founder-authorized 2026-08-26,
    # IMAGE_MEDIA_IDEMPOTENCY=FAIL / DUPLICATE_ROOT_CAUSE directive): this
    # function had NO deduplication by message_id at all — it always
    # appended. The R5 media-transaction claim/release guard (see
    # claim_media_transaction() below) only ever prevented a NEW
    # transaction from starting while another was in flight; it never
    # protected this function itself. Any code path that calls
    # append_thread_message() twice with the SAME client-generated id —
    # confirmed real gap: the Share-target flow (mrsHandleIncomingShare())
    # had no transaction id or claim/release guard of its own at all, and
    # Android is a documented source of duplicate native-callback/intent
    # delivery for a single real user action — durably created TWO
    # messages sharing one id, which is exactly what a repeated
    # "[Image attached]"/"Analyzing image..." row actually is once
    # persisted. Making THIS function itself idempotent closes the class
    # of bug for every current and future caller, not just the one path
    # already covered by the transaction claim: a duplicate call with an
    # id already present in the active thread returns the EXISTING
    # message, unchanged, rather than creating a second one.
    for existing in thread.get("messages", []):
        if existing.get("message_id") == real_message_id:
            return existing

    message = {
        "message_id": real_message_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "text": text,
        "attachment_type": attachment_type,
        "status": status,
    }
    thread["messages"].append(message)
    if len(thread["messages"]) > MAX_THREAD_MESSAGES:
        thread["messages"] = thread["messages"][-MAX_THREAD_MESSAGES:]
    ctx[_ACTIVE_THREAD_KEY] = thread
    _save_context(ctx)
    return message


def update_thread_message(message_id: str, *, text: str | None = None, status: str | None = None) -> bool:
    """Updates a real, already-persisted message in place -- the durable
    backing for the app's own "pending -> complete/failed/timed_out"
    bubble update (a real analysis job started, then finished or failed).
    Returns False (never raises) if the message_id genuinely isn't in the
    active thread -- a real, honest signal the caller can log rather than
    a silent no-op or a crash."""
    ctx = _load_context()
    thread = ctx.get(_ACTIVE_THREAD_KEY)
    if not isinstance(thread, dict):
        return False
    for message in thread.get("messages", []):
        if message.get("message_id") == message_id:
            if text is not None:
                message["text"] = text
            if status is not None:
                message["status"] = status
            ctx[_ACTIVE_THREAD_KEY] = thread
            _save_context(ctx)
            return True
    return False


def start_new_thread() -> dict[str, Any]:
    """Founder-explicit "new chat" -- archives (never deletes) the prior
    thread if it has any real messages, bounded to MAX_ARCHIVED_THREADS,
    then starts a genuinely fresh active thread."""
    ctx = _load_context()
    old = ctx.get(_ACTIVE_THREAD_KEY)
    if isinstance(old, dict) and old.get("messages"):
        archive = ctx.get(_ARCHIVED_THREADS_KEY, [])
        if not isinstance(archive, list):
            archive = []
        archive.append(old)
        ctx[_ARCHIVED_THREADS_KEY] = archive[-MAX_ARCHIVED_THREADS:]
    new_thread = _new_thread()
    ctx[_ACTIVE_THREAD_KEY] = new_thread
    _save_context(ctx)
    return new_thread


# V10.3R5 real-incident fix (Founder-authorized 2026-08-26,
# MEDIA_SINGLE_FLIGHT_PHYSICAL=FAIL): a real physical S25 recording
# proved two genuinely separate backend analysis jobs ran for one
# Founder image-send, roughly 38 seconds apart (real job records:
# d4ad2b81.../cbb347a3... then 974e5ee2.../f8cc1ac4..., started at
# 12:55:31 and 12:56:09 respectively) -- a client-side, in-memory-only
# guard (V10.3R4's mrsMediaTransactionInFlight) cannot survive whatever
# caused that gap (a WebView/Activity/JS-context reset is the leading
# real-world candidate on Android, though the exact trigger was not
# reproducible from static source review alone). The Founder's own
# explicit requirement: "Backend must also be idempotent: duplicate
# POST/retry with the same submission id must return the existing
# transaction, not create a second job/message." This is that durable,
# server-enforced claim -- survives any client-side state loss, because
# it lives in the SAME durable conversation_context.json every other
# real conversation-persistence function here already uses.
_ACTIVE_MEDIA_TRANSACTION_KEY = "active_media_transaction"
# Generously above the client's own MRS_MEDIA_ANALYSIS_TIMEOUT_MS
# (200000ms) so a genuinely still-running analysis is never treated as
# abandoned; bounds how long a truly abandoned claim (e.g. the app was
# killed mid-analysis and never called release) can block a real new
# attachment send.
_MEDIA_TRANSACTION_STALE_AFTER_S = 240


def claim_media_transaction(transaction_id: str, *, kind: str) -> dict[str, Any]:
    """Atomically claims the one-at-a-time media-analysis slot for
    transaction_id. Returns {"claimed": True, "transaction_id": ...} on
    success -- including when the SAME transaction_id claims again (a
    genuine retry: idempotent, never a second job). Returns
    {"claimed": False, "existing_transaction_id": ...} if a DIFFERENT
    transaction is still genuinely in flight and not yet stale."""
    if not transaction_id or not _CLIENT_MESSAGE_ID_RE.match(transaction_id):
        raise ValueError(f"invalid transaction_id: {transaction_id!r}")
    ctx = _load_context()
    existing = ctx.get(_ACTIVE_MEDIA_TRANSACTION_KEY)
    if isinstance(existing, dict) and existing.get("status") == "in_flight":
        if existing.get("transaction_id") == transaction_id:
            return {"claimed": True, "transaction_id": transaction_id}
        stale = True
        claimed_at = existing.get("claimed_at")
        if claimed_at:
            try:
                age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(claimed_at)).total_seconds()
                stale = age_s > _MEDIA_TRANSACTION_STALE_AFTER_S
            except ValueError:
                stale = True
        if not stale:
            return {"claimed": False, "existing_transaction_id": existing.get("transaction_id")}
    ctx[_ACTIVE_MEDIA_TRANSACTION_KEY] = {
        "transaction_id": transaction_id, "kind": kind,
        "status": "in_flight", "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_context(ctx)
    return {"claimed": True, "transaction_id": transaction_id}


def release_media_transaction(transaction_id: str, *, status: str) -> bool:
    """Marks transaction_id terminal (status: complete|failed|timed_out),
    freeing the slot for a genuinely new attachment. False if
    transaction_id doesn't match the currently-claimed one (a stale/
    already-superseded release -- never lets an old transaction's release
    clobber a newer, different one's claim)."""
    ctx = _load_context()
    existing = ctx.get(_ACTIVE_MEDIA_TRANSACTION_KEY)
    if not isinstance(existing, dict) or existing.get("transaction_id") != transaction_id:
        return False
    existing["status"] = status
    existing["released_at"] = datetime.now(timezone.utc).isoformat()
    ctx[_ACTIVE_MEDIA_TRANSACTION_KEY] = existing
    _save_context(ctx)
    return True


def _gather_studio_evidence() -> dict[str, Any]:
    """Real, bounded evidence snapshot for the natural-language fallback —
    reuses the EXACT same real data sources handle_intent()'s own
    deterministic branches already call (autonomous_cycle, evolution.
    proposal lessons, evolution.founder_request, campaign, organ_discovery),
    PLUS (2026-09-08 Domain G connection) this campaign's own studio_
    status.py aggregator and mission.py's Mission/WorkGraph state — real
    sources that existed but were never wired into this conversational
    layer. No new data source invented, no fabrication; this is the SAME
    evidence a deterministic query would have returned, gathered broadly
    enough for a model to reason across several of them at once.

    Defect fixed here: pending_founder_requests previously came straight
    from founder_request.list_pending_founder_requests() — the SAME raw,
    unfiltered list whose Telegram-push equivalent leaked test/synthetic
    campaigns into the real Founder channel (fixed separately, same
    campaign). This function had the identical bug in the conversational
    answer path: a Founder asking "what needs me?" could have been told
    about test fixtures. Now sourced from approval_backlog_hygiene's real,
    tested classification instead — the same fix, applied to the second
    place it was needed.

    WorkGraph state is summarized (counts by state), never dumped item-by-
    item — studio_status.collect() already does this bounding; this
    function does not re-derive or duplicate that logic."""
    import autonomous_cycle
    import campaign as campaign_mod
    import mission
    import organ_discovery
    import approval_backlog_hygiene
    import studio_status as studio_status_mod

    history = autonomous_cycle.cycle_history_summary(limit=10)
    latest = autonomous_cycle.latest_cycle()
    active_missions = [c for c in campaign_mod.list_all() if c.status in (campaign_mod.STATUS_ACTIVE, campaign_mod.STATUS_PAUSED)]

    # Real, filtered pending approvals (test/synthetic noise excluded) —
    # see defect note above. capability_needed=="production_promotion" is
    # always a genuine Founder gate; everything else real-and-pending
    # still needs a human/operator judgment call, not autonomous action.
    approval_report = approval_backlog_hygiene.backlog_report()
    real_pending = [
        c for c in approval_backlog_hygiene.classify_all()
        if c.category in (approval_backlog_hygiene.Category.TRUE_FOUNDER_GATE,
                          approval_backlog_hygiene.Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT)
    ]

    lessons: list[str] = []
    from evolution import proposal as proposal_mod
    if proposal_mod.LESSONS_PATH.exists():
        for line in proposal_mod.LESSONS_PATH.read_text().splitlines()[-8:]:
            try:
                lesson = json.loads(line).get("lesson")
            except json.JSONDecodeError:
                continue
            if lesson:
                lessons.append(lesson)

    health = organ_discovery.studio_health_summary()

    # mission.py's own real Missions (distinct from campaign_mod's
    # "active_missions" above — two real, currently-separate tracking
    # systems; both are real, neither fabricated, see this session's own
    # census notes) — top-priority few, not a full dump.
    real_missions = sorted(mission.list_all(), key=lambda m: -m.priority)[:8]
    status = studio_status_mod.collect()

    # DOMAIN_G_COVERAGE_AUDIT (2026-09-08): a real Founder question this
    # evidence bundle previously had no field for — "what did you repair?"
    # and "what are you doing next?" — both answerable from the SAME
    # latest real cycle record every other field here already reads, no
    # new source. reconciled_canary/reconciled_terminal/repair_retried are
    # real, already-tested workgraph_scheduler_phase counters (see
    # proposal_work_bridge.py and work_graph.py); RUNNABLE is the real
    # WorkGraph count of what the next natural cycle will actually pick up
    # — never a guess or a plan this function invents itself.
    wg_phase = (latest.workgraph_scheduler_phase or {}) if latest else {}
    wg_status = wg_phase.get("work_graph_status", {}) or {}
    repairs_this_cycle = {
        "canary_reconciled": wg_phase.get("reconciled_canary", 0),
        "terminal_state_reconciled": wg_phase.get("reconciled_terminal", 0),
        "repair_retried": len(wg_phase.get("repair_retried", []) or []),
    }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        # pending_founder_requests deliberately listed FIRST — it's the
        # authoritative answer for "anything need me?"/approval-class
        # questions, and earlier context is weighted more reliably by the
        # local model than a field buried lower in the evidence bundle.
        "pending_founder_requests": [
            {"subject": c.reason, "category": c.category.value, "job_id": c.job_id}
            for c in real_pending
        ],
        "pending_founder_requests_raw_count_excluding_test_noise": f"{approval_report.pending_real} real "
            f"of {approval_report.pending_total} raw pending ({approval_report.synthetic_noise} were test/synthetic)",
        "recent_autonomous_cycle_history": history,
        "latest_cycle_status": latest.final_status if latest else "no cycles recorded yet",
        "latest_cycle_started_at": latest.started_at if latest else None,
        "real_missions": [
            {"mission_id": m.mission_id, "goal": m.goal, "state": m.state, "priority": m.priority}
            for m in real_missions
        ],
        "real_workitem_summary": status.workitems_by_state,
        "real_workitem_total": status.workitems_total,
        "real_recent_failures_last_24h": status.anomalies_new_failure,
        "real_validation_failures": status.validation_failures_real,
        "real_canary_failures": status.canary_failures_real,
        "real_maintenance_findings": status.maintenance_findings_active,
        "unclaimed_registered_divisions": status.registry_candidates_unclaimed,
        "active_missions": [
            {"objective": c.objective, "status": c.status, "priority": c.priority} for c in active_missions
        ],
        "recent_lessons_learned": lessons,
        "studio_health_by_engine_and_provider": health.get("entries", []),
        "real_repairs_last_cycle": repairs_this_cycle,
        "runnable_now_next_natural_cycle_will_pick_up": wg_status.get("RUNNABLE", 0),
    }


def _natural_language_fallback(text: str, *, requested_by: str) -> ResolvedIntent:
    """Reached ONLY when resolve_intent() found no deterministic pattern
    match. Gathers real evidence, then reasons over it with the SAME
    local_model_bridge.submit_job() primitive local_video_bridge.py already
    uses for its own reasoning pass — no new model integration. This call
    is granted NO tools (requested_tools=set(), enforced inside submit_job
    itself) — it can only read the evidence given and return text; it can
    never take any action, so it cannot become an authority bypass no
    matter what the Founder or the model says.

    Explicitly grounded, never fabricated: the prompt instructs the model
    to answer ONLY from the real evidence and to say so honestly when the
    evidence doesn't cover the question, rather than inventing Studio
    facts — this is a prompt-level instruction, not a technical guarantee,
    so format_response() also labels this intent's answers distinctly from
    the deterministic ones for Founder transparency."""
    resolved = ResolvedIntent(intent=INTENT_NATURAL_LANGUAGE, raw_text=text)

    # Scorpio's Corner is deliberately isolated from MR. SILENT's own
    # monitoring (Founder law, unchanged) — studio_health_summary() has
    # NO real entry for it by design, and echoing "Scorpio" into a model
    # prompt would also hit GATED_KEYWORDS inside submit_job(). The
    # honest answer needs no model call at all: there genuinely is no
    # evidence to reason over.
    if _SCORPIO_RE.search(text):
        resolved.answer = {"note": "scorpio_isolation"}
        resolved.clarification_needed = (
            "Scorpio's Corner is intentionally isolated from my own monitoring — I don't have real evidence "
            "about its internal state to share, by design. I can tell you about MR. SILENT's own Studio status "
            "if that helps."
        )
        return resolved

    # Real S25 Ultra defect (2026-08-24): this used to inject whatever was
    # stored, unconditionally and with no age check — so a plain "Hello mr
    # silent" could surface an old engineering test turn or a stale camera/
    # vision answer as if it were part of the current exchange. Now bounded
    # to real, recent turns only, and honestly labeled by source (never
    # blended into one undifferentiated "earlier in this conversation") —
    # see _NL_CONTEXT_KEY_AT/_NL_CONTEXT_KEY_SOURCE.
    ctx = _load_context()
    history_block = ""
    stored_at = ctx.get(_NL_CONTEXT_KEY_AT)
    age_s = None
    if stored_at:
        try:
            t = datetime.fromisoformat(stored_at)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - t).total_seconds()
        except ValueError:
            age_s = None
    if (ctx.get(_NL_CONTEXT_KEY_QUESTION) and ctx.get(_NL_CONTEXT_KEY_ANSWER)
            and age_s is not None and age_s <= _NL_CONTEXT_STALE_AFTER_S):
        source = ctx.get(_NL_CONTEXT_KEY_SOURCE, "")
        label = "a moment ago, from your camera/vision question" if source == "camera_vision" else "earlier in this same conversation"
        history_block = (
            f"\nContext from {label}:\nFounder: {ctx[_NL_CONTEXT_KEY_QUESTION]}\n"
            f"You: {ctx[_NL_CONTEXT_KEY_ANSWER]}\n"
        )

    evidence = _gather_studio_evidence()
    prompt = (
        "You are MR. SILENT, TH3S1L3NTK1D Studios' autonomous engineering assistant, speaking directly with "
        "the Founder in an ordinary conversation. Answer using ONLY the real evidence below — if it doesn't "
        "contain what's needed, say so honestly instead of guessing or inventing Studio facts. If the Founder "
        "asks about approvals, waiting, or 'anything need me', prioritize pending_founder_requests above the "
        "other evidence sections — that field is the authoritative answer to that class of question.\n\n"
        f"REAL STUDIO EVIDENCE (as of {evidence['as_of']}):\n{json.dumps(evidence, indent=2, default=str)}\n"
        f"{history_block}\n"
        f"Founder: {text}\n\n"
        "Respond in 1-4 short, natural sentences, like a knowledgeable colleague — not a raw data dump. If the "
        "question is genuinely ambiguous, ask exactly ONE clear clarifying question instead of guessing."
    )

    import local_model_bridge
    try:
        job = local_model_bridge.submit_job(prompt, requested_by=requested_by, timeout_s=_REASONING_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — a reasoning failure must fall back honestly, never crash the conversation
        resolved.clarification_needed = f"I couldn't reason about that right now ({e!r}) — try rephrasing, or ask something more specific."
        return resolved

    if job.status != "succeeded" or not job.response_excerpt:
        reason = {
            "local_model_unavailable": "my local reasoning model isn't reachable right now",
            "rejected_policy": "that touches a topic I need explicit Founder approval to reason about",
        }.get(job.status, f"the reasoning attempt didn't complete cleanly (status={job.status})")
        resolved.clarification_needed = f"I couldn't answer that just now — {reason}. Try rephrasing, or ask a more specific question."
        return resolved

    answer_text = job.response_excerpt.strip()
    # Real structural diagnostics (Founder-authorized 2026-08-24, App
    # Convergence repair slice): safe (no secrets, no raw payload content
    # beyond what's already in `evidence`/`grounded_in`) trace of exactly
    # what context sources fed this one answer -- so a future contamination
    # report can be diagnosed from ONE /chat response instead of a manual,
    # multi-round code trace. There is genuinely no session/device
    # identifier anywhere in this request path (live_sensor_api.py's /chat
    # passes a fixed literal requested_by="mrsilent_app" for every caller)
    # -- session_id is honestly null, not fabricated, until real per-device
    # session identity exists (out of scope for this repair slice, which
    # fixes the actual reported contamination without inventing a new
    # identity/session mechanism).
    _TEST_MARKERS = ("test", "synthetic")
    engineering_test_context_included = (
        any(any(m in (mm.get("objective") or "").lower() for m in _TEST_MARKERS) for mm in evidence.get("active_missions", []))
        or any(any(m in (lesson or "").lower() for m in _TEST_MARKERS) for lesson in evidence.get("recent_lessons_learned", []))
    )
    resolved.answer = {
        "response_text": answer_text, "grounded_in": list(evidence.keys()),
        "context_trace": {
            "session_id": None,  # honest gap: no per-device/session identifier exists in this request path yet
            # This module keeps a single-slot "last turn" cache, never a
            # full transcript log (see _NL_CONTEXT_KEY_QUESTION/_ANSWER) --
            # so the real, honest count is 0 or 1, not a longer history size.
            "prior_message_count": 1 if history_block else 0,
            "prior_message_included": bool(history_block),
            "prior_message_age_s": round(age_s) if (history_block and age_s is not None) else None,
            "prior_message_source": ctx.get(_NL_CONTEXT_KEY_SOURCE) if history_block else None,
            "sensor_context_included": bool(history_block) and ctx.get(_NL_CONTEXT_KEY_SOURCE) == "camera_vision",
            "campaign_context_included": bool(evidence.get("active_missions")),
            "engineering_test_context_included": engineering_test_context_included,
            "persisted_history_included": bool(history_block),
            "context_order": ["real_studio_evidence_bundle", "prior_turn_history_if_recent", "founder_message"],
        },
    }
    ctx[_NL_CONTEXT_KEY_QUESTION] = text
    ctx[_NL_CONTEXT_KEY_ANSWER] = answer_text
    ctx[_NL_CONTEXT_KEY_AT] = datetime.now(timezone.utc).isoformat()
    ctx[_NL_CONTEXT_KEY_SOURCE] = "text_conversation"
    _save_context(ctx)
    return resolved


def combined_pending() -> tuple[list[dict[str, Any]], bool, str | None]:
    """Merges Render's own real escalations with Pulse's real authoritative
    unified pending inbox into ONE Founder-facing list (App/Command
    Orchestration milestone, Phase 5, Founder-authorized 2026-08-20) --
    MR_SILENT_LOGICAL_ENTITY_COUNT=1. Each item keeps its real
    source_authority so a decision routes back to whichever system
    actually owns it; neither source is copied into the other. Returns
    (items, pulse_reachable, pulse_error) -- Pulse being unreachable is
    NEVER silently treated as 'nothing pending there', so a Founder query
    can honestly say so rather than under-report."""
    from evolution import founder_request
    render_items = []
    for r in founder_request.list_pending_founder_requests():
        item = dict(r)
        item["source_authority"] = "render"
        item["approval_id"] = r["escalation_id"]
        render_items.append(item)
    try:
        from evolution import pulse_bridge
        pulse_items = pulse_bridge.list_pulse_pending()
        # Third source (Founder Notification Delivery Certification,
        # 2026-09-03): Pulse's OWN evolution/founder_request.py escalations
        # -- the real store evolution/advance.py's proposal-eligibility
        # gate reads -- had NO Founder-facing surface anywhere until now
        # (its own notification_note said so). Best-effort within the same
        # try as pulse_items: if Pulse is reachable for one federated call
        # it is reachable for both, and a failure here must not silently
        # drop the (already-working) pulse_items list.
        try:
            pulse_escalation_items = pulse_bridge.list_pulse_escalations()
        except Exception:  # noqa: BLE001 — this specific source degrading must not blank the rest of an otherwise-working merged list
            pulse_escalation_items = []
        return render_items + pulse_items + pulse_escalation_items, True, None
    except Exception as e:  # noqa: BLE001 — Pulse reachability is best-effort; Render's own list must never be lost because of it
        return render_items, False, str(e)


def resolve_any_decision(approval_id: str, decision_word: str, *, note: str = "") -> dict[str, Any]:
    """Explicit-ID decision routing (App/Command Orchestration milestone,
    Phase 5) for callers that already have an unambiguous target — e.g. an
    app button tap on a specific card — never guessing, same as the
    conversational APPROVE/DENY path above. decision_word: 'approve' |
    'deny'. Routes by the id's own real shape: Render's escalation_id is
    always a 16-hex-char fingerprint (see evolution/founder_request.py);
    anything else is a Pulse approval_id (TAG-id, e.g. 'OS-...'). Never a
    second approval database — calls the exact same real resolvers the
    merged pending list and the conversational path already use."""
    if decision_word not in ("approve", "deny"):
        raise ValueError(f"decision_word must be 'approve' or 'deny', got {decision_word!r}")
    from evolution import pulse_bridge
    if approval_id.startswith(pulse_bridge.PULSE_ESCALATION_ID_PREFIX):
        record = pulse_bridge.resolve_pulse_escalation(approval_id, decision_word, note=note)
        return {"resolved": record, "source_authority": "pulse_escalation"}
    is_render_id = bool(re.fullmatch(r"[0-9a-f]{16}", approval_id))
    if is_render_id:
        from evolution import founder_request
        decision = "approved" if decision_word == "approve" else "denied"
        record = founder_request.resolve_founder_decision(approval_id, decision, note=note)
        return {"resolved": record, "source_authority": "render"}
    record = pulse_bridge.resolve_pulse_decision(approval_id, decision_word)
    return {"resolved": record, "source_authority": "pulse"}


def handle_intent(text: str, *, requested_by: str = "founder") -> ResolvedIntent:
    """Resolves AND executes read-only intents against real data; for
    APPROVE/DENY, only executes if the target is genuinely unambiguous
    (exactly one pending Founder request) — otherwise returns a
    clarification question instead of guessing. Never weakens any existing
    gate: approval here calls the SAME founder_request.resolve_
    founder_decision() the explicit CLI path already uses."""
    resolved = resolve_intent(text)

    if resolved.intent == INTENT_GREETING:
        # Deliberately does nothing else — no evidence gathering, no
        # context read, no LLM call. See INTENT_GREETING's own comment.
        resolved.answer = {}

    elif resolved.intent == INTENT_QUERY_RECENT_WORK:
        import autonomous_cycle
        history = autonomous_cycle.cycle_history_summary(limit=10)
        latest = autonomous_cycle.latest_cycle()
        resolved.answer = {
            "recent_cycles": history,
            "latest_cycle_status": latest.final_status if latest else "no cycles recorded yet",
        }
        # Real S25 Ultra defect (2026-08-24): "what are you working on"
        # only ever answered from raw cycle statistics ("10 active work, 20
        # recovery actions") — never the actual campaign/objective/step a
        # Founder is really asking about. autonomy_status_snapshot.py (run
        # on Pulse, the canonical authority) already computes exactly that
        # detail for the app's own Mission sheet (/mission route) — reused
        # here verbatim, never a second/duplicate computation, so a
        # conversational answer and the Mission sheet can never disagree.
        # Best-effort: if Pulse is unreachable, the answer below still
        # returns Render's own real cycle stats rather than failing outright.
        try:
            from evolution import pulse_bridge
            resolved.answer["pulse_autonomy_status"] = pulse_bridge.get_pulse_autonomy_status()
        except Exception as e:  # noqa: BLE001 — Pulse reachability is best-effort; the Render-local stats above must still answer
            resolved.answer["pulse_unreachable"] = str(e)
        # Approval-count consistency fix (real S25 Ultra defect, 2026-08-24):
        # pulse_autonomy_status's own founder_approvals_pending_count is
        # Pulse's RAW, unfiltered internal count (confirmed live: 102) --
        # NOT the same "Normal" (real, actionable, noise/history-excluded)
        # count the Home heartbeat pill and the Approvals sheet both already
        # show (confirmed live: 48, via combined_pending() + founder_view_
        # filter.split_normal_and_history(), the SAME source /studio-status
        # and /pending-requests use). A conversational answer quoting the
        # raw 102 while the UI shows 48 would be exactly the kind of
        # cross-surface inconsistency the Founder explicitly flagged --
        # format_response() below uses THIS field, never the raw Pulse one.
        from evolution import founder_view_filter
        raw_pending, pending_pulse_reachable, _ = combined_pending()
        normal_pending, _ = founder_view_filter.split_normal_and_history(raw_pending)
        resolved.answer["normal_pending_count"] = len(normal_pending)
        resolved.answer["normal_pending_reachable"] = pending_pulse_reachable

    elif resolved.intent == INTENT_QUERY_LEARNING:
        from evolution import proposal as proposal_mod
        lessons = []
        if proposal_mod.LESSONS_PATH.exists():
            import json
            for line in proposal_mod.LESSONS_PATH.read_text().splitlines()[-10:]:
                try:
                    lessons.append(json.loads(line).get("lesson"))
                except json.JSONDecodeError:
                    continue
        resolved.answer = {"recent_lessons": lessons}

    elif resolved.intent == INTENT_QUERY_PENDING_REQUESTS:
        # Normal/History split (real S25 Ultra device finding, 2026-08-20):
        # matches the app's own Approvals panel exactly -- "what needs me?"
        # and the panel must never disagree. Real device evidence: the raw
        # federated total reached 52 items, ~12 of them this project's own
        # test artifacts; a Founder should never have to parse those to
        # answer "what needs me?".
        from evolution import founder_view_filter
        raw_pending, pulse_reachable, pulse_error = combined_pending()
        pending, _ = founder_view_filter.split_normal_and_history(raw_pending)
        resolved.answer = {"pending_requests": pending, "pulse_reachable": pulse_reachable}
        if pulse_error:
            resolved.answer["pulse_error"] = pulse_error

    elif resolved.intent == INTENT_QUERY_EVIDENCE:
        # Federated 2026-08-20 (App/Command Orchestration Phase 2): covers
        # both real authorities. A specific ordinal/numeric reference
        # ("tell me about the second one", "details on 3") fetches that
        # one item's real detail -- Render items already carry full detail
        # in payload; Pulse items get a real, live detail lookup via the
        # same arc211 renderer bot.py's own /details command uses.
        from evolution import founder_view_filter
        raw_pending, pulse_reachable, pulse_error = combined_pending()
        pending, _ = founder_view_filter.split_normal_and_history(raw_pending)
        idx = None
        num_m = re.search(r"\b(\d+)\b", text)
        if num_m:
            idx = int(num_m.group(1)) - 1
        else:
            for word, ordinal_idx in _ORDINAL_WORDS.items():
                if word in text.lower():
                    idx = ordinal_idx
                    break
        if idx is not None and 0 <= idx < len(pending):
            item = pending[idx]
            if item.get("source_authority") == "pulse":
                from evolution import pulse_bridge
                try:
                    detail_text = pulse_bridge.get_pulse_detail(item["position"])
                    resolved.answer = {"detail": detail_text, "approval_id": item["approval_id"], "source_authority": "pulse"}
                except pulse_bridge.PulseUnreachableError as e:
                    resolved.answer = {"note": f"couldn't reach that item's detail just now ({e})"}
            else:
                resolved.answer = {"detail": item, "approval_id": item["approval_id"], "source_authority": "render"}
        else:
            resolved.answer = {"pending_requests_with_evidence": pending}
            if not pending:
                resolved.answer["note"] = "no pending Founder request to show evidence for right now"

    elif resolved.intent == INTENT_QUERY_ORGAN_HEALTH and resolved.target:
        import organ_discovery
        # Omni God Mode Phase 11 (2026-08-18): route through the unified
        # HEALTHY/DEGRADED/UNAVAILABLE/STALE/PARTIAL/UNKNOWN vocabulary
        # (studio_health_summary()) instead of only duty_check() — a
        # question like "health of Ollama" or "what's wrong with Provider
        # B" previously got NOTHING back (duty_check only covers the 4
        # job_ledger-tracked engines by name), even though real evidence
        # for those existed via provider_health()/circuit_status(); this
        # was a genuine, disclosed gap in the prior certification pass.
        resolved.answer = organ_discovery.studio_health_summary(resolved.target)

    elif resolved.intent in (INTENT_APPROVE, INTENT_DENY):
        # Normal/History split applies here too -- "approve that" must
        # resolve against the SAME real, actionable set the Founder was
        # just shown, never accidentally against a test artifact hidden
        # from the normal view.
        from evolution import founder_view_filter
        raw_pending, pulse_reachable, pulse_error = combined_pending()
        pending, _ = founder_view_filter.split_normal_and_history(raw_pending)
        if len(pending) == 1:
            item = pending[0]
            decision_word = "approve" if resolved.intent == INTENT_APPROVE else "deny"
            if item["source_authority"] == "render":
                from evolution import founder_request
                decision = "approved" if resolved.intent == INTENT_APPROVE else "denied"
                record = founder_request.resolve_founder_decision(
                    item["approval_id"], decision, note=f"resolved via natural-language conversation: {text!r}")
                resolved.answer = {"resolved": record, "source_authority": "render"}
            else:
                from evolution import pulse_bridge
                try:
                    record = pulse_bridge.resolve_pulse_decision(item["approval_id"], decision_word)
                    resolved.answer = {"resolved": record, "source_authority": "pulse"}
                except pulse_bridge.PulseUnreachableError as e:
                    resolved.ambiguous = True
                    resolved.clarification_needed = f"That item is owned by Pulse, but I couldn't reach it just now ({e}) — try again shortly."
        elif len(pending) == 0:
            resolved.ambiguous = True
            if pulse_reachable:
                resolved.clarification_needed = "There's nothing currently pending your approval — did you mean something else?"
            else:
                resolved.clarification_needed = (
                    f"Nothing pending on my own side, but I couldn't reach Pulse's queue just now ({pulse_error}) "
                    "— there may be items there I can't see right now."
                )
        else:
            resolved.ambiguous = True
            resolved.clarification_needed = (
                f"There are {len(pending)} pending requests — which one? "
                f"IDs: {[p['approval_id'] for p in pending]}"
            )

    elif resolved.intent == INTENT_MISSION_STATUS:
        # Normal/History split (real S25 Ultra device finding, 2026-08-20):
        # matches the app's own Projects panel exactly, so "what missions
        # are active" and the panel never disagree — One MR. SILENT.
        import campaign as campaign_mod
        from evolution import founder_view_filter
        normal_c, _ = founder_view_filter.split_campaigns_normal_and_history(campaign_mod.list_all())
        active = sorted((c for c in normal_c if c.status in (campaign_mod.STATUS_ACTIVE, campaign_mod.STATUS_PAUSED)),
                         key=lambda c: c.priority)
        _remember(campaign_ids=[c.campaign_id for c in active])
        resolved.answer = {"active_missions": [
            {"campaign_id": c.campaign_id, "objective": c.objective, "status": c.status,
             "priority": c.priority, "steps_completed": sum(1 for s in c.steps if s["status"] == "completed"),
             "steps_total": len(c.steps)} for c in active
        ]}

    elif resolved.intent == INTENT_MISSION_FINISHED:
        import campaign as campaign_mod
        from evolution import founder_view_filter
        normal_c, _ = founder_view_filter.split_campaigns_normal_and_history(campaign_mod.list_all())
        done = [c for c in normal_c if c.status == campaign_mod.STATUS_COMPLETED]
        # most-recently-finished first — list_all() itself sorts by
        # campaign_id (filename), not recency, so a plain [-10:] slice
        # would show an arbitrary alphabetical subset rather than the
        # actual most recent completions.
        done.sort(key=lambda c: c.history[-1]["at"] if c.history else c.created_at, reverse=True)
        resolved.answer = {"completed_missions": [{"campaign_id": c.campaign_id, "objective": c.objective,
                                                     "outcome": c.outcome} for c in done[:10]]}

    elif resolved.intent == INTENT_MISSION_FAILED:
        import campaign as campaign_mod
        from evolution import founder_view_filter
        normal_c, _ = founder_view_filter.split_campaigns_normal_and_history(campaign_mod.list_all())
        failed = [c for c in normal_c if c.status == campaign_mod.STATUS_ABANDONED]
        failed.sort(key=lambda c: c.history[-1]["at"] if c.history else c.created_at, reverse=True)
        resolved.answer = {"abandoned_missions": [{"campaign_id": c.campaign_id, "objective": c.objective,
                                                     "outcome": c.outcome} for c in failed[:10]]}

    elif resolved.intent == INTENT_MISSION_BLOCKED_REASON:
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        else:
            import campaign as campaign_mod
            c = campaign_mod.load(cid)
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "status": c.status, "sub_status": c.sub_status,
                                "learned_lessons": c.learned_lessons[-3:], "recent_history": c.history[-3:]}

    elif resolved.intent == INTENT_MISSION_PAUSE:
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        else:
            import campaign as campaign_mod
            c = campaign_mod.pause(cid, reason=f"Founder conversation: {text!r}")
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "status": c.status}

    elif resolved.intent == INTENT_MISSION_RESUME:
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        else:
            import campaign as campaign_mod
            c = campaign_mod.resume(cid, reason=f"Founder conversation: {text!r}")
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "status": c.status}

    elif resolved.intent == INTENT_MISSION_CANCEL:
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        else:
            import campaign as campaign_mod
            c = campaign_mod.abandon(cid, reason=f"Founder conversation: cancelled — {text!r}")
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "status": c.status}

    elif resolved.intent in (INTENT_MISSION_PRIORITY_UP, INTENT_MISSION_PRIORITY_DOWN):
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        else:
            import campaign as campaign_mod
            c = campaign_mod.load(cid)
            new_priority = 1 if resolved.intent == INTENT_MISSION_PRIORITY_UP else min(c.priority + 3, 10)
            campaign_mod.set_priority(cid, new_priority, reason=f"Founder conversation: {text!r}")
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "old_priority": c.priority, "new_priority": new_priority}

    elif resolved.intent == INTENT_MISSION_OBJECTIVE_CHANGE:
        cid, ambiguous, clarification = _resolve_mission_reference(text)
        m = re.search(r"change the objective to\s+(.+)", text, re.I)
        if ambiguous:
            resolved.ambiguous = True
            resolved.clarification_needed = clarification
        elif not m:
            resolved.ambiguous = True
            resolved.clarification_needed = "What should the new objective say? (e.g. \"change the objective to ...\")"
        else:
            import campaign as campaign_mod
            new_text = m.group(1).strip()
            c = campaign_mod.change_objective(cid, new_text, reason="Founder conversation")
            _remember(referenced=cid)
            resolved.target = cid
            resolved.answer = {"campaign_id": cid, "new_objective": c.objective,
                                "note": "wording updated; run replan-mission if the step plan itself needs to change"}

    elif resolved.intent == INTENT_MISSION_CREATE:
        m = _MISSION_CREATE_OBJECTIVE_RE.search(text)
        objective_text = m.group(1).strip() if m else ""
        if not objective_text:
            resolved.ambiguous = True
            resolved.clarification_needed = (
                "What's the objective? (e.g. \"new mission: add dark mode to the settings page\")"
            )
        else:
            from evolution import mission_decomposition
            result = mission_decomposition.decompose_objective(objective_text, requested_by=requested_by)
            if result["status"] == "created":
                resolved.target = result["campaign_id"]
                resolved.answer = {
                    "campaign_id": result["campaign_id"], "objective": objective_text,
                    "steps": result["steps"], "assumptions": result["assumptions"],
                }
            else:
                # needs_clarification / decomposition_failed — nothing was
                # created; the Founder needs to know that plainly rather
                # than a silent no-op.
                resolved.ambiguous = True
                resolved.clarification_needed = (
                    f"I couldn't start that as a mission — {result.get('reason', 'decomposition did not succeed')}. "
                    "Try rephrasing the objective, or be more specific."
                )

    elif resolved.intent == INTENT_HANDLE_EVERYTHING:
        resolved.answer = {"note": "acknowledged — MR. SILENT already only contacts you when a genuine authority "
                                    "gate, credential, or ambiguous decision requires it; no change needed to make that true"}

    elif resolved.intent == INTENT_PLANNING_NEXT:
        import campaign as campaign_mod
        active = [c for c in campaign_mod.list_all() if c.status == campaign_mod.STATUS_ACTIVE]
        next_steps = []
        for c in active:
            nxt = campaign_mod.next_planned_step(c)
            if nxt:
                next_steps.append({"campaign_id": c.campaign_id, "objective": c.objective, "next_step": nxt["description"]})
        resolved.answer = {"next_planned_work": next_steps}

    elif resolved.intent == INTENT_RETRY_ALTERNATIVE:
        resolved.answer = {"note": "acknowledged — this intent is recorded but requires a human/engineer to pick "
                                    "and dispatch the actual alternative approach; not auto-executed from conversation alone"}

    elif resolved.intent == INTENT_UNRECOGNIZED:
        # Real Founder-device finding (2026-08-19): ordinary conversational
        # phrasing that didn't match a deterministic pattern used to dump
        # the Founder straight to "use cli.py" — never acceptable for a
        # unified conversational interface. Falls through to real,
        # evidence-grounded reasoning instead of a canned refusal; see
        # _natural_language_fallback()'s own docstring for why this can
        # never become an authority bypass (no tools, read-only evidence).
        resolved = _natural_language_fallback(text, requested_by=requested_by)

    # Universal conversational memory (2026-08-19 real-device finding): a
    # DETERMINISTIC turn (e.g. "What are you working on?" -> QUERY_RECENT_
    # WORK) used to leave NO trace for the natural-language fallback to
    # find, so a follow-up like "Why?" had nothing to resolve against even
    # though the Founder clearly meant "why [the answer you just gave]".
    # _natural_language_fallback() already records its OWN turns (see
    # above) — this covers every OTHER intent so "why"/"it"/"that" can
    # resolve against the immediately preceding turn regardless of which
    # path answered it. Skipped for a turn that only asked a clarifying
    # question back (nothing was actually answered yet to refer to).
    # GREETING excluded (2026-08-24 real-device fix): a bare "hello" has no
    # real content worth remembering, and recording it here would let the
    # NEXT turn's NL fallback quote a greeting back as "earlier in this
    # conversation" — harmless in content but pointless, and keeping this
    # write path strictly to turns with real informational content is part
    # of the same fix that stops stale/irrelevant turns from being echoed.
    if resolved.intent not in (INTENT_NATURAL_LANGUAGE, INTENT_GREETING) and not resolved.clarification_needed:
        try:
            answer_text = format_response(resolved)
            ctx = _load_context()
            ctx[_NL_CONTEXT_KEY_QUESTION] = text
            ctx[_NL_CONTEXT_KEY_ANSWER] = answer_text
            ctx[_NL_CONTEXT_KEY_AT] = datetime.now(timezone.utc).isoformat()
            ctx[_NL_CONTEXT_KEY_SOURCE] = f"deterministic:{resolved.intent}"
            _save_context(ctx)
        except Exception:  # noqa: BLE001 — conversational memory is a convenience, never fatal to the actual answer
            pass

    return resolved


# Omni/God Mode Consolidation, P4 (Founder-authorized 2026-08-21): real
# gap found via a live /chat test ("what are you working on" literally
# answered "Right now: work_performed...") -- autonomous_cycle.py's own
# internal final_status enum (idle | work_performed | blocked | error |
# crashed, see its own module docstring) was leaking verbatim into Normal-
# mode Founder prose. This translates the SAME real, unmodified enum value
# into plain English at the presentation layer only -- the underlying
# state machine, its values, and every other consumer of final_status are
# untouched; Admin mode (P2) can still show the raw value if ever needed.
_CYCLE_STATUS_PLAIN = {
    "idle": "idle — nothing new to work on right now",
    "work_performed": "actively working",
    "blocked": "blocked, waiting on something",
    "error": "hit an error during the last cycle",
    "crashed": "recovering after a crash in the last cycle",
}
# Shorter form for the per-cycle-count breakdown ("10 active work cycles,
# 2 idle cycles, ...") where the full sentence-style phrase above would
# read awkwardly repeated across a comma list.
_CYCLE_STATUS_SHORT = {
    "idle": "idle",
    "work_performed": "active work",
    "blocked": "blocked",
    "error": "error",
    "crashed": "crash",
}


def _plain_cycle_status(raw: str) -> str:
    return _CYCLE_STATUS_PLAIN.get(raw, raw)


def _plain_cycle_status_short(raw: str) -> str:
    return _CYCLE_STATUS_SHORT.get(raw, raw.replace("_", " "))


def _shorten_founder_text(text: str, max_len: int = 100) -> str:
    """Real campaign objectives can run to hundreds of words (they often
    embed a full registry-division brief, verbatim). Founder-facing prose
    needs the gist, not the whole brief — cuts at the first sentence
    boundary within max_len when one exists, else a plain hard truncation.
    Never used for Admin mode, which may show the real, full text."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    first_sentence_end = text.find(". ")
    if 0 < first_sentence_end <= max_len:
        return text[:first_sentence_end + 1]
    # No early sentence boundary -- cut at the last real word boundary
    # before max_len rather than mid-word (a raw text[:max_len] slice can
    # land inside a word, e.g. "...Task-Scoping Fidelity" -> "...Pr…").
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


def _mission_primary_sentence(pulse_status: dict | None, normal_pending_n: int | None) -> str | None:
    """The one real 'what MR. SILENT is doing right now' sentence, shared
    by the conversational QUERY_RECENT_WORK answer and the app's own
    concise Home status line (see home_status_summary() below) -- ONE
    real source computed from Pulse's live autonomy snapshot, never two
    independently-maintained summaries that could quietly drift apart."""
    if not pulse_status:
        return None
    mission = pulse_status.get("mission")
    if mission:
        step_bits = []
        if mission.get("current_step_description"):
            step_bits.append(f"current step: {_shorten_founder_text(mission['current_step_description'])}")
        if mission.get("total_steps"):
            pct = f" ({mission['progress_percent']}%)" if mission.get("progress_percent") is not None else ""
            step_bits.append(f"{mission['completed_steps']}/{mission['total_steps']} steps done{pct}")
        if mission.get("current_worker"):
            step_bits.append(f"worker: {mission['current_worker']}")
        step_line = " — " + "; ".join(step_bits) if step_bits else ""
        primary = f"Working on: {_shorten_founder_text(mission['objective'])}{step_line}."
        if mission.get("founder_approval_required"):
            primary += " This needs your approval to continue."
        return primary

    # Idle: real last-completed-action + waiting reason + next cycle +
    # real approval count, matching the Founder's own example style ("I'm
    # between campaigns right now. My last completed action was X. The
    # next autonomous cycle is Y. You have N decisions waiting for
    # approval.") rather than leading with raw cycle statistics.
    primary = "I'm between campaigns right now."
    recently_completed = ((pulse_status.get("projects") or {}).get("recently_completed") or [])
    if recently_completed:
        last_action = _shorten_founder_text(recently_completed[0].get("objective", ""))
        trailing = "" if last_action.endswith(("…", ".", "!", "?")) else "."
        primary += f" My last completed action was: {last_action}{trailing}"
    waiting = pulse_status.get("waiting_reason")
    # Real bug found via regression test (2026-08-24): autonomy_status_
    # snapshot.py's fallback waiting_reason is sometimes the raw dev-facing
    # string "last cycle final_status=<enum>", not Founder prose (its
    # OTHER source, latest.next_recommended_action, already is real plain
    # English and is passed through unchanged). Translate the raw form the
    # same way cycle-status text already is, rather than leaking the
    # internal enum token.
    if waiting and waiting.startswith("last cycle final_status="):
        waiting = f"Last cycle: {_plain_cycle_status(waiting.split('=', 1)[1])}"
    if waiting:
        primary += f" {waiting}."
    next_cycle = pulse_status.get("next_autonomous_cycle")
    if next_cycle:
        try:
            when = datetime.fromisoformat(next_cycle).strftime("%H:%M UTC")
            primary += f" The next autonomous cycle is {when}."
        except ValueError:
            pass
    if normal_pending_n:
        primary += f" You have {normal_pending_n} decision(s) waiting for your approval."
    return primary


def home_status_summary(pulse_status: dict | None) -> str:
    """Concise (roughly 1-2 phone-width lines), persistent Home status
    line (Founder-authorized 2026-08-24 repair slice: "The Founder should
    be able to glance at Home and understand MR. SILENT in under 2
    seconds... do NOT dump 10-cycle aggregate statistics/recovery counts/
    long objective text onto Home automatically"). Trims _mission_primary_
    sentence()'s fuller conversational detail (step/worker/approval-note)
    down to just the headline fact real Home glancing needs; the SAME
    live Pulse autonomy snapshot the conversational answer and Mission
    sheet already use -- never a second/invented status source."""
    if not pulse_status:
        return "Studio status unavailable right now."
    mission = pulse_status.get("mission")
    if mission:
        return f"Working on: {_shorten_founder_text(mission['objective'], max_len=90)}"
    parts = ["Between campaigns."]
    recently_completed = ((pulse_status.get("projects") or {}).get("recently_completed") or [])
    if recently_completed:
        parts.append(f"Finished: {_shorten_founder_text(recently_completed[0].get('objective', ''), max_len=55)}")
    next_cycle = pulse_status.get("next_autonomous_cycle")
    if next_cycle:
        try:
            when = datetime.fromisoformat(next_cycle).strftime("%H:%M UTC")
            parts.append(f"Next cycle at {when}.")
        except ValueError:
            pass
    return " ".join(parts)


def format_response(resolved: ResolvedIntent) -> str:
    """GOAL 8 (Founder-authorized 2026-08-18): concise, human-readable
    prose over the structured answer — not raw JSON, not developer jargon,
    unless nothing else applies. The machine-readable ResolvedIntent
    (including .answer) remains fully intact underneath this — this
    function only changes what's DISPLAYED, never what's recorded."""
    if resolved.clarification_needed:
        return resolved.clarification_needed
    a = resolved.answer or {}

    if resolved.intent == INTENT_GREETING:
        return "Hello — I'm here and listening. What can I help with?"

    if resolved.intent == INTENT_NATURAL_LANGUAGE:
        # The model's own prose IS the answer — grounded in the real
        # evidence bundle assembled in _natural_language_fallback(), never
        # further reformatted here (that would risk quietly editorializing
        # a grounded answer).
        return a.get("response_text", "I'm not sure how to answer that.")

    if resolved.intent == INTENT_QUERY_RECENT_WORK:
        # Real gap found via the Founder's own real-device conversation
        # test (2026-08-19): this intent already gathered real evidence
        # (handle_intent(), above) but format_response() never had actual
        # prose for it. A SECOND real-device gap found 2026-08-24: even once
        # it had prose, the answer was pure cycle statistics ("10 active
        # work, 20 recovery actions") with no actual campaign/objective/
        # step/worker detail — not what a Founder means by "what are you
        # working on". The primary answer now leads with the real current
        # mission (or a real, specific idle/waiting reason) from Pulse's own
        # autonomy snapshot — the SAME data the app's Mission sheet already
        # shows, never invented here. Raw cycle stats are kept, but only as
        # a short trailing secondary sentence.
        # Priority order per the Founder's explicit spec (2026-08-24 repair
        # slice): current campaign/objective -> current step -> progress ->
        # current worker -> last meaningful action -> waiting/blocking
        # reason -> next autonomous cycle -> Founder approval required ->
        # secondary historical/cycle stats only if asked. Approval count
        # ALWAYS uses the corrected "Normal" count (a.get("normal_pending_
        # count")) -- see the note at its computation site above -- never
        # Pulse's raw internal snapshot number, so this answer can never
        # disagree with the Home pill or the Approvals sheet.
        pulse_status = a.get("pulse_autonomy_status")
        normal_pending_n = a.get("normal_pending_count")
        primary = _mission_primary_sentence(pulse_status, normal_pending_n)

        latest = a.get("latest_cycle_status", "no cycles recorded yet")
        latest_plain = _plain_cycle_status(latest)
        hist = a.get("recent_cycles") or {}
        count = hist.get("count", 0)
        if count == 0:
            secondary = "I don't have any cycle history yet to summarize."
        else:
            counts = hist.get("status_counts", {})
            parts = [f"{v} {_plain_cycle_status_short(k)}" for k, v in counts.items()]
            summary = f"Over the last {count} autonomous cycles: " + ", ".join(parts) + "."
            work_bits = []
            if hist.get("total_proposals_created"):
                work_bits.append(f"created {hist['total_proposals_created']} proposal(s)")
            if hist.get("total_promotion_candidates"):
                work_bits.append(f"built {hist['total_promotion_candidates']} real change(s) ready for your review")
            if hist.get("total_recovery_actions"):
                work_bits.append(f"handled {hist['total_recovery_actions']} recovery action(s)")
            work_line = (" I " + ", ".join(work_bits) + ".") if work_bits else ""
            secondary = f"{summary}{work_line}"

        if primary:
            return f"{primary} (Recent activity: {secondary})"
        return f"Right now: {latest_plain}. {secondary}"

    if resolved.intent == INTENT_MISSION_CREATE:
        steps = a.get("steps", 0)
        line = f"Started a new mission (campaign {a.get('campaign_id', '')[:8]}): {a.get('objective', '')} — {steps} step(s) planned."
        assumptions = a.get("assumptions") or []
        if assumptions:
            line += f" Note: {len(assumptions)} assumption(s) made during planning — ask \"what's blocking that\" if it stalls."
        return line

    if resolved.intent == INTENT_MISSION_STATUS:
        missions = a.get("active_missions", [])
        if not missions:
            return "Nothing active right now — I'm idle, waiting for new signals or a new objective from you."
        lines = [f"{i+1}. {m['objective']} — {m['status']}, {m['steps_completed']}/{m['steps_total']} steps done "
                 f"(priority {m['priority']})" for i, m in enumerate(missions)]
        return "Here's what's active:\n" + "\n".join(lines)

    if resolved.intent == INTENT_MISSION_PAUSE:
        return f"Paused. I'll leave it exactly where it is until you say resume."
    if resolved.intent == INTENT_MISSION_RESUME:
        return f"Resumed — it'll pick back up on the next autonomous cycle."
    if resolved.intent == INTENT_MISSION_CANCEL:
        return f"Cancelled. Any work already completed stays on record; nothing further will happen on it."
    if resolved.intent in (INTENT_MISSION_PRIORITY_UP, INTENT_MISSION_PRIORITY_DOWN):
        return f"Priority updated ({a.get('old_priority')} -> {a.get('new_priority')}, lower runs sooner)."
    if resolved.intent == INTENT_MISSION_OBJECTIVE_CHANGE:
        return f"Objective updated to: {a.get('new_objective')!r}. {a.get('note', '')}"
    if resolved.intent == INTENT_MISSION_BLOCKED_REASON:
        status = a.get("status")
        if status == "paused":
            lessons = a.get("learned_lessons") or []
            if lessons:
                return f"It's paused. {lessons[-1]}"
            # pause() records its reason in history (the "reason" kwarg),
            # not learned_lessons — check there too before giving up.
            history = a.get("recent_history") or []
            pause_events = [h for h in reversed(history) if h.get("event") == "paused" and h.get("reason")]
            if pause_events:
                return f"It's paused. {pause_events[0]['reason']}"
            return "It's paused, but no specific reason was recorded."
        return f"It's currently {status} — nothing is blocking it."
    if resolved.intent == INTENT_MISSION_FINISHED:
        done = a.get("completed_missions", [])
        return ("Nothing has finished yet." if not done else
                "Finished: " + "; ".join(m["objective"][:60] for m in done))
    if resolved.intent == INTENT_MISSION_FAILED:
        failed = a.get("abandoned_missions", [])
        return ("Nothing has failed." if not failed else
                "These didn't make it: " + "; ".join(m["objective"][:60] for m in failed))
    if resolved.intent == INTENT_PLANNING_NEXT:
        nxt = a.get("next_planned_work", [])
        return ("Nothing queued up right now." if not nxt else
                "Next up: " + "; ".join(f"{n['next_step']} (for {n['objective'][:40]})" for n in nxt))
    if resolved.intent in (INTENT_APPROVE, INTENT_DENY) and a.get("resolved"):
        return f"Got it — {a['resolved']['status']}. " + (
            "I'll continue automatically." if a['resolved']['status'] == "approved" else "I'll leave it paused.")
    if resolved.intent == INTENT_QUERY_PENDING_REQUESTS:
        # Federated 2026-08-20 (App/Command Orchestration Phase 2): normal
        # conversational phrasing never names an internal system/node —
        # matches the app's own Approvals card wording exactly, so the
        # Founder gets one consistent voice whether typing or tapping.
        pending = a.get("pending_requests", [])
        lines = []
        for i, p in enumerate(pending, start=1):
            if p.get("source_authority") == "pulse":
                lines.append(f"{i}. Pending review — risk {p.get('risk', 'unknown')} (ref: {p['approval_id']})")
            else:
                # Omni/God Mode Consolidation, P4 (2026-08-21): real gap
                # found via a live /chat test -- human_readable embeds
                # advance.py's raw recommended_action verbatim ("promote
                # job <uuid> (implemented via claude_code)"), leaking a raw
                # job ID and internal engine name into Normal-mode prose.
                # 'finding' is the SAME clean text the already-certified
                # Approvals UI card already shows (_approvalCardText() in
                # mrsilent_app.html) -- this makes the conversational
                # answer match the tappable card exactly, one consistent
                # voice, without inventing any new field or losing any
                # Founder-relevant substance (the Approve/Deny action
                # itself, not its internal mechanics, is what matters here).
                lines.append(f"{i}. {p['payload']['finding']}")
        prefix = "" if a.get("pulse_reachable", True) else "(note: some of my other resources aren't reachable right now — this list may be incomplete)\n\n"
        return prefix + ("Nothing needs you right now." if not lines else "\n\n".join(lines))
    if resolved.intent == INTENT_QUERY_EVIDENCE:
        if a.get("note"):
            return a["note"]
        if "detail" in a:
            if a.get("source_authority") == "pulse":
                return a["detail"]
            return a["detail"]["payload"]["human_readable"]
        pending = a.get("pending_requests_with_evidence", [])
        if not pending:
            return a.get("note", "Nothing needs your approval right now.")
        lines = []
        for i, p in enumerate(pending, start=1):
            if p.get("source_authority") == "pulse":
                lines.append(f"{i}. Pending review — risk {p.get('risk', 'unknown')} (ref: {p['approval_id']})")
            else:
                lines.append(f"{i}. {p['payload']['finding']}")  # P4: same fix as QUERY_PENDING_REQUESTS above
        return "\n\n".join(lines)
    if resolved.intent == INTENT_QUERY_ORGAN_HEALTH:
        # Omni God Mode Phase 11 (2026-08-18): this used to fall through to
        # the generic "(QUERY_ORGAN_HEALTH) — see the machine-readable
        # answer" stub — real prose over organ_discovery.
        # studio_health_summary()'s unified vocabulary now.
        if a.get("ambiguous"):
            names = ", ".join(m["name"] for m in a.get("matches", []))
            return f"That could mean a few things: {names}. Which one?"
        status, reason = a.get("status"), a.get("reason")
        if status is None:
            return f"I don't have a health signal for {resolved.target!r}."
        return f"{a.get('target', resolved.target)}: {status.title()} — {reason}"

    return f"({resolved.intent}) — see the machine-readable answer for full detail."
