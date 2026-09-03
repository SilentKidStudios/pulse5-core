#!/usr/bin/env python3
"""
Tests for the FOUNDER TOP-10 -> CANONICAL PROPOSAL bridge
(evolution/observe.py::signal_governing_priority_needs_proposal(), wired
into observe.run() and threaded from autonomous_cycle.run_cycle()'s
already-computed _consume_founder_top10() result).

WHAT THIS CLOSES: the live persistent clock (mrsilent-autonomous-cycle.timer
-> autonomous_cycle.run_cycle()) previously read the Founder Top-10 governing
rank every cycle but never fed it anywhere -- OBSERVE only reacted to its own
operational signals (failed jobs, service health, capability gaps), so a
governing rank with zero existing engineering proposal could sit forever with
no autonomous path to one. This bridge adds exactly one more OBSERVE signal,
reusing the SAME proposal store / dedup / advance pipeline every other signal
already uses -- no second selector, no second proposal system, no second
scheduler (BLEND_NOT_REPLACE).

WHAT THIS DELIBERATELY DOES NOT DO: it never suggests risk_score="low" for a
freshly-bridged, un-scoped Founder priority -- that would make an abstract
strategic priority silently auto-executable, which the project's own
governance semantics explicitly reject ("not every discovered idea is
executable"). It always proposes founder_gated, surfaced via the existing
pending_founder_gated_jobs reporting path for explicit Founder review/
scoping. Only an already-scoped "low" proposal (created the normal way, by a
human or a properly-scoped future signal) is eligible to auto-advance --
proven unaffected here too.

These tests never call autonomous_cycle.run_cycle() -- that would manually
trigger the real, live, currently-scheduled persistent clock against real
system state, which this project's own operating doctrine reserves for the
natural systemd timer only. The autonomous_cycle.py wiring is instead proven
by source inspection (same technique test_founder_top10_persistent_clock.py
already uses for its own no-duplicate-selector proof).

Run: python3 tests/test_founder_priority_proposal_bridge.py
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autonomous_cycle  # noqa: E402
from evolution import advance  # noqa: E402
from evolution import observe  # noqa: E402
from evolution import proposal as proposal_mod  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)
        proposal_mod.record_lesson(proposal_id, note)


def _synthetic_rank_id() -> str:
    return f"SYNTHETIC_TEST_RANK_{uuid.uuid4().hex[:12]}"


def _code_only(fn) -> str:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(tree)


# --------------------------------------------------------------------- #
# 1. no governing id -> no-op
# --------------------------------------------------------------------- #
def test_none_governing_id_is_a_noop() -> None:
    obs = observe.signal_governing_priority_needs_proposal(None)
    check("None governing id produces zero observations", obs == [], str(obs))


# --------------------------------------------------------------------- #
# 2. a genuinely unproposed rank gets one founder_gated observation
# --------------------------------------------------------------------- #
def test_unproposed_rank_gets_one_founder_gated_observation() -> None:
    rank_id = _synthetic_rank_id()
    obs = observe.signal_governing_priority_needs_proposal(rank_id)
    check("exactly one observation for a fresh unproposed rank", len(obs) == 1, str(obs))
    if not obs:
        return
    o = obs[0]
    check("signal_type is founder_priority_unproposed", o.signal_type == "founder_priority_unproposed", o.signal_type)
    check("severity is medium (not low -- low clusters are skipped, would silently never propose)",
          o.severity == "medium", o.severity)
    check("suggested_risk_score is founder_gated, never low",
          o.suggested_risk_score == "founder_gated", o.suggested_risk_score)
    check("dedupe_key is stable and rank-specific",
          o.dedupe_key == [f"founder_priority_unproposed:{rank_id}"], str(o.dedupe_key))
    check("rank id appears in the description", rank_id in o.description, o.description)


# --------------------------------------------------------------------- #
# 3. dedup: a rank that already has ANY proposal (open or closed) -> no-op
# --------------------------------------------------------------------- #
def test_rank_with_existing_proposal_is_not_duplicated() -> None:
    rank_id = _synthetic_rank_id()
    p = proposal_mod.create(
        observed_weakness=f"pre-existing manual proposal referencing {rank_id}",
        proposed_upgrade="n/a", risk_score="founder_gated", origin="manual")
    try:
        obs = observe.signal_governing_priority_needs_proposal(rank_id)
        check("no observation created when a proposal already references the rank (any status)",
              obs == [], str(obs))
    finally:
        _cleanup(p.proposal_id, "synthetic dedup test — cleaned up per test-artifact policy")


def test_live_governing_rank_already_proposed_is_not_duplicated() -> None:
    """Live proof against real current state: OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION
    already has real canonical proposals (seeded 2026-09-03), so the bridge
    must recognize that and do nothing -- proving NO_DUPLICATE_PROPOSAL_OR_JOB
    against genuine data, not just a synthetic fixture."""
    obs = observe.signal_governing_priority_needs_proposal("OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION")
    check("live Rank-1 id (already has real proposals) produces no new observation",
          obs == [], str(obs))


def test_live_rank1_state_is_founder_gated_open_not_actionable() -> None:
    """Live proof of the exact deadlock this repair closes: real Rank-1 has
    one REJECTED (terminal) low-risk proposal and one OBSERVED founder_gated
    proposal. The tri-state classification must report founder_gated_open
    (a real, unresolved gate) and NOT actionable and NOT terminal_only --
    this is what drives autonomous_cycle.py's authority_block instead of a
    misleading "none -- re-run later"."""
    st = observe.classify_governing_priority_proposals("OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION")
    check("live Rank-1 has 2 known matches", len(st.matches) == 2, str([p.proposal_id for p in st.matches]))
    check("live Rank-1 has zero actionable proposals (the low-risk one was rejected)", st.actionable == [])
    check("live Rank-1 has exactly the founder_gated proposal open",
          [p.proposal_id for p in st.founder_gated_open] == ["937a60b8-43d7-489c-b20a-645bc9879f10"],
          str([p.proposal_id for p in st.founder_gated_open]))
    check("live Rank-1 is NOT terminal_only (the founder_gated one is still open)", st.terminal_only is False)


# --------------------------------------------------------------------- #
# 3b. THE CORE DEADLOCK FIX: a rank whose ONLY proposal is rejected/terminal
# must NOT be treated as "has an existing proposal" -- the bridge must
# re-fire so the gap can be reconciled, not silently suppressed forever.
# --------------------------------------------------------------------- #
def test_rejected_only_proposal_does_not_suppress_actionable_gap() -> None:
    rank_id = _synthetic_rank_id()
    p = proposal_mod.create(
        observed_weakness=f"a delta for {rank_id} that will be rejected",
        proposed_upgrade="n/a", risk_score="low", origin="manual")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                          note="synthetic terminal-only test — deliberately rejected, not cleaned up early")
    try:
        st = observe.classify_governing_priority_proposals(rank_id)
        check("the rejected proposal is recorded as a match", len(st.matches) == 1, str(st.matches))
        check("the rejected proposal is not actionable", st.actionable == [])
        check("the rejected proposal is not an open founder_gated block either", st.founder_gated_open == [])
        check("terminal_only is True -- everything that exists is closed", st.terminal_only is True)

        obs = observe.signal_governing_priority_needs_proposal(rank_id)
        check("bridge signal RE-FIRES when the only proposal is rejected/terminal "
              "(the exact deadlock this repair closes)", len(obs) == 1, str(obs))
    finally:
        pass  # already REJECTED (terminal) -- _cleanup() would be a no-op; nothing further to close


# --------------------------------------------------------------------- #
# 4. end-to-end through observe.run(): exactly one founder_gated proposal
# --------------------------------------------------------------------- #
def test_run_creates_exactly_one_founder_gated_proposal_end_to_end() -> None:
    rank_id = _synthetic_rank_id()
    before_ids = {p.proposal_id for p in proposal_mod.list_all()}
    report = observe.run(auto_propose=True, governing_priority_id=rank_id)
    after = {p.proposal_id: p for p in proposal_mod.list_all()}
    new_ids = set(after) - before_ids
    matching = [after[i] for i in new_ids if rank_id in (after[i].observed_weakness or "")]
    try:
        check("exactly one new canonical proposal created for the unproposed rank",
              len(matching) == 1, f"new_ids={new_ids}")
        if matching:
            p = matching[0]
            check("created proposal is founder_gated", p.risk_score == "founder_gated", p.risk_score)
            check("created proposal origin is observe_engine", p.origin == "observe_engine", p.origin)
            check("proposal_id is recorded in the observe report", p.proposal_id in report.proposals_created,
                  str(report.proposals_created))
    finally:
        for p in matching:
            _cleanup(p.proposal_id, "synthetic end-to-end bridge test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 5. idempotent re-entry: once a proposal exists, re-entry creates nothing
# more. Uses the signal function directly (not the full observe.run(), which
# is expensive against this project's large accumulated history -- ~40s per
# call, see test 4) so this stays fast while still proving the same thing:
# a second natural cycle after a crash/restart must never double-propose.
# --------------------------------------------------------------------- #
def test_rerun_is_idempotent_no_duplicate_proposal() -> None:
    rank_id = _synthetic_rank_id()
    obs1 = observe.signal_governing_priority_needs_proposal(rank_id)
    check("first pass finds the rank unproposed", len(obs1) == 1, str(obs1))
    if not obs1:
        return
    p = proposal_mod.create(
        observed_weakness=obs1[0].description, proposed_upgrade="review and scope",
        risk_score=obs1[0].suggested_risk_score, origin="observe_engine",
        fingerprint=obs1[0].dedupe_key[0])
    try:
        obs2 = observe.signal_governing_priority_needs_proposal(rank_id)
        check("second pass (proposal now exists) creates nothing new -- idempotent re-entry",
              obs2 == [], str(obs2))
    finally:
        _cleanup(p.proposal_id, "synthetic idempotency test — cleaned up per test-artifact policy")


# --------------------------------------------------------------------- #
# 6. an already-scoped LOW proposal still auto-advances without Founder input
# --------------------------------------------------------------------- #
def test_ordinary_low_risk_proposal_still_eligible_without_founder_gate() -> None:
    """The bridge only ever adds founder_gated proposals for un-scoped
    priorities -- it must not change eligibility for an ordinary,
    already-scoped low-risk proposal (e.g. one a human properly scoped, the
    same way the real OmniSim omnisim_loop.py delta was seeded)."""
    p = proposal_mod.create(
        observed_weakness="synthetic ordinary low-risk delta, already scoped",
        proposed_upgrade="do the small ordinary thing", risk_score="low", origin="manual")
    try:
        ok, reason = advance._eligible(p)
        check("an ordinary low-risk proposal remains auto-eligible (no Founder keep-going required)",
              ok is True, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic ordinary-eligibility test — cleaned up per test-artifact policy")


def test_no_duplicate_proposal_when_actionable_low_risk_proposal_exists() -> None:
    """A rank with an actionable (low-risk, open) proposal must classify as
    actionable, must NOT get a redundant authority-block trigger, and the
    bridge signal must not duplicate it."""
    rank_id = _synthetic_rank_id()
    p = proposal_mod.create(
        observed_weakness=f"an already-scoped, actionable low-risk delta for {rank_id}",
        proposed_upgrade="n/a", risk_score="low", origin="manual")
    try:
        st = observe.classify_governing_priority_proposals(rank_id)
        check("the actionable proposal is classified as actionable", [x.proposal_id for x in st.actionable] == [p.proposal_id])
        check("an actionable match never counts as a founder_gated block", st.founder_gated_open == [])
        check("an actionable match is never terminal_only", st.terminal_only is False)
        obs = observe.signal_governing_priority_needs_proposal(rank_id)
        check("bridge signal creates nothing new while an actionable proposal exists", obs == [], str(obs))
    finally:
        _cleanup(p.proposal_id, "synthetic actionable-dedup test — cleaned up per test-artifact policy")


def test_founder_gated_bridge_proposal_never_auto_eligible() -> None:
    rank_id = _synthetic_rank_id()
    obs = observe.signal_governing_priority_needs_proposal(rank_id)
    p = proposal_mod.create(
        observed_weakness=obs[0].description, proposed_upgrade="review and scope",
        risk_score=obs[0].suggested_risk_score, origin="observe_engine", fingerprint=obs[0].dedupe_key[0])
    try:
        ok, reason = advance._eligible(p)
        check("a bridge-created founder_gated proposal is never auto-eligible (protected gate preserved)",
              ok is False, reason)
    finally:
        _cleanup(p.proposal_id, "synthetic protected-gate test — cleaned up per test-artifact policy")


def test_eligibility_semantics_shared_with_advance_not_duplicated() -> None:
    """No second risk/eligibility policy: classify_governing_priority_proposals
    must call the literal SAME function object evolution/advance.py's own
    advance_eligible() uses, not a re-implementation that could drift."""
    check("observe.py's advance_mod._eligible IS the real evolution.advance._eligible function",
          observe.advance_mod._eligible is advance._eligible)
    code = _code_only(observe.classify_governing_priority_proposals)
    check("classify_governing_priority_proposals delegates eligibility to advance_mod._eligible, "
          "does not re-derive risk/status rules itself",
          "advance_mod._eligible(" in code, code)
    check("classify_governing_priority_proposals does not hardcode its own risk_score comparison "
          "(that belongs to advance_mod._eligible alone)",
          'risk_score ==' not in code and 'risk_score !=' not in code, code)


def test_founder_gated_only_match_would_surface_truthful_authority_block() -> None:
    """Proves the exact trigger condition autonomous_cycle.py uses
    (gov_state.founder_gated_open and not gov_state.actionable) against a
    synthetic founder_gated-only rank -- the condition under which run_cycle()
    appends a real authority_block instead of reporting a misleading
    "none -- re-run later". Does not call run_cycle() itself (see module
    docstring); the wiring itself is proven by source inspection below."""
    rank_id = _synthetic_rank_id()
    p = proposal_mod.create(
        observed_weakness=f"an already-scoped but founder_gated delta for {rank_id}",
        proposed_upgrade="n/a", risk_score="founder_gated", origin="manual")
    try:
        st = observe.classify_governing_priority_proposals(rank_id)
        would_block = bool(st.founder_gated_open) and not st.actionable
        check("a founder_gated-only match sets the exact condition run_cycle() uses "
              "to append a truthful authority_block", would_block is True, str(st))
        check("the block would name the real proposal_id",
              st.founder_gated_open[0].proposal_id == p.proposal_id)
    finally:
        _cleanup(p.proposal_id, "synthetic authority-block test — cleaned up per test-artifact policy")


def test_no_silent_gate_downgrade() -> None:
    """classify_governing_priority_proposals and the bridge signal are
    read-only -- neither may ever advance/approve/mutate a proposal's own
    status or risk_score (that would be an auto-downgrade of a real Founder
    gate, exactly what this repair must never do)."""
    for fn in (observe.classify_governing_priority_proposals, observe.signal_governing_priority_needs_proposal):
        code = _code_only(fn)
        check(f"{fn.__name__} never calls proposal_mod.advance()/save() (no gate downgrade)",
              "proposal_mod.advance(" not in code and ".save(" not in code, code)
        check(f"{fn.__name__} never assigns .risk_score or .status on a proposal",
              ".risk_score =" not in code and ".status =" not in code, code)
    rank_id = _synthetic_rank_id()
    p = proposal_mod.create(observed_weakness=f"gate for {rank_id}", proposed_upgrade="n/a",
                             risk_score="founder_gated", origin="manual")
    try:
        before = (p.status, p.risk_score)
        observe.classify_governing_priority_proposals(rank_id)
        observe.signal_governing_priority_needs_proposal(rank_id)
        after_p = proposal_mod.load(p.proposal_id)
        check("the founder_gated proposal's status/risk_score are byte-for-byte unchanged after classification",
              (after_p.status, after_p.risk_score) == before, f"{before} -> {(after_p.status, after_p.risk_score)}")
    finally:
        _cleanup(p.proposal_id, "synthetic no-downgrade test — cleaned up per test-artifact policy")


def test_no_direct_priority_state_mutation() -> None:
    """The bridge only ever READS founder_priority_state.json (via the
    existing governor, unchanged) and never writes it -- classifying
    proposals must not touch priority truth-state at all."""
    state_path = Path("/opt/pulse5-core/mr_silent_spine/state/founder_priority_state.json")
    before = state_path.read_bytes()
    observe.classify_governing_priority_proposals("OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION")
    observe.signal_governing_priority_needs_proposal("OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION")
    autonomous_cycle._consume_founder_top10()
    check("founder_priority_state.json is byte-for-byte unchanged after the full bridge read path",
          state_path.read_bytes() == before)


# --------------------------------------------------------------------- #
# 7. no competing scheduler / no duplicate selector invocation
# --------------------------------------------------------------------- #
def test_bridge_signal_invokes_no_selector_no_subprocess() -> None:
    for fn in (observe.signal_governing_priority_needs_proposal, observe.classify_governing_priority_proposals):
        code = _code_only(fn)
        check(f"{fn.__name__} does not invoke the multi_goal_arbiter selector",
              "multi_goal_arbiter" not in code and "multi_goal_selection" not in code, code)
        check(f"{fn.__name__} does not call the blending selector apply_governor",
              "apply_governor" not in code, code)
        check(f"{fn.__name__} does not spawn a subprocess or new scheduler",
              "subprocess" not in code and "Popen" not in code and "schedule" not in code.lower(), code)


def test_autonomous_cycle_threads_existing_governor_result_not_a_new_call() -> None:
    src = Path(autonomous_cycle.__file__).read_text()
    body = src.split("def run_cycle(", 1)[1]
    i_hook = body.index("_consume_founder_top10(")
    i_observe = body.index("observe_mod.run(")
    i_governing_arg = body.index("governing_priority_id=record.founder_top10.get(\"governing_id\")")
    check("Top-10 hook still runs before OBSERVE", i_hook < i_observe)
    check("OBSERVE is called with the id already computed by the hook (no second governor read)",
          i_hook < i_governing_arg < i_observe + 200, f"{i_hook} / {i_governing_arg} / {i_observe}")
    check("run_cycle() does not call choose_governing_rank a second time itself",
          "choose_governing_rank" not in body)
    i_classify = body.index("observe_mod.classify_governing_priority_proposals(")
    check("run_cycle() reuses classify_governing_priority_proposals (single source of truth), "
          "does not re-implement the actionable/founder_gated/terminal split itself",
          i_observe < i_classify, f"{i_observe} / {i_classify}")
    check("run_cycle() never calls proposal_mod.advance()/approves a proposal itself (no silent gate downgrade)",
          "proposal_mod.advance(" not in body[i_classify:i_classify + 1500])
    i_authblock = body.index("record.authority_blocks.append({", i_classify)
    check("the authority_block append is gated on founder_gated_open AND NOT actionable "
          "(never fires while real forward progress exists)",
          "gov_state.founder_gated_open and not gov_state.actionable" in body[i_classify:i_authblock + 50],
          body[i_classify:i_authblock + 200])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"raised {e!r}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nALL {len(tests)} TESTS PASSED")
