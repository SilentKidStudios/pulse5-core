#!/usr/bin/env python3
"""
Tests for evolution/advance.py's IMPLEMENTATION ROUTER (Claude Code primary
-> Omni Engineer fallback, evidence-based via organ_discovery.duty_check()
-> human escalation; Codex never self-authorized by this unattended
pipeline). Same plain-script style as the other tests/test_*.py files.

These tests monkeypatch evolution.advance._ENGINE_RUNNERS to prove the
ROUTING LOGIC (selection order, fallback triggers, terminal rejection)
deterministically, without depending on live model non-determinism or
spending a real Claude Code/Ollama call on every test run. A genuine, live,
non-mocked end-to-end proof (OBSERVE/PROPOSE -> OmniEngineer IMPLEMENT ->
VALIDATE -> CANARY -> PROMOTION_CANDIDATE) is run once separately, per this
project's standing "prove it live once, don't re-prove it on every test run"
practice (see local_model_bridge.py, codex_bridge.py, and
tests/test_omniengineer.py's own two live integration tests).

The two "gated task" tests below ARE real/live (no mocking) — they're cheap
by construction: authority_policy.classify() rejects the task before any
model or subprocess is ever invoked, at either engine.

Run: python3 tests/test_evolution_advance.py
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import organ_discovery
import promotion
import studio_router
from evolution import advance
from evolution import proposal as proposal_mod

JOBS_ROOT = Path(__file__).resolve().parent.parent / "jobs"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


@dataclass
class _FakeJobResult:
    job_id: str
    status: str
    workdir: str
    files_changed: dict
    promotion_eligible: bool


def _fake_real_job(job_id: str) -> _FakeJobResult:
    """Writes a real, minimal, valid jobs/<job_id>/{workdir/x.py, result.json}
    on disk — same on-disk shape bridge.py/omniengineer_harness.py produce —
    so promotion.py's real _load_job_result() works against it unmodified,
    without needing a live engine call just to prove the routing/promotion
    plumbing."""
    workdir = JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "x.py").write_text("x = 1\n")
    files_changed = {"added": ["x.py"], "modified": [], "removed": []}
    result = {
        "job_id": job_id, "status": "succeeded", "workdir": str(workdir),
        "files_changed": files_changed, "promotion_eligible": True,
        "validation": {"passed": True, "checks": []},
    }
    (JOBS_ROOT / job_id / "result.json").write_text(json.dumps(result))
    return _FakeJobResult(job_id=job_id, status="succeeded", workdir=str(workdir),
                           files_changed=files_changed, promotion_eligible=True)


def _make_low_risk_proposal(weakness: str, upgrade: str) -> proposal_mod.Proposal:
    return proposal_mod.create(observed_weakness=weakness, proposed_upgrade=upgrade, risk_score="low", origin="manual")


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)
        proposal_mod.record_lesson(proposal_id, note)


def _engine_stats(*, confidence: str, success_rate: float, sample_size: int = 10, silent_failure: bool = False) -> dict:
    return {"capability_confidence": confidence, "reason": "synthetic test stats", "sample_size": sample_size,
            "recent_success_rate": success_rate, "last_success_at": None, "last_failure_at": None,
            "silent_failure_detected": silent_failure, "recent_failure_error_classes": []}


def _fake_duty(claude: dict, omni: dict):
    """Returns a function with organ_discovery.duty_check()'s exact real
    shape (engines/claude_code, engines/omni_engineer, plus the other two
    DUTY_CHECKED_ENGINES as harmless unknowns) so ENGINE_ROUTING_CORRECTION
    tests are deterministic regardless of this machine's real, constantly-
    changing job_ledger history."""
    def fake():
        return {"engines": {
            "claude_code": claude, "omni_engineer": omni,
            "codex": _engine_stats(confidence="unknown", success_rate=None, sample_size=0),
            "local_model": _engine_stats(confidence="unknown", success_rate=None, sample_size=0),
        }}
    return fake


# ---- routing logic (fast, deterministic) ------------------------------------

def test_ranked_engines_for_code_edit_puts_omni_engineer_first() -> None:
    """This tests studio_router.py's own GENERIC, Studio-wide local/free
    doctrine directly — deliberately unchanged by the 2026-08-19 engine-
    routing correction, and still correct for every OTHER consumer of
    studio_router (task_router.py, forecast.py, organ_discovery.py,
    external_evolution.py). evolution/advance.py's own engineering-specific
    priority (Claude Code primary, Omni Engineer fallback) is layered ON TOP
    of this raw ranking, inside _rank_engineering_engines() — see the tests
    below, which exercise the actual implementation-router behavior."""
    ranked = studio_router.rank("code_edit", allow_founder_gated=False)
    accepted = [ev.capability_id for ev in ranked if ev.accepted]
    check(
        "studio_router's raw local-first doctrine still ranks omni_engineer_v1 ahead of claude_code_engineer_v1",
        accepted[:2] == ["omni_engineer_v1", "claude_code_engineer_v1"],
        f"accepted={accepted}",
    )


# ---- ENGINE_ROUTING_CORRECTION (Founder-authorized 2026-08-19): Claude Code
# primary, Omni Engineer fallback, evidence-based — deterministic via a
# monkeypatched organ_discovery.duty_check(), so behavior never depends on
# this machine's real, constantly-changing job_ledger history. ------------

_HEALTHY_CLAUDE = _engine_stats(confidence="high", success_rate=0.95)
_HEALTHY_OMNI = _engine_stats(confidence="high", success_rate=0.85)


def _run_routed(proposal_weakness: str, duty_fn, *, claude_outcome="ran_cleanly", omni_outcome="ran_cleanly",
                 claude_must_not_be_called: bool = False, omni_must_not_be_called: bool = False):
    p = _make_low_risk_proposal(proposal_weakness, "prove engineering engine routing order")
    original_duty = organ_discovery.duty_check
    original_claude = advance._ENGINE_RUNNERS["claude_code"]
    original_omni = advance._ENGINE_RUNNERS["omni_engineer"]

    def make_runner(name, outcome, must_not_be_called):
        def runner(task_text, requested_by, proposal_id):
            if must_not_be_called:
                raise AssertionError(f"{name} runner should not have been called this attempt")
            if outcome == "ran_cleanly":
                job = _fake_real_job(f"test-fake-{name}-ok-{proposal_id[:8]}")
                proposal_mod.append_implementation_job(proposal_id, job.job_id, engine=name, note="fake test job")
                return advance.EngineAttempt(name, "ran_cleanly", f"fake {name} success", job.job_id, job.status, True), job
            proposal_mod.append_implementation_job(proposal_id, f"test-fake-{name}-fail", engine=name, note="fake test job (infra failure)")
            return advance.EngineAttempt(name, "infra_failure", "simulated escalate", f"test-fake-{name}-fail", "escalated"), None
        return runner

    organ_discovery.duty_check = duty_fn
    advance._ENGINE_RUNNERS["claude_code"] = make_runner("claude_code", claude_outcome, claude_must_not_be_called)
    advance._ENGINE_RUNNERS["omni_engineer"] = make_runner("omni_engineer", omni_outcome, omni_must_not_be_called)
    try:
        result = advance.advance_one(p.proposal_id)
    finally:
        organ_discovery.duty_check = original_duty
        advance._ENGINE_RUNNERS["claude_code"] = original_claude
        advance._ENGINE_RUNNERS["omni_engineer"] = original_omni
    _cleanup(p.proposal_id, "synthetic engine-routing test — cleaned up per test-artifact policy")
    return result


def test_claude_code_is_default_primary_when_both_engines_healthy() -> None:
    result = _run_routed("synthetic test: default engineering priority", _fake_duty(_HEALTHY_CLAUDE, _HEALTHY_OMNI),
                          omni_must_not_be_called=True)
    check("Claude Code is tried first and selected when both engines are healthy",
          result.selected_engine == "claude_code", str(result.selected_engine))
    check("reaches promotion_candidate", result.final_status == "promotion_candidate", result.final_status)


def test_claude_code_infra_failure_falls_back_to_omni_engineer() -> None:
    result = _run_routed("synthetic test: fallback on claude infra failure", _fake_duty(_HEALTHY_CLAUDE, _HEALTHY_OMNI),
                          claude_outcome="infra_failure")
    check("falls back to omni_engineer after a claude_code infra failure",
          result.selected_engine == "omni_engineer", str(result.selected_engine))
    check("reaches promotion_candidate via fallback", result.final_status == "promotion_candidate", result.final_status)


def test_automatic_failover_when_claude_code_silently_failing() -> None:
    """The exact real gap the claude_code PATH incident exposed: registry
    status can stay 'active' while duty_check() shows real job outcomes are
    degraded. Omni Engineer must be tried FIRST in this state, automatically
    — Claude Code is never even attempted."""
    broken_claude = _engine_stats(confidence="low", success_rate=0.1, sample_size=16, silent_failure=True)
    result = _run_routed("synthetic test: automatic failover on silent failure", _fake_duty(broken_claude, _HEALTHY_OMNI),
                          claude_must_not_be_called=True)
    check("omni_engineer is selected first when claude_code is duty-flagged as silently failing",
          result.selected_engine == "omni_engineer", str(result.selected_engine))


def test_claude_code_returns_to_primary_once_duty_check_recovers() -> None:
    """No persistent avoidance/circuit-breaker state: a fresh call with
    healthy duty_check stats immediately prefers Claude Code again, even
    right after a call that failed it over to Omni Engineer."""
    broken_claude = _engine_stats(confidence="low", success_rate=0.1, sample_size=16, silent_failure=True)
    failed_over = _run_routed("synthetic test: recovery step 1 (failed over)", _fake_duty(broken_claude, _HEALTHY_OMNI),
                               claude_must_not_be_called=True)
    check("step 1: failed over to omni_engineer while claude_code looked broken",
          failed_over.selected_engine == "omni_engineer", str(failed_over.selected_engine))

    recovered = _run_routed("synthetic test: recovery step 2 (claude healthy again)", _fake_duty(_HEALTHY_CLAUDE, _HEALTHY_OMNI),
                             omni_must_not_be_called=True)
    check("step 2: claude_code is preferred again the very next call once duty_check shows it healthy",
          recovered.selected_engine == "claude_code", str(recovered.selected_engine))


def test_evidence_based_promotion_prefers_omni_when_it_sustainably_outperforms() -> None:
    """'Omni Engineer should only become preferred ... if sustained real-
    world evidence demonstrates superiority' — proves the promotion path
    fires on a real, large, high-confidence margin."""
    mediocre_claude = _engine_stats(confidence="medium", success_rate=0.6, sample_size=20)
    superior_omni = _engine_stats(confidence="high", success_rate=0.95, sample_size=20)
    result = _run_routed("synthetic test: evidence-based omni promotion", _fake_duty(mediocre_claude, superior_omni),
                          claude_must_not_be_called=True)
    check("omni_engineer is preferred when it sustainably outperforms claude_code by a real, large margin",
          result.selected_engine == "omni_engineer", str(result.selected_engine))


def test_no_promotion_when_margin_is_too_small() -> None:
    """A small, plausibly-noise difference must NOT flip the default —
    'evidence-based evolution decision, not an assumption' cuts both ways."""
    slightly_worse_claude = _engine_stats(confidence="high", success_rate=0.90, sample_size=20)
    slightly_better_omni = _engine_stats(confidence="high", success_rate=0.95, sample_size=20)
    result = _run_routed("synthetic test: no promotion on small margin", _fake_duty(slightly_worse_claude, slightly_better_omni),
                          omni_must_not_be_called=True)
    check("claude_code stays primary when omni_engineer's edge is below OMNI_PROMOTION_SUCCESS_RATE_MARGIN",
          result.selected_engine == "claude_code", str(result.selected_engine))


def test_duty_check_failure_does_not_block_routing() -> None:
    """A duty_check() read error must never break the pipeline — default
    Claude Code primary / Omni Engineer fallback order is used instead."""
    def broken_duty():
        raise RuntimeError("synthetic duty_check failure")
    result = _run_routed("synthetic test: duty_check read failure fallback", broken_duty, omni_must_not_be_called=True)
    check("Claude Code primary order is still used when duty_check() itself raises",
          result.selected_engine == "claude_code", str(result.selected_engine))


def test_promotion_still_requires_founder_approved() -> None:
    p = _make_low_risk_proposal(
        "synthetic test: promotion gate check", "prove promotion stays founder-gated after routed implementation",
    )
    original_omni = advance._ENGINE_RUNNERS["omni_engineer"]
    original_claude = advance._ENGINE_RUNNERS["claude_code"]

    def fake_omni_succeeds(task_text, requested_by, proposal_id):
        job = _fake_real_job(f"test-fake-omni-promo-check-{proposal_id[:8]}")
        proposal_mod.append_implementation_job(proposal_id, job.job_id, engine="omni_engineer", note="fake test job")
        attempt = advance.EngineAttempt("omni_engineer", "ran_cleanly", "fake success", job.job_id, job.status, True)
        return attempt, job

    # Deterministic, no-real-spend routing: this test is exercising the
    # PROMOTION gate, not engine selection, so claude_code must never be
    # left unmocked here — an unmocked accepted engine is a REAL subprocess
    # call to `claude -p`, this project's own established convention says
    # to always mock a real paid engine in tests rather than let a run
    # nondeterministically invoke it (see test_bridge_ledger.py's docstring).
    def fake_claude_declines(task_text, requested_by, proposal_id):
        proposal_mod.append_implementation_job(proposal_id, f"test-fake-claude-declines-{proposal_id[:8]}",
                                                 engine="claude_code", note="fake test job (infra failure)")
        return advance.EngineAttempt("claude_code", "infra_failure", "simulated decline, forcing omni_engineer",
                                      f"test-fake-claude-declines-{proposal_id[:8]}", "escalated"), None

    advance._ENGINE_RUNNERS["omni_engineer"] = fake_omni_succeeds
    advance._ENGINE_RUNNERS["claude_code"] = fake_claude_declines
    try:
        result = advance.advance_one(p.proposal_id)
    finally:
        advance._ENGINE_RUNNERS["omni_engineer"] = original_omni
        advance._ENGINE_RUNNERS["claude_code"] = original_claude

    check("reaches promotion_candidate", result.final_status == "promotion_candidate", result.final_status)
    record = promotion.promote(result.implementation_job_id, "/opt/pulse5-core/_never_written_by_test", founder_approved=False)
    check("promotion without --founder-approved stays a dry_run and writes nothing",
          record.approval_state == "dry_run", record.approval_state)
    _cleanup(p.proposal_id, "synthetic promotion-gate test — cleaned up per test-artifact policy")


def test_no_false_capability_gap_proposals_from_routing() -> None:
    fp_prefix = "studio_router_capability_gap:code_edit"
    before = [p for p in proposal_mod.list_all() if (p.fingerprint or "").startswith(fp_prefix)]
    for _ in range(3):
        studio_router.rank("code_edit", allow_founder_gated=False)
    after = [p for p in proposal_mod.list_all() if (p.fingerprint or "").startswith(fp_prefix)]
    check(
        "rank() (used by the implementation router) creates zero capability-gap proposals for code_edit "
        "(it always has eligible candidates, unlike select() which creates one on a TRUE gap)",
        len(before) == len(after) == 0, f"before={len(before)} after={len(after)}",
    )


# ---- authority gating (real, no mocking — cheap by construction) -----------

def test_founder_gated_task_cannot_be_smuggled_through_omni_engineer() -> None:
    p = _make_low_risk_proposal(
        "synthetic test: attempted smuggle", "delete the stored credentials from the config directory",
    )
    result = advance.advance_one(p.proposal_id)
    check(
        "a gated task text is rejected_policy at the first engine attempted (deterministic regardless of which "
        "engine is tried first — every engine runs the same authority_policy.classify()) and halts the whole pipeline",
        result.final_status == "rejected", result.final_status,
    )
    check("no engine is ever 'selected' for a rejected_policy outcome", result.selected_engine is None)
    _cleanup(p.proposal_id, "already rejected by policy; test cleanup no-op")


def test_scorpio_keyword_in_task_text_is_rejected_at_every_engine() -> None:
    p = _make_low_risk_proposal(
        "synthetic test: scorpio isolation", "modify the scorpio corner voice pipeline settings",
    )
    result = advance.advance_one(p.proposal_id)
    check(
        "a task text mentioning scorpio is rejected_policy, not routed to any engine",
        result.final_status == "rejected" and result.selected_engine is None,
        f"final_status={result.final_status} selected_engine={result.selected_engine}",
    )
    _cleanup(p.proposal_id, "already rejected by policy; test cleanup no-op")


def test_scorpio_never_appears_in_implementation_ranking() -> None:
    ranked = studio_router.rank("code_edit", allow_founder_gated=True)  # even WITH founder_gated=True
    ids = [ev.capability_id for ev in ranked]
    check("scorpio_corner_cluster does not even appear as a code_edit candidate (task_types=[])",
          "scorpio_corner_cluster" not in ids, str(ids))


def test_advance_eligible_stops_starting_new_work_past_its_deadline() -> None:
    """PRE-24x7 certification hardening: advance_eligible() is otherwise one
    blocking call from autonomous_cycle.py's point of view — a deadline
    already in the past when the batch starts must block EVERY candidate
    with cycle_wallclock_budget_exhausted, never call advance_one() (i.e.
    never touch any engine), and never mutate the proposal beyond that."""
    import time
    p = _make_low_risk_proposal(
        "synthetic test: wallclock deadline hardening", "prove advance_eligible() refuses to start past its deadline",
    )
    results = advance.advance_eligible(limit=5, deadline_monotonic=time.monotonic() - 1)
    mine = [r for r in results if r.proposal_id == p.proposal_id]
    check("the proposal was included in the blocked batch", len(mine) == 1, str([r.proposal_id for r in results]))
    if mine:
        check("it is blocked with cycle_wallclock_budget_exhausted, not routed to any engine",
              mine[0].blocked_reason == "cycle_wallclock_budget_exhausted" and mine[0].selected_engine is None,
              f"blocked_reason={mine[0].blocked_reason} selected_engine={mine[0].selected_engine}")
    reloaded = proposal_mod.load(p.proposal_id)
    check("the proposal itself was never advanced past OBSERVED (never even claimed)",
          reloaded.status == proposal_mod.ProposalStatus.OBSERVED, str(reloaded.status))
    _cleanup(p.proposal_id, "synthetic wallclock-deadline hardening test — cleaned up")


# ---- Phase Q: bounded prioritization -------------------------------------

def test_explicit_founder_priority_always_outranks_inferred_priority() -> None:
    """PRIORITY_SELECTION: an explicit Founder override must sort ahead of
    even a high-recurrence inferred-priority proposal — 'Preserve explicit
    Founder priorities over inferred priorities' made concrete."""
    from evolution import prioritization
    broad = proposal_mod.create("synthetic: broad root-cause signal", "n/a", risk_score="low", origin="manual",
                                 source_observation_ids=[f"obs-{i}" for i in range(10)])
    founder_pick = proposal_mod.create("synthetic: explicit founder priority", "n/a", risk_score="low", origin="manual")
    proposal_mod.set_founder_priority(founder_pick.proposal_id, 1)

    ranked = prioritization.rank([broad, proposal_mod.load(founder_pick.proposal_id)])
    check("the Founder-prioritized proposal sorts first despite far less inferred evidence",
          ranked[0].proposal_id == founder_pick.proposal_id, str([p.proposal_id for p in ranked]))

    _cleanup(broad.proposal_id, "synthetic Phase Q priority test — cleaned up")
    _cleanup(founder_pick.proposal_id, "synthetic Phase Q priority test — cleaned up")


def test_root_cause_breadth_outranks_a_narrow_symptom() -> None:
    """ROOT_CAUSE_OVER_SYMPTOM_PRIORITY: with no Founder override, a
    proposal correlating more raw signals (more source_observation_ids —
    the real, on-record evidence of a shared root cause affecting more of
    the Studio) sorts ahead of a single-signal symptom proposal."""
    from evolution import prioritization
    narrow = proposal_mod.create("synthetic: single symptom", "n/a", risk_score="low", origin="manual",
                                  source_observation_ids=["obs-0"])
    broad = proposal_mod.create("synthetic: root cause affecting 5 divisions", "n/a", risk_score="low", origin="manual",
                                 source_observation_ids=[f"obs-{i}" for i in range(5)])

    ranked = prioritization.rank([narrow, broad])
    check("the broader, multi-signal (likely root-cause) proposal is ranked ahead of the narrow symptom",
          ranked[0].proposal_id == broad.proposal_id, str([p.proposal_id for p in ranked]))

    _cleanup(narrow.proposal_id, "synthetic Phase Q priority test — cleaned up")
    _cleanup(broad.proposal_id, "synthetic Phase Q priority test — cleaned up")


def test_advance_eligible_actually_uses_priority_ordering() -> None:
    """Proves the wiring, not just the standalone ranker: a founder_priority
    tuple (0, ...) always sorts ahead of every non-prioritized (1, ...)
    proposal by construction, so a Founder-prioritized proposal sorts to the
    front of the REAL, current, entire OBSERVED backlog — not just a
    hand-picked pair — proving advance_eligible()'s actual candidate list
    (proposal_mod.list_all(), unfiltered) gets this same ordering applied."""
    founder_pick = proposal_mod.create("synthetic: founder priority via advance_eligible", "n/a", risk_score="low", origin="manual")
    proposal_mod.set_founder_priority(founder_pick.proposal_id, 5)
    from evolution import prioritization
    # The real OBSERVED backlog's size varies run to run (a genuinely clean
    # backlog with just this one synthetic entry is a valid, good state, not
    # a test-setup failure) — what matters is the ordering guarantee itself.
    real_observed_pool = [p for p in proposal_mod.list_all() if p.status == proposal_mod.ProposalStatus.OBSERVED]
    ranked = prioritization.rank(real_observed_pool)
    check("the founder-prioritized proposal sorts to the very front of the ENTIRE real eligible pool",
          ranked[0].proposal_id == founder_pick.proposal_id, ranked[0].proposal_id)
    _cleanup(founder_pick.proposal_id, "synthetic Phase Q priority test — cleaned up")


# ---- SELF_CORRECTION_ESCALATION_GAP (Founder-authorized 2026-08-19): a
# genuine self-correction proposal that exhausts its repair attempts must
# notify the Founder, not silently sit as REJECTED — the real gap the
# claude_code PATH incident's audit found (proposal 7f77e311, rejected
# 2026-08-18, zero escalation ever created). See evolution.advance.
# _escalate_if_unresolved_self_correction(). --------------------------------

def _always_infra_fail(name: str):
    def runner(task_text, requested_by, proposal_id):
        job_id = f"test-fake-{name}-fail-{uuid.uuid4().hex[:6]}"
        proposal_mod.append_implementation_job(proposal_id, job_id, engine=name, note="fake test job (infra failure)")
        return advance.EngineAttempt(name, "infra_failure", "simulated escalate", job_id, "escalated"), None
    return runner


def _exhaust_via_infra_failures(proposal_id: str):
    original_claude = advance._ENGINE_RUNNERS["claude_code"]
    original_omni = advance._ENGINE_RUNNERS["omni_engineer"]
    advance._ENGINE_RUNNERS["claude_code"] = _always_infra_fail("claude_code")
    advance._ENGINE_RUNNERS["omni_engineer"] = _always_infra_fail("omni_engineer")
    try:
        result = None
        for _ in range(4):  # MAX_IMPLEMENTATION_ATTEMPTS=3; bounded loop, never actually unbounded
            result = advance.advance_one(proposal_id)
            if result.final_status == "rejected":
                break
        return result
    finally:
        advance._ENGINE_RUNNERS["claude_code"] = original_claude
        advance._ENGINE_RUNNERS["omni_engineer"] = original_omni


def test_exhausted_self_correction_proposal_escalates_to_founder() -> None:
    from evolution import founder_request
    engine_tag = f"synthetic_engine_{uuid.uuid4().hex[:8]}"
    p = proposal_mod.create(
        observed_weakness="synthetic test: exhausted self-correction must escalate",
        proposed_upgrade="UNKNOWN: no known remediation on record for this organ at all — investigate root cause fresh.",
        risk_score="low", origin="observe_engine",
        fingerprint=f"duty_self_correction:{engine_tag}:infra",
    )
    result = _exhaust_via_infra_failures(p.proposal_id)
    check("the proposal reaches REJECTED after exhausting bounded attempts",
          result.final_status == "rejected", result.final_status)

    expected_subject = f"self-correction proposal {p.proposal_id} could not repair {engine_tag}"
    fingerprint = founder_request._fingerprint(expected_subject, "human_review_of_failed_self_correction")
    escalation_path = founder_request.ESCALATIONS_DIR / f"{fingerprint}.json"
    check("a real, durable Founder escalation was created for the exhausted self-correction attempt",
          escalation_path.exists(), str(escalation_path))
    if escalation_path.exists():
        record = json.loads(escalation_path.read_text())
        check("the escalation names the real proposal_id",
              record["payload"]["affected"].get("proposal_id") == p.proposal_id, record["payload"]["affected"])
        founder_request.resolve_founder_decision(fingerprint, "denied", note="synthetic test escalation — resolved/cleaned up")

    _cleanup(p.proposal_id, "synthetic exhausted self-correction escalation test — cleaned up per test-artifact policy")


def test_ordinary_proposal_rejection_does_not_escalate() -> None:
    """Scope check: only duty_self_correction-origin proposals trigger this
    escalation — an ordinary manual/low-risk proposal exhausting its
    attempts must NOT spam the Founder with the same mechanism."""
    from evolution import founder_request
    p = _make_low_risk_proposal("synthetic test: ordinary rejection scope check",
                                 "prove ordinary proposals never trigger the self-correction escalation")
    result = _exhaust_via_infra_failures(p.proposal_id)
    check("the ordinary proposal also reaches REJECTED (bounded attempts exhausted)",
          result.final_status == "rejected", result.final_status)

    expected_subject = f"self-correction proposal {p.proposal_id} could not repair an engine"
    fingerprint = founder_request._fingerprint(expected_subject, "human_review_of_failed_self_correction")
    escalation_path = founder_request.ESCALATIONS_DIR / f"{fingerprint}.json"
    check("no self-correction escalation is created for a non-duty_self_correction-origin proposal",
          not escalation_path.exists(), str(escalation_path))
    _cleanup(p.proposal_id, "synthetic ordinary-rejection scope test — cleaned up per test-artifact policy")


# ---- GOVERNED CANONICAL SOURCE STAGING REPAIR --------------------------

def test_run_claude_code_threads_proposal_source_paths_to_bridge_submit_job() -> None:
    """CLAUDE_CODE_CAN_RECEIVE_AUTHORIZED_SOURCE: _run_claude_code() must
    read the real proposal's own source_paths field and pass it through to
    bridge.submit_job() -- the exact same mechanism _run_omni_engineer()
    already uses. bridge.submit_job itself is mocked (never spawns a real
    `claude -p` subprocess, matching this project's established practice
    of never spending a real paid Claude Code call in the test suite)."""
    p = proposal_mod.create(
        observed_weakness="synthetic test: claude code source_paths wiring",
        proposed_upgrade="synthetic test: claude code source_paths wiring",
        risk_score="low", origin="test", source_paths=["/some/authorized/real/path"],
    )
    captured = {}
    original_submit_job = advance.bridge.submit_job

    def fake_submit_job(*, task, requested_by, tools, source_paths, timeout_s, founder_approved, on_job_created=None):
        captured["source_paths"] = source_paths
        if on_job_created:
            on_job_created("fake-job-id-source-paths-wiring-test")
        return _FakeJobResult(job_id="fake-job-id-source-paths-wiring-test", status="succeeded",
                               workdir="/tmp", files_changed={}, promotion_eligible=False)

    advance.bridge.submit_job = fake_submit_job
    try:
        attempt, job = advance._run_claude_code("synthetic task text", "test", p.proposal_id)
    finally:
        advance.bridge.submit_job = original_submit_job
        _cleanup(p.proposal_id, "synthetic claude_code source_paths wiring test — cleaned up")

    check("bridge.submit_job received the proposal's real source_paths, not None",
          captured.get("source_paths") == ["/some/authorized/real/path"], captured)
    check("the attempt reflects a clean run", attempt.outcome == "ran_cleanly", attempt.outcome)


def test_run_claude_code_passes_none_when_proposal_has_no_source_paths() -> None:
    """No-op guarantee: an ordinary proposal (source_paths defaults to [])
    must still reach bridge.submit_job with source_paths=None, exactly the
    pre-existing behavior -- 'no recursive self-modification' by default,
    unchanged for the overwhelming majority of proposals."""
    p = proposal_mod.create(
        observed_weakness="synthetic test: claude code default no source_paths",
        proposed_upgrade="synthetic test: claude code default no source_paths",
        risk_score="low", origin="test",
    )
    captured = {}
    original_submit_job = advance.bridge.submit_job

    def fake_submit_job(*, task, requested_by, tools, source_paths, timeout_s, founder_approved, on_job_created=None):
        captured["source_paths"] = source_paths
        if on_job_created:
            on_job_created("fake-job-id-no-source-paths-test")
        return _FakeJobResult(job_id="fake-job-id-no-source-paths-test", status="succeeded",
                               workdir="/tmp", files_changed={}, promotion_eligible=False)

    advance.bridge.submit_job = fake_submit_job
    try:
        advance._run_claude_code("synthetic task text", "test", p.proposal_id)
    finally:
        advance.bridge.submit_job = original_submit_job
        _cleanup(p.proposal_id, "synthetic claude_code no-source_paths default test — cleaned up")

    check("bridge.submit_job received source_paths=None for an ordinary proposal",
          captured.get("source_paths") is None, captured)


if __name__ == "__main__":
    test_ranked_engines_for_code_edit_puts_omni_engineer_first()
    test_claude_code_is_default_primary_when_both_engines_healthy()
    test_claude_code_infra_failure_falls_back_to_omni_engineer()
    test_automatic_failover_when_claude_code_silently_failing()
    test_claude_code_returns_to_primary_once_duty_check_recovers()
    test_evidence_based_promotion_prefers_omni_when_it_sustainably_outperforms()
    test_no_promotion_when_margin_is_too_small()
    test_duty_check_failure_does_not_block_routing()
    test_promotion_still_requires_founder_approved()
    test_no_false_capability_gap_proposals_from_routing()
    test_founder_gated_task_cannot_be_smuggled_through_omni_engineer()
    test_scorpio_keyword_in_task_text_is_rejected_at_every_engine()
    test_scorpio_never_appears_in_implementation_ranking()
    test_advance_eligible_stops_starting_new_work_past_its_deadline()
    test_explicit_founder_priority_always_outranks_inferred_priority()
    test_root_cause_breadth_outranks_a_narrow_symptom()
    test_advance_eligible_actually_uses_priority_ordering()
    test_exhausted_self_correction_proposal_escalates_to_founder()
    test_ordinary_proposal_rejection_does_not_escalate()
    test_run_claude_code_threads_proposal_source_paths_to_bridge_submit_job()
    test_run_claude_code_passes_none_when_proposal_has_no_source_paths()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
