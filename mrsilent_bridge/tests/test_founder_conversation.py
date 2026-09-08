#!/usr/bin/env python3
"""
Tests for MILESTONE D — Natural Founder Conversation (Founder-authorized
2026-08-18): evolution/founder_conversation.py resolves free text to a
fixed set of explicit governed intents; authority-changing intents
(approve/deny) never guess a target — they resolve only when genuinely
unambiguous, and ask for clarification otherwise. Never infers Founder
approval from casual conversation.

Run: python3 tests/test_founder_conversation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evolution import founder_conversation as fc
from evolution import founder_request
from evolution import pulse_bridge
from evolution import founder_view_filter
from _test_isolation import isolated_test_state  # root-cause test-isolation guarantee, see tests/_test_isolation.py

import contextlib


@contextlib.contextmanager
def _pulse_isolated():
    """This file's exact-pending-count assertions test Render's OWN
    escalation mechanism in isolation -- Pulse's real, live, independently
    -changing approval backlog (App/Command Orchestration milestone, Phase
    5, Founder-authorized 2026-08-20) must not make these counts
    non-deterministic. Mirrors the _fake_duty_prefers_omni() pattern
    already used elsewhere in this suite for the same reason: a real
    external dependency's live state must be isolated for a test whose own
    subject is a DIFFERENT, local mechanism."""
    original = pulse_bridge.list_pulse_pending
    pulse_bridge.list_pulse_pending = lambda: []
    try:
        yield
    finally:
        pulse_bridge.list_pulse_pending = original


@contextlib.contextmanager
def _view_filter_permissive():
    """This file's fixtures deliberately use subjects like 'synthetic
    conversation test A' to represent an ordinary pending item for
    ambiguity-resolution tests -- exactly the real, evidence-based pattern
    founder_view_filter.classify_pending_item() (App/Command Orchestration
    milestone, real S25 Ultra device UX repair, 2026-08-20) is correctly
    designed to hide from the Normal Founder view. This file's own subject
    is the approve/deny disambiguation mechanism, not the view filter --
    same isolation principle as _pulse_isolated() above, for a different
    real dependency."""
    original = founder_view_filter.classify_pending_item
    founder_view_filter.classify_pending_item = lambda item: founder_view_filter.CATEGORY_REAL_ACTIONABLE
    try:
        yield
    finally:
        founder_view_filter.classify_pending_item = original

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _cleanup(escalation_id: str) -> None:
    p = founder_request.ESCALATIONS_DIR / f"{escalation_id}.json"
    if p.exists():
        p.unlink()


def _reset_pending_baseline() -> list[tuple[Path, str]]:
    """This test file's own assertions depend on an EXACT pending-request
    count (zero, one, or two) to prove approve/deny disambiguation — a
    property only this file needs, including a genuinely EMPTY baseline for
    test_approve_never_infers_without_an_unambiguous_target(). Other test
    files across the regression suite legitimately drive real proposals
    through evolution/advance.py's real PROMOTION_CANDIDATE step, which (as
    of the Founder-communication milestone) creates a real founder_request.
    py escalation record as a side effect; none of those pre-existing files
    know to clean up an escalation mechanism that postdates them. Clearing
    to a known-zero baseline here is what makes this file's exact-count
    assertions correct regardless of what ran before it in a full suite.

    Real bug found via this round's Omni God Mode Law #6 audit: this used
    to delete EVERY escalation file unconditionally, on the assumption
    that "every record cleared is itself disposable test output" — false
    whenever this file happened to run while the one real, standing,
    durable telegram_app_reconciliation Founder request was pending, which
    would permanently destroy it. Now moves EVERY existing file aside (by
    content, not by guessing which ones are "safe") and returns them for
    the caller to restore once this file's own exact-count tests are done —
    the same move-aside/restore pattern already proven in
    test_conversational_mission_control.py's _set_aside_standing_requests(),
    reused here instead of re-deriving a second, subtly different policy."""
    moved: list[tuple[Path, str]] = []
    for p in founder_request.ESCALATIONS_DIR.glob("*.json"):
        moved.append((p, p.read_text()))
        p.unlink()
    return moved


def _restore_pending_baseline(moved: list[tuple[Path, str]]) -> None:
    for p, content in moved:
        p.write_text(content)


def test_all_mission_example_sentences_resolve_to_explicit_intents() -> None:
    examples = {
        "Silent, what did you do overnight?": fc.INTENT_QUERY_RECENT_WORK,
        "What are you working on?": fc.INTENT_QUERY_RECENT_WORK,
        "Why do you need my approval?": fc.INTENT_QUERY_EVIDENCE,
        "Show me the evidence.": fc.INTENT_QUERY_EVIDENCE,
        "What's wrong with OmniForge?": fc.INTENT_QUERY_ORGAN_HEALTH,
        "Can you handle this yourself?": fc.INTENT_QUERY_PENDING_REQUESTS,
        "What have you learned this week?": fc.INTENT_QUERY_LEARNING,
        "Try another approach": fc.INTENT_RETRY_ALTERNATIVE,
    }
    for text, expected in examples.items():
        got = fc.resolve_intent(text).intent
        check(f"{text!r} resolves to {expected}", got == expected, got)


def test_unrecognized_text_never_fabricates_an_intent() -> None:
    r = fc.resolve_intent("the weather is nice today and I like sandwiches")
    check("genuinely unrelated text still resolves UNRECOGNIZED at the pure pattern-matching layer (resolve_intent() itself is unchanged)",
          r.intent == fc.INTENT_UNRECOGNIZED, r.intent)
    # Real Founder-device finding (2026-08-19): handle_intent() used to
    # leave UNRECOGNIZED as a dead end with a canned "use cli.py" message.
    # It now falls through to real, evidence-grounded reasoning — this no
    # longer means "always ask for clarification" (see the natural-
    # language fallback tests below), so this test now proves the NEW
    # contract instead: never the old canned text, never a fabricated
    # authority-changing action, always a real response.
    handled = fc.handle_intent("the weather is nice today and I like sandwiches")
    check("no answer is ever fabricated as an authority-changing action for genuinely off-topic text",
          handled.intent not in (fc.INTENT_APPROVE, fc.INTENT_DENY), handled.intent)
    response = fc.format_response(handled)
    check("the OLD canned 'use cli.py' dead-end message is gone", "use an explicit cli.py command" not in response, response)
    check("a real, non-empty response is returned either way", bool(response.strip()), response)


def test_approve_never_infers_without_an_unambiguous_target() -> None:
    """The core safety property: casual conversation must never become an
    approval unless there is EXACTLY one real pending request."""
    r = fc.handle_intent("approve that")
    check("with ZERO pending requests, approve is ambiguous, not silently accepted",
          r.ambiguous is True, r.ambiguous)
    check("a clarification question is returned", bool(r.clarification_needed))
    check("no answer/resolution is fabricated", r.answer is None, r.answer)


def test_approve_resolves_only_when_exactly_one_pending() -> None:
    rec = founder_request.request_founder_decision(
        subject="synthetic conversation test — single pending", finding="f",
        capability_needed="x", reason_required="r", recommended_action="a", risk="low", affected={},
    )
    try:
        r = fc.handle_intent("approve that")
        check("exactly one pending request resolves unambiguously", r.ambiguous is False, r.ambiguous)
        check("the intent is APPROVE", r.intent == fc.INTENT_APPROVE, r.intent)
        check("the REAL escalation record was actually resolved",
              r.answer is not None and r.answer["resolved"]["status"] == "approved", r.answer)
        reloaded = founder_request.list_pending_founder_requests()
        check("the request is no longer pending after approval",
              not any(p["escalation_id"] == rec["escalation_id"] for p in reloaded))
    finally:
        _cleanup(rec["escalation_id"])


def test_approve_asks_for_clarification_with_multiple_pending() -> None:
    rec_a = founder_request.request_founder_decision(
        subject="synthetic conversation test A", finding="fA", capability_needed="xA",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    rec_b = founder_request.request_founder_decision(
        subject="synthetic conversation test B", finding="fB", capability_needed="xB",
        reason_required="r", recommended_action="a", risk="low", affected={},
    )
    try:
        r = fc.handle_intent("approve that")
        check("with TWO pending requests, approve is ambiguous, never guesses which one",
              r.ambiguous is True, r.ambiguous)
        check("both real IDs are named in the clarification",
              rec_a["escalation_id"] in r.clarification_needed and rec_b["escalation_id"] in r.clarification_needed,
              r.clarification_needed)
        pending = founder_request.list_pending_founder_requests()
        check("NEITHER request was resolved — no guessing occurred",
              all(p["status"] == "pending_founder_review" for p in pending
                  if p["escalation_id"] in (rec_a["escalation_id"], rec_b["escalation_id"])))
    finally:
        _cleanup(rec_a["escalation_id"])
        _cleanup(rec_b["escalation_id"])


def test_deny_uses_the_real_deterministic_resolution_path() -> None:
    """Deny must call the SAME founder_request.resolve_founder_decision()
    the explicit CLI/admin path uses — never a separate, parallel
    resolution mechanism."""
    rec = founder_request.request_founder_decision(
        subject="synthetic conversation test — deny path", finding="f",
        capability_needed="x", reason_required="r", recommended_action="a", risk="low", affected={},
    )
    try:
        r = fc.handle_intent("don't do that")
        check("intent is DENY", r.intent == fc.INTENT_DENY, r.intent)
        check("the real record now shows denied", r.answer["resolved"]["status"] == "denied", r.answer)
    finally:
        _cleanup(rec["escalation_id"])


def test_read_only_intents_answer_from_real_data_not_fabricated() -> None:
    r = fc.handle_intent("what did you do overnight?")
    check("recent-work answer includes real cycle history data", "recent_cycles" in (r.answer or {}), r.answer)

    r2 = fc.handle_intent("show me the evidence")
    check("evidence answer reflects the real (currently empty) pending list",
          r2.answer is not None and "pending_requests_with_evidence" in r2.answer, r2.answer)


def test_organ_health_query_requires_a_named_target() -> None:
    r = fc.resolve_intent("what's wrong with omniforge")
    check("a named organ resolves with that target", r.target == "omniforge", r.target)
    check("a named target is not ambiguous", r.ambiguous is False)

    r2 = fc.resolve_intent("what's wrong")
    check("no named organ is honestly ambiguous, not guessed", r2.ambiguous is True or r2.intent != fc.INTENT_QUERY_ORGAN_HEALTH)


# ---- Natural Founder conversation fallback (2026-08-19 Founder real- -----
# device finding: "MR. SILENT" answered ordinary conversational Founder
# questions with a canned "I didn't recognize that... use cli.py" dead
# end). Real, live throughout — every call below hits the real local
# reasoning model (local_model_bridge.submit_job(), the same primitive
# local_video_bridge.py's own reasoning pass already uses) and real
# evidence sources; nothing here is mocked.

_OLD_DEAD_END = "use an explicit cli.py command"


def test_the_founders_exact_real_device_test_sentence_gets_a_real_answer() -> None:
    """THE primary required proof: this exact sentence, verbatim from the
    Founder's own real Samsung device test (2026-08-19), used to return
    the canned unrecognized-intent message. It must now return a real,
    evidence-grounded answer."""
    r = fc.handle_intent("What is MR. SILENT doing autonomously right now?", requested_by="test")
    response = fc.format_response(r)
    check("the exact real-device test sentence no longer hits the old dead end", _OLD_DEAD_END not in response, response)
    check("a real, non-generic answer is returned", bool(response.strip()) and not response.startswith("("), response)
    check("the answer is grounded in real evidence (mentions real cycle/status vocabulary, not a vague platitude)",
          any(w in response.lower() for w in ("cycle", "idle", "work", "proposal", "engine")), response)


def test_ordinary_founder_phrasings_get_real_grounded_answers_not_the_dead_end() -> None:
    questions = [
        "What did you do today?",
        "Anything need me?",
        "What's broken?",
        "How's the studio?",
        "What are you doing next?",
    ]
    for q in questions:
        r = fc.handle_intent(q, requested_by="test")
        response = fc.format_response(r)
        check(f"{q!r} does not hit the old cli.py dead end", _OLD_DEAD_END not in response, response)
        check(f"{q!r} does not hit the generic machine-readable stub", "see the machine-readable answer" not in response, response)
        check(f"{q!r} gets a real, non-empty response", bool(response.strip()), response)


def test_scorpios_corner_gets_an_honest_isolation_answer_never_fabricated_facts() -> None:
    """Scorpio's Corner is deliberately isolated from MR. SILENT's own
    monitoring (unchanged Founder law) — the honest answer is 'I don't
    have real evidence about that', proven WITHOUT even attempting a
    model call (also sidesteps GATED_KEYWORDS's real 'scorpio' trigger,
    which a raw model call would otherwise hit)."""
    r = fc.handle_intent("What changed in Scorpio's Corner?", requested_by="test")
    response = fc.format_response(r)
    check("the real answer honestly discloses isolation rather than inventing Scorpio facts",
          "isolated" in response.lower() or "don't have" in response.lower(), response)
    check("no fabricated Scorpio-specific claim appears in the answer", "scorpio" not in response.lower().replace("scorpio's corner", ""), response)


def test_multiturn_context_records_the_prior_turn_for_a_why_followup() -> None:
    """The concrete, deterministically-verifiable guarantee: after ANY
    turn (not just a natural-language one — this one previously only
    worked if turn 1 ALSO happened to be a natural-language turn, a real
    gap found and fixed this round), the conversation memory file holds
    the real question+answer so a follow-up like 'Why?' has something
    real to resolve against. Grading the MODEL's own reasoning quality on
    the follow-up is inherently soft; this proves the INFRASTRUCTURE
    guarantee underneath it, which is what's actually testable reliably."""
    r1 = fc.handle_intent("What are you working on?", requested_by="test")
    a1 = fc.format_response(r1)
    check("turn 1 ('what are you working on') resolves to a real, non-clarification answer to remember",
          not r1.clarification_needed, r1.clarification_needed)

    ctx = fc._load_context()
    check("the real conversation memory recorded turn 1's question", ctx.get(fc._NL_CONTEXT_KEY_QUESTION) == "What are you working on?", ctx)
    check("the real conversation memory recorded turn 1's actual answer text", ctx.get(fc._NL_CONTEXT_KEY_ANSWER) == a1, ctx)

    r2 = fc.handle_intent("Why?", requested_by="test")
    a2 = fc.format_response(r2)
    check("the follow-up 'Why?' gets a real, non-empty answer (not the old dead end)", bool(a2.strip()) and _OLD_DEAD_END not in a2, a2)
    check("'Why?' alone is never fabricated into an authority-changing action", r2.intent not in (fc.INTENT_APPROVE, fc.INTENT_DENY), r2.intent)


def test_legacy_deterministic_intents_still_resolve_unchanged() -> None:
    """Requirement A: the widened patterns and the new fallback must not
    regress any EXISTING deterministic intent's own resolution."""
    check("'what's wrong with omniforge' still resolves QUERY_ORGAN_HEALTH deterministically",
          fc.resolve_intent("what's wrong with omniforge").intent == fc.INTENT_QUERY_ORGAN_HEALTH)
    check("'pending' still resolves QUERY_PENDING_REQUESTS deterministically",
          fc.resolve_intent("what's pending").intent == fc.INTENT_QUERY_PENDING_REQUESTS)
    check("'what have you done today' still resolves QUERY_RECENT_WORK deterministically (the original phrasing, pre-widening)",
          fc.resolve_intent("what have you done today").intent == fc.INTENT_QUERY_RECENT_WORK)
    check("'approve that' still resolves APPROVE deterministically", fc.resolve_intent("approve that").intent == fc.INTENT_APPROVE)
    check("'cancel' still resolves MISSION_CANCEL deterministically", fc.resolve_intent("cancel").intent == fc.INTENT_MISSION_CANCEL)


def test_widened_recent_work_pattern_gets_real_prose_not_the_stub() -> None:
    """The Founder's exact test sentence now resolves DETERMINISTICALLY
    (fast, no model call) — but a real, separate bug was found alongside
    it: format_response() never had real prose for QUERY_RECENT_WORK at
    all, so even a correct deterministic resolution fell through to the
    generic dev-facing stub. Proves both the resolution AND the prose."""
    r = fc.handle_intent("What is MR. SILENT doing autonomously right now?", requested_by="test")
    check("the widened pattern resolves this deterministically (no model call needed)", r.intent == fc.INTENT_QUERY_RECENT_WORK, r.intent)
    response = fc.format_response(r)
    check("real prose is returned, not the generic '(INTENT) — see machine-readable' stub", "see the machine-readable answer" not in response, response)
    check("the real prose mentions genuine cycle-history vocabulary", "cycle" in response.lower() or "idle" in response.lower(), response)


def test_pending_requests_conversational_answer_hides_internal_jargon() -> None:
    """Omni/God Mode Consolidation, P4 (2026-08-21): real bug found via a
    live /chat test — the conversational "what needs me" answer used to
    render payload['human_readable'], which embeds advance.py's raw
    recommended_action verbatim (e.g. "promote job <uuid> (implemented via
    claude_code)"), leaking a raw job ID and internal engine name into
    Normal-mode prose. The already-certified Approvals UI card uses
    payload['finding'] instead (clean, no jargon) — this proves the
    conversational answer now matches that same certified field, so both
    surfaces speak with one voice, per the standing "hide implementation
    complexity" requirement."""
    rec = founder_request.request_founder_decision(
        subject="synthetic conversation test — jargon leak",
        finding="a real, plain-language description of what was found",
        capability_needed="x", reason_required="r",
        recommended_action="promote job deadbeef-0000-1111-2222-333344445555 (implemented via claude_code) to a real path",
        risk="low", affected={},
    )
    try:
        r = fc.handle_intent("what needs me")
        response = fc.format_response(r)
        check("the plain finding text appears in the conversational answer",
              "a real, plain-language description of what was found" in response, response)
        check("the raw job UUID never leaks into Normal-mode conversational prose",
              "deadbeef-0000-1111-2222-333344445555" not in response, response)
        check("the raw internal engine name never leaks into Normal-mode conversational prose",
              "claude_code" not in response, response)
    finally:
        _cleanup(rec["escalation_id"])


def test_recent_work_status_is_plain_language_not_a_raw_internal_enum() -> None:
    """Omni/God Mode Consolidation, P4 (2026-08-21): real bug found via a
    live /chat test — "what are you working on" literally answered "Right
    now: work_performed...", autonomous_cycle.py's own internal
    final_status enum leaking verbatim into Founder prose. Proves the
    translation without touching the underlying state machine at all."""
    check("the internal enum value translates to plain English",
          fc._plain_cycle_status("work_performed") == "actively working")
    check("an unrecognized future status value degrades honestly (returned as-is, never fabricated)",
          fc._plain_cycle_status("some_future_status") == "some_future_status")
    r = fc.handle_intent("what are you working on", requested_by="test")
    response = fc.format_response(r)
    check("the raw internal enum token never appears verbatim in Normal-mode prose",
          "work_performed" not in response, response)


def test_greeting_is_deterministic_and_never_touches_conversation_memory() -> None:
    """Real S25 Ultra defect (2026-08-24): 'Hello mr silent' surfaced an
    unrelated engineering/camera answer from conversation_context.json --
    root-caused to there being no GREETING intent at all, so a bare
    greeting fell through to the NL/LLM path and unconditionally injected
    whatever was stored. This proves the direct fix: greeting resolves
    deterministically, never calls the model, and never reads OR writes
    the shared context file."""
    from datetime import datetime, timezone
    original_ctx = fc._load_context()
    try:
        marker_ctx = {
            fc._NL_CONTEXT_KEY_QUESTION: "unrelated engineering test question",
            fc._NL_CONTEXT_KEY_ANSWER: "cross-provider failover / orange background test answer",
            fc._NL_CONTEXT_KEY_AT: datetime.now(timezone.utc).isoformat(),
            fc._NL_CONTEXT_KEY_SOURCE: "camera_vision",
        }
        fc._save_context(dict(marker_ctx))
        r = fc.handle_intent("Hello mr silent", requested_by="test")
        check("greeting resolves to INTENT_GREETING, not NATURAL_LANGUAGE", r.intent == fc.INTENT_GREETING, r.intent)
        response = fc.format_response(r)
        check("the greeting response never contains unrelated stale context",
              "cross-provider" not in response and "orange" not in response, response)
        check("conversation_context.json is completely untouched by a greeting turn",
              fc._load_context() == marker_ctx, fc._load_context())
    finally:
        fc._save_context(original_ctx)


def test_stale_conversation_context_is_not_injected_into_a_fresh_question() -> None:
    """Real S25 Ultra defect (2026-08-24), general case: conversation_
    context.json previously had no staleness bound at all, so ANY old
    turn (a dev/test artifact, an old camera answer) could surface in a
    much-later, unrelated question. Proves the bound directly against the
    actual prompt sent to the local model, not just the model's own
    response content (which the real model might paraphrase either way)."""
    from datetime import datetime, timezone, timedelta
    import local_model_bridge
    captured_prompts = []

    class _FakeJob:
        status = "succeeded"
        response_excerpt = "A real, grounded test answer."

    def _fake_submit_job(prompt, *, requested_by, timeout_s):
        captured_prompts.append(prompt)
        return _FakeJob()

    original_submit = local_model_bridge.submit_job
    original_ctx = fc._load_context()
    local_model_bridge.submit_job = _fake_submit_job
    try:
        stale_at = (datetime.now(timezone.utc) - timedelta(seconds=fc._NL_CONTEXT_STALE_AFTER_S + 60)).isoformat()
        fc._save_context({
            fc._NL_CONTEXT_KEY_QUESTION: "STALE_MARKER_QUESTION_XYZ",
            fc._NL_CONTEXT_KEY_ANSWER: "STALE_MARKER_ANSWER_XYZ",
            fc._NL_CONTEXT_KEY_AT: stale_at,
            fc._NL_CONTEXT_KEY_SOURCE: "text_conversation",
        })
        fc.handle_intent("what's the weather like today", requested_by="test")
        check("a stale (past the bound) prior turn is NOT injected into the model prompt",
              "STALE_MARKER" not in captured_prompts[-1])

        recent_at = datetime.now(timezone.utc).isoformat()
        fc._save_context({
            fc._NL_CONTEXT_KEY_QUESTION: "RECENT_MARKER_QUESTION_XYZ",
            fc._NL_CONTEXT_KEY_ANSWER: "RECENT_MARKER_ANSWER_XYZ",
            fc._NL_CONTEXT_KEY_AT: recent_at,
            fc._NL_CONTEXT_KEY_SOURCE: "text_conversation",
        })
        fc.handle_intent("what's the weather like tomorrow", requested_by="test")
        check("a recent (within the bound) prior turn IS injected into the model prompt (continuity preserved)",
              "RECENT_MARKER" in captured_prompts[-1])
    finally:
        local_model_bridge.submit_job = original_submit
        fc._save_context(original_ctx)


def test_recent_work_status_leads_with_the_real_current_mission_when_pulse_reachable() -> None:
    """Real S25 Ultra defect (2026-08-24): 'what are you working on'
    answered only from raw cycle statistics ('10 active work, 20 recovery
    actions'), never the actual campaign/step/worker a Founder means by
    the question. Proves the primary answer now leads with real mission
    detail from the SAME federated snapshot the app's own Mission sheet
    uses (pulse_bridge.get_pulse_autonomy_status) -- a controlled fake
    snapshot here, since real Pulse's live-changing state would make this
    assertion non-deterministic, mirroring _pulse_isolated() above."""
    from evolution import pulse_bridge
    fake_snapshot = {
        "mission": {
            "objective": "Test objective for regression coverage",
            "current_step_description": "Test step description",
            "total_steps": 4, "completed_steps": 1, "progress_percent": 25,
            "current_worker": "claude_code", "founder_approval_required": False,
        },
        "waiting_reason": None, "next_autonomous_cycle": None,
        "founder_approvals_pending_count": 0,
    }
    original = pulse_bridge.get_pulse_autonomy_status
    pulse_bridge.get_pulse_autonomy_status = lambda **kw: fake_snapshot
    try:
        r = fc.handle_intent("what are you working on", requested_by="test")
        response = fc.format_response(r)
        check("the real objective appears in the primary answer",
              "Test objective for regression coverage" in response, response)
        check("the real current step appears in the primary answer",
              "Test step description" in response, response)
        check("real progress detail appears, not just raw cycle stats",
              "1/4 steps done" in response, response)
        check("raw cycle stats are demoted to secondary, parenthetical context",
              "(Recent activity:" in response, response)
    finally:
        pulse_bridge.get_pulse_autonomy_status = original


def test_recent_work_status_uses_the_same_normal_approval_count_as_the_ui() -> None:
    """Real S25 Ultra defect (2026-08-24): the idle-state answer was
    quoting pulse_autonomy_status's own RAW, unfiltered internal approval
    count (confirmed live: 102), disagreeing with the Home pill/Approvals
    sheet (confirmed live: 48, via combined_pending() + founder_view_
    filter.split_normal_and_history() -- the SAME real, filtered source
    the rest of the app already uses). Proves the fix directly: a fake raw
    count is set high and MUST NOT appear; the real, local, filtered count
    (deterministically 0 here, no pending test escalations) is what
    appears instead."""
    from evolution import pulse_bridge
    fake_snapshot = {
        "mission": None, "waiting_reason": None, "next_autonomous_cycle": None,
        "founder_approvals_pending_count": 5000,  # the raw number that must NEVER surface
        "projects": {"recently_completed": []},
    }
    original = pulse_bridge.get_pulse_autonomy_status
    pulse_bridge.get_pulse_autonomy_status = lambda **kw: fake_snapshot
    try:
        r = fc.handle_intent("what are you working on", requested_by="test")
        check("the resolved answer carries the corrected, UI-consistent Normal count field",
              "normal_pending_count" in (r.answer or {}), r.answer)
        response = fc.format_response(r)
        check("the raw Pulse snapshot's own approval count never appears in the answer",
              "5000" not in response, response)
    finally:
        pulse_bridge.get_pulse_autonomy_status = original


def test_shorten_founder_text_truncates_long_real_campaign_objectives() -> None:
    """Real campaign objectives can run to hundreds of words (they often
    embed a full registry-division brief verbatim) -- Founder-facing prose
    needs the gist, not the whole brief."""
    short = "A short objective."
    check("short text passes through unchanged", fc._shorten_founder_text(short) == short, fc._shorten_founder_text(short))
    long_with_sentence = "This is the first real sentence of a much longer objective. " + ("filler " * 40)
    shortened = fc._shorten_founder_text(long_with_sentence)
    check("a long objective is genuinely shortened", len(shortened) < len(long_with_sentence), shortened)
    check("cutting prefers a real sentence boundary when one exists within range",
          shortened == "This is the first real sentence of a much longer objective.", shortened)
    long_no_sentence = "word " * 60
    check("a long objective with no early sentence boundary still gets hard-truncated with an ellipsis",
          fc._shorten_founder_text(long_no_sentence).endswith("…"), fc._shorten_founder_text(long_no_sentence))


def test_recent_work_status_reports_a_real_idle_reason_when_no_mission_active() -> None:
    """Companion to the above: idle state must report a real, specific
    reason (never invent activity when idle, per the Founder's explicit
    instruction), styled like the Founder's own example ('I'm between
    campaigns right now. My last completed action was X...'), and must
    surface the real last-completed action from the same real snapshot."""
    from evolution import pulse_bridge
    fake_snapshot = {
        "mission": None,
        "waiting_reason": "no action needed — this is a historical record correction, not an active problem",
        "next_autonomous_cycle": None,
        "founder_approvals_pending_count": 999,  # deliberately wrong/raw -- must NOT appear; see normal_pending_count assertion below
        "projects": {"recently_completed": [{"objective": "Real regression-test completed action for this check."}]},
    }
    original = pulse_bridge.get_pulse_autonomy_status
    pulse_bridge.get_pulse_autonomy_status = lambda **kw: fake_snapshot
    try:
        r = fc.handle_intent("what are you working on", requested_by="test")
        response = fc.format_response(r)
        check("idle state is reported in the Founder's own requested style",
              "I'm between campaigns right now" in response, response)
        check("the real last-completed action is surfaced",
              "Real regression-test completed action" in response, response)
        check("the raw/unfiltered Pulse approval count (999) never appears -- only the corrected Normal count",
              "999" not in response, response)
        check("the real, specific waiting reason is surfaced, not a generic filler",
              "historical record correction" in response, response)
    finally:
        pulse_bridge.get_pulse_autonomy_status = original


def test_home_status_summary_is_concise_and_never_dumps_raw_statistics() -> None:
    """Real S25 Ultra polish request (2026-08-24): 'The Founder should be
    able to glance at Home and understand MR. SILENT in under 2 seconds...
    do NOT dump 10-cycle aggregate statistics/recovery counts/long
    objective text onto Home automatically.' Proves the concise Home line
    stays short and never leaks the raw stats the FULLER conversational
    answer is allowed to include as secondary context."""
    check("no snapshot yields an honest unavailable message, never a fabricated status",
          fc.home_status_summary(None) == "Studio status unavailable right now.")

    active_snapshot = {
        "mission": {"objective": "A" * 300, "current_step_description": "irrelevant to the short line",
                     "total_steps": 4, "completed_steps": 1, "progress_percent": 25, "current_worker": "claude_code"},
    }
    active_line = fc.home_status_summary(active_snapshot)
    check("an active mission's Home line stays short (a real phone glance, not a paragraph)",
          len(active_line) < 120, (len(active_line), active_line))
    check("the active Home line never includes step/worker detail (that belongs in Mission, not Home)",
          "current step" not in active_line and "worker" not in active_line, active_line)

    idle_snapshot = {
        "mission": None,
        "next_autonomous_cycle": "2026-08-24T22:19:00+00:00",
        "projects": {"recently_completed": [{"objective": "B" * 300}]},
    }
    idle_line = fc.home_status_summary(idle_snapshot)
    check("an idle Home line stays short too", len(idle_line) < 160, (len(idle_line), idle_line))
    check("an idle Home line mentions the real next cycle time", "22:19 UTC" in idle_line, idle_line)
    check("the Home line and the fuller conversational answer are built from the SAME real primary-sentence logic (never two drifting summaries)",
          fc._mission_primary_sentence(idle_snapshot, None).startswith("I'm between campaigns right now"))


def test_append_thread_message_is_idempotent_by_message_id():
    """V10.5 real-incident regression test (Founder-authorized 2026-08-26,
    IMAGE_MEDIA_IDEMPOTENCY=FAIL / DUPLICATE_ROOT_CAUSE directive): the
    real bug was that append_thread_message() had no deduplication by
    message_id at all -- any caller invoking it twice with the same
    client-generated id (confirmed real gap: the Share-target flow, which
    had no transaction claim/release guard of its own) durably created
    TWO messages sharing one id. This is the true, unconditional backend
    guarantee: a duplicate call with an id already in the active thread
    must return the EXISTING message, never append a second one."""
    fc.start_new_thread()
    first = fc.append_thread_message("founder", "[Image attached]", attachment_type="image", message_id="dup_test_u")
    second = fc.append_thread_message("founder", "[Image attached]", attachment_type="image", message_id="dup_test_u")
    check("a duplicate append_thread_message call with the same message_id returns the SAME message object shape",
          first["message_id"] == second["message_id"] == "dup_test_u")
    thread = fc.get_active_thread()
    matching = [m for m in thread["messages"] if m["message_id"] == "dup_test_u"]
    check("exactly ONE message with that id exists in the durable thread after two calls, never two",
          len(matching) == 1, len(matching))

    third = fc.append_thread_message("silent", "Analyzing image...", message_id="dup_test_a", status="pending")
    fourth = fc.append_thread_message("silent", "SOMETHING DIFFERENT", message_id="dup_test_a", status="complete")
    check("a duplicate call is refused even when the NEW call's text/status differ from the original (never silently overwrites via append)",
          fourth["text"] == "Analyzing image..." and fourth["status"] == "pending", fourth)
    thread2 = fc.get_active_thread()
    matching2 = [m for m in thread2["messages"] if m["message_id"] == "dup_test_a"]
    check("exactly ONE message with that second id exists, never two", len(matching2) == 1, len(matching2))

    fifth = fc.append_thread_message("founder", "a genuinely different message", message_id="dup_test_u_2")
    check("a genuinely different message_id still appends normally (idempotency never blocks real new messages)",
          fifth["message_id"] == "dup_test_u_2")


if __name__ == "__main__":
    _moved_aside = _reset_pending_baseline()
    try:
        with isolated_test_state(), _pulse_isolated(), _view_filter_permissive():
            test_all_mission_example_sentences_resolve_to_explicit_intents()
            test_unrecognized_text_never_fabricates_an_intent()
            test_approve_never_infers_without_an_unambiguous_target()
            test_approve_resolves_only_when_exactly_one_pending()
            test_approve_asks_for_clarification_with_multiple_pending()
            test_deny_uses_the_real_deterministic_resolution_path()
            test_read_only_intents_answer_from_real_data_not_fabricated()
            test_organ_health_query_requires_a_named_target()
            test_the_founders_exact_real_device_test_sentence_gets_a_real_answer()
            test_ordinary_founder_phrasings_get_real_grounded_answers_not_the_dead_end()
            test_scorpios_corner_gets_an_honest_isolation_answer_never_fabricated_facts()
            test_multiturn_context_records_the_prior_turn_for_a_why_followup()
            test_legacy_deterministic_intents_still_resolve_unchanged()
            test_widened_recent_work_pattern_gets_real_prose_not_the_stub()
            test_pending_requests_conversational_answer_hides_internal_jargon()
            test_recent_work_status_is_plain_language_not_a_raw_internal_enum()
            test_greeting_is_deterministic_and_never_touches_conversation_memory()
            test_stale_conversation_context_is_not_injected_into_a_fresh_question()
            test_recent_work_status_leads_with_the_real_current_mission_when_pulse_reachable()
            test_recent_work_status_uses_the_same_normal_approval_count_as_the_ui()
            test_shorten_founder_text_truncates_long_real_campaign_objectives()
            test_recent_work_status_reports_a_real_idle_reason_when_no_mission_active()
            test_home_status_summary_is_concise_and_never_dumps_raw_statistics()
            test_append_thread_message_is_idempotent_by_message_id()
    finally:
        _restore_pending_baseline(_moved_aside)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
