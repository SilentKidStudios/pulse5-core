"""
Proposal <-> Work Graph Bridge — WALK-AWAY CONVERGENCE live-integration
phase (2026-09-04): closes the gap between the canonical evolution/
proposal.py store (existing authority for "what work exists") and the
work_graph.py/scheduler.py concurrent-dispatch layer already landed on
main this same campaign.

ARCHITECTURE, deliberate and precise:

WorkGraph creates NO new notion of "work". Every WorkItem here corresponds
to exactly one existing evolution.proposal.Proposal, upserted idempotently
keyed by proposal_id (workitem_id_for_proposal). Calling
upsert_workitems_from_proposals() every cycle NEVER creates a duplicate
WorkItem for the same proposal, and NEVER resets an already-RUNNING,
COMPLETED, or TERMINAL_FAILED item's state back to RUNNABLE merely because
the proposal store still shows it open (see the guard inside that
function) — this is what makes population idempotent and restart-safe.

Execution is handed off to evolution.advance.advance_one() — the EXACT
SAME single-proposal advancement function evolution/advance.py::
advance_eligible()'s existing serial `for` loop already calls one
proposal at a time. This file creates ZERO second execution/governance
authority: make_advance_executor() returns a callable that does nothing
but call advance_one() and translate its real, documented
AdvancementResult (read directly from evolution/advance.py's source —
see _classify_advancement_result()'s docstring for the exact reasoning,
with file:line provenance) into a WorkGraph state transition.
scheduler.py's only real contribution is running several of these
CONCURRENTLY, subject to the same resource-admission and session-
ownership guards already proven for the synthetic scale harness — not a
second decision-maker.

TESTABILITY (2026-09-04): evolution/proposal.py and evolution/
founder_request.py both have ZERO non-stdlib imports and are exercised
here for REAL, fully tested (confirmed via direct grep of their imports).
evolution/advance.py — the actual executor — transitively imports several
files untracked in this repository (audit, campaign, capability_registry,
organ_discovery, validation -> env_util, omniengineer_harness ->
engine_identity) and cannot be imported from this isolated worktree — the
same wall that has blocked autonomous_cycle.py integration all campaign.
make_advance_executor() therefore accepts an injectable `advance_mod`
(defaults to a lazy real import at call time, which only succeeds where
evolution.advance's real dependencies are actually present — i.e. the
live checkout, not this worktree) so its WIRING/translation logic is
fully unit-tested here against a fake stand-in with the same field names;
it does not and cannot prove evolution.advance.advance_one()'s own real
runtime behavior — that remains evolution/advance.py's own test suite's
job (test_evolution_advance.py), which itself cannot collect in this
worktree for the identical untracked-dependency reason.
"""
from __future__ import annotations

from typing import Any, Callable

import work_graph as wg
from evolution import founder_request
from evolution import proposal as proposal_mod

WORK_ITEM_KIND = "proposal"

# blocked_reason values from evolution/advance.py::advance_one() that mean
# "try again later, nothing permanently wrong" — read directly from its
# source (the _finish(...) call sites for concurrent_advancer_blocked,
# prior_job_still_active, and no_engine_available_this_attempt), not
# guessed. Mapped to REPAIRABLE_FAILED so work_graph.retry_repairable_
# failed()'s existing bounded backoff/budget applies — no new retry
# policy invented here.
_TRANSIENT_BLOCKED_REASONS = frozenset({
    "concurrent_advancer_blocked",
    "prior_job_still_active",
    "no_engine_available_this_attempt",
})


def workitem_id_for_proposal(proposal_id: str) -> str:
    return f"proposal:{proposal_id}"


def _founder_gate_state(p: "proposal_mod.Proposal") -> tuple[bool, str | None]:
    """True + the resolved decision iff this proposal is currently blocked
    on a genuine, unresolved Founder gate. Uses founder_request.exact_
    proposal_decision()'s exact-proposal-id matching — never fuzzy, never
    inherited from a different proposal (see that function's own
    docstring: "an approval recorded for one proposal can never be
    mistaken for approval of a different one").

    PROPOSAL COMPLETENESS GATE (2026-09-07): a founder_gated proposal that
    is not yet decision-ready (evolution/proposal.py::
    proposal_completeness()) is NEVER reported as Founder-gated here —
    see _needs_refinement() below for where it goes instead. This is what
    keeps an incomplete, unscoped proposal (the real example:
    8d0d01cb-d2f5-4638-b5f5-3b3945de07bf) out of the Founder-visible
    blocked_founder rollup entirely, until it actually has something in it
    worth a Founder's attention."""
    if p.risk_score != "founder_gated":
        return False, None
    if not proposal_mod.is_decision_ready(p):
        return False, None
    decision = founder_request.exact_proposal_decision(p.proposal_id)
    return decision != "approved", decision


def _needs_refinement(p: "proposal_mod.Proposal") -> bool:
    """True iff this proposal would otherwise be Founder-gated but is not
    yet decision-ready — the WorkItem for it should be parked in
    BLOCKED_REFINEMENT (never BLOCKED_FOUNDER, never RUNNABLE) until
    evolution/proposal.py::refine() makes it complete."""
    return p.risk_score == "founder_gated" and not proposal_mod.is_decision_ready(p)


def upsert_workitems_from_proposals(
    *, self_session_id: str = "proposal-bridge",
    reconcile_orphaned_canary_fn: Callable[[str], Any] | None = None,
) -> dict[str, int]:
    """LIVE_WORKITEM_POPULATION, idempotent and restart-safe: one WorkItem
    per non-closed proposal (CLOSED_STATUSES == promoted/rolled_back/
    rejected — see proposal.py). Safe to call every autonomous cycle:

      - a proposal with no existing WorkItem gets one created RUNNABLE (or
        BLOCKED_DEFERRED / BLOCKED_FOUNDER if a genuine reason applies);
      - a proposal whose WorkItem already exists and is RUNNING,
        COMPLETED, or TERMINAL_FAILED is left completely untouched — this
        function never re-arms or duplicates in-flight/finished work;
      - a proposal whose WorkItem is RUNNABLE/BLOCKED_FOUNDER/
        BLOCKED_DEFERRED/BLOCKED_DEPENDENCY/BLOCKED_EXTERNAL_SESSION/
        REPAIRABLE_FAILED gets its deferred/Founder-gate status
        re-evaluated every cycle (a status change recorded since the last
        check correctly lifts BLOCKED_DEFERRED/BLOCKED_FOUNDER with no
        manual step — see the "released" branch below).

    DEFERRED_CHURN gap-closure (2026-09-06): evolution.advance.advance_one()
    only ever accepts OBSERVED/PROPOSED as an eligible starting status
    (_eligible()) — a DEFERRED proposal is NEVER auto-eligible, by design
    (defer() is explicitly "not auto-actionable... only a deliberate human
    or reactive pursuit un-defers this", see proposal.py). Before this fix,
    a DEFERRED proposal still got a RUNNABLE WorkItem exactly like an open
    one, so the scheduler dispatched it every cycle only for advance_one()
    to reject it and work_graph's retry-repair budget to churn on it
    forever for work that can never actually advance. DEFERRED now takes
    precedence over the Founder-gate check (a deferred proposal can't
    advance regardless of risk_score) and parks the WorkItem in
    BLOCKED_DEFERRED instead — never RUNNABLE, so it consumes zero retry
    budget and dispatches zero scheduler passes. Re-evaluated fresh every
    cycle from proposal_mod.list_all() (always a disk re-read), so the
    moment something explicitly changes the proposal's status away from
    DEFERRED, the very next population pass naturally releases it back to
    RUNNABLE (or BLOCKED_FOUNDER, if still separately gated) — no separate
    "reopen" code path needed; provenance/history is never touched or lost.

    owner_path is set from the proposal's own source_paths (real,
    explicitly-authorized canonical paths a caller named — see proposal.py
    field docstring) when present, so session_ownership's path-collision
    protection actually applies to proposal-backed work that touches
    specific files.

    Returns counts for observability, never raises for an individual
    malformed/missing proposal — this must be safe to call unattended
    every cycle."""
    created = 0
    updated = 0
    left_untouched = 0
    reconciled_canary = 0
    for p in proposal_mod.list_all():
        if p.status in proposal_mod.CLOSED_STATUSES:
            continue  # closed proposals are history, not schedulable work

        # CANARY_RECONCILIATION gap-closure (2026-09-08): a proposal orphaned
        # at status=canary is durably known-good (see evolution/advance.py::
        # reconcile_orphaned_canary()'s docstring for why CANARY specifically
        # is always a genuine passed canary, never a failure) — resume it to
        # PROMOTION_CANDIDATE + Founder communication BEFORE the ORPHANED_
        # STATUS classification below, which would otherwise just keep
        # re-parking it in BLOCKED_FOUNDER forever with the completed work
        # never surfaced to a human. Injectable exactly like make_advance_
        # executor()'s advance_mod, for the same reason (evolution.advance's
        # untracked transitive deps can't import in this isolated worktree) —
        # a real caller gets a lazy real import; tests inject a fake. Reads
        # p fresh from disk afterward since reconciliation may have changed
        # its status out from under the stale in-loop copy. No-op (and safe
        # to call every cycle) for a proposal not currently at CANARY.
        if p.status == proposal_mod.ProposalStatus.CANARY:
            fn = reconcile_orphaned_canary_fn
            if fn is None:
                from evolution import advance as _advance_mod  # noqa: intentional lazy real import
                fn = _advance_mod.reconcile_orphaned_canary
            result = fn(p.proposal_id)
            if getattr(result, "final_status", None) != proposal_mod.ProposalStatus.CANARY:
                reconciled_canary += 1
            p = proposal_mod.load(p.proposal_id)
            if p.status in proposal_mod.CLOSED_STATUSES:
                continue

        work_id = workitem_id_for_proposal(p.proposal_id)
        existing = wg.load(work_id)
        deferred = p.status == proposal_mod.ProposalStatus.DEFERRED
        gated, decision = _founder_gate_state(p)
        needs_refinement = _needs_refinement(p)
        owner_path = p.source_paths[0] if p.source_paths else None

        # Precedence, most-restrictive first: DEFERRED > ORPHANED_STATUS >
        # REFINEMENT > FOUNDER > None. A deferred proposal can never advance
        # regardless of risk_score.
        #
        # ORPHANED_STATUS gap-closure (2026-09-08, same class of bug as
        # DEFERRED_CHURN above but for a different status family): evolution/
        # advance.py::_eligible() only ever accepts status in {observed,
        # proposed} as a fresh advance_one() starting point. A proposal left
        # at implemented/tested/canary/promotion_candidate — either orphaned
        # mid-flight by a prior advance() call that was interrupted before
        # reaching promotion_candidate, or genuinely already terminal and
        # awaiting Founder promotion — was, before this fix, indistinguishable
        # from an ordinary open proposal here: it got a RUNNABLE WorkItem,
        # the scheduler dispatched it, advance_one() returned the SAME
        # deterministic blocked_reason every time ("status=X is not an
        # eligible starting point"), make_advance_executor() turned that into
        # a bare `raise RuntimeError(...)`, and failure_diagnosis.py's closed
        # exception-CLASS taxonomy (blind to the message) misdiagnosed it as
        # DIAGNOSIS_TRANSIENT — burning a scheduler dispatch slot on
        # deterministically-doomed work every cycle, forever. Real evidence:
        # proposals 83c43e59-fcfb-4b3f-ab0c-29f2dba8d17b and c3b3c10a-dabf-
        # 4b92-923b-6039cbb67ad3, both orphaned at status=canary, both stuck
        # in exactly this loop across natural cycles on 2026-09-08. Routed to
        # BLOCKED_FOUNDER (source="orphaned_status", see mark_blocked_
        # founder()) rather than a new state: it genuinely does need a human
        # to look at it (resume/reset the proposal, or promote it), and the
        # release-branch fix below ensures it — like a repair-budget
        # escalation — is never auto-released just because this proposal's
        # own founder-gate check happens to be False.
        #
        # An incomplete founder_gated proposal is never usefully
        # BLOCKED_FOUNDER (nothing is there yet for a Founder to review) --
        # see _needs_refinement(). One target blocked state per pass, never
        # flickering between them across cycles.
        orphaned_mid_flight = not deferred and p.status not in (
            proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED,
        )
        blocked_founder_source = None
        if deferred:
            target_blocked_state = wg.WorkState.BLOCKED_DEFERRED
            block_note = f"status=deferred, defer_reason={p.defer_reason!r}"
        elif orphaned_mid_flight:
            target_blocked_state = wg.WorkState.BLOCKED_FOUNDER
            blocked_founder_source = "orphaned_status"
            block_note = (
                f"status={p.status!r} is not a fresh advance_one() starting point (only "
                "observed/proposed are) -- mid-flight and orphaned by an interrupted prior "
                "advance, or already terminal (promotion_candidate) and awaiting Founder "
                "promotion; needs Founder/stewardship review, never eligible for blind re-dispatch"
            )
        elif needs_refinement:
            target_blocked_state = wg.WorkState.BLOCKED_REFINEMENT
            _complete, missing = proposal_mod.proposal_completeness(p)
            block_note = f"risk_score=founder_gated but not decision-ready, missing={missing}"
        elif gated:
            target_blocked_state = wg.WorkState.BLOCKED_FOUNDER
            blocked_founder_source = "founder_gate"
            block_note = f"risk_score=founder_gated, decision={decision}"
        else:
            target_blocked_state = None
            block_note = ""

        def _apply_blocked_state(wid: str) -> None:
            if target_blocked_state == wg.WorkState.BLOCKED_DEFERRED:
                wg.mark_blocked_deferred(wid, note=block_note)
            elif target_blocked_state == wg.WorkState.BLOCKED_REFINEMENT:
                wg.mark_blocked_refinement(wid, note=block_note)
            elif target_blocked_state == wg.WorkState.BLOCKED_FOUNDER:
                wg.mark_blocked_founder(wid, note=block_note, source=blocked_founder_source)

        if existing is None:
            wg.create(
                work_id, kind=WORK_ITEM_KIND, description=p.observed_weakness,
                owner_path=owner_path,
                provenance={"proposal_id": p.proposal_id, "created_because": "eligible open proposal"},
            )
            if target_blocked_state is not None:
                _apply_blocked_state(work_id)
            created += 1
            continue

        if existing.state in (wg.WorkState.RUNNING, wg.WorkState.COMPLETED, wg.WorkState.TERMINAL_FAILED):
            left_untouched += 1
            continue

        if target_blocked_state is not None:
            if existing.state != target_blocked_state:
                _apply_blocked_state(work_id)
                updated += 1
            else:
                left_untouched += 1
        elif existing.state in (wg.WorkState.BLOCKED_DEFERRED, wg.WorkState.BLOCKED_REFINEMENT):
            # none of deferred/needs-refinement anymore -- release back to runnable
            record = wg.load(work_id)
            record.state = wg.WorkState.RUNNABLE
            released_reason = ("proposal status changed away from deferred"
                                if existing.state == wg.WorkState.BLOCKED_DEFERRED
                                else "proposal became decision-ready")
            wg._save(record, note=f"{released_reason} — released")
            updated += 1
        elif existing.state == wg.WorkState.BLOCKED_FOUNDER:
            # RELEASE-CONFLATION fix (2026-09-08): BLOCKED_FOUNDER has two
            # semantically distinct causes -- a genuine, resolvable
            # founder-gate decision (blocked_founder_source="founder_gate",
            # the only case THIS function ever marks that way) versus an
            # escalation (blocked_founder_source="repair_budget_exhausted"
            # or "orphaned_status") that has nothing to do with this
            # proposal's own founder-gate/decision state. Before this fix,
            # every BLOCKED_FOUNDER was unconditionally released the moment
            # `gated` came back False for this proposal -- silently
            # defeating retry_repairable_failed()'s own "escalate instead of
            # quietly vanishing" contract for any non-founder_gated proposal
            # exhausting its repair budget (see the real incident this
            # closed, in the ORPHANED_STATUS comment above). Only a real
            # founder-gate source is safe to auto-release this way; any
            # escalation source requires a separate, real resolving action
            # (proposal status genuinely changing) before it naturally
            # re-enters this same population pass's normal precedence above.
            record = wg.load(work_id)
            if record.provenance.get("blocked_founder_source") == "founder_gate":
                record.state = wg.WorkState.RUNNABLE
                wg._save(record, note=f"Founder decision resolved ({decision}) — released")
                updated += 1
            else:
                left_untouched += 1
        else:
            left_untouched += 1

    return {"created": created, "updated": updated, "left_untouched": left_untouched,
            "reconciled_canary": reconciled_canary}


def _classify_advancement_result(result: Any) -> dict[str, Any]:
    """Pure translation of a real evolution.advance.AdvancementResult (or
    an equivalent stand-in with the same field names, for testing) into
    {"terminal": "completed"|"failed"|"repairable", ...evidence}. Read
    directly from evolution/advance.py's source (advance_one()'s multiple
    _finish(...) call sites, 2026-09-04) — not guessed:

      - final_status == "promotion_candidate" -> the proposal reached the
        terminal ready-for-Founder-promotion state; WorkGraph's job is
        done (actual production promotion remains a separate, explicitly
        Founder-gated `cli.py promote ... --founder-approved` action,
        completely untouched by this).
      - final_status == "rejected" -> permanently closed by advance_one()
        itself (policy rejection, exhausted implementation attempts,
        failed validation/canary/validator-disagreement) — not repairable
        via retry, since proposal.py's CLOSED_STATUSES will refuse to
        re-advance it anyway.
      - blocked_reason in _TRANSIENT_BLOCKED_REASONS -> try again later,
        nothing permanently wrong; maps to "repairable" so the existing
        bounded work_graph.retry_repairable_failed() budget/backoff
        applies — no new retry policy invented here.
      - anything else unrecognized -> fails closed to "repairable" rather
        than ever assuming success it cannot verify."""
    final_status = getattr(result, "final_status", None)
    blocked_reason = getattr(result, "blocked_reason", None)
    evidence = {
        "proposal_id": getattr(result, "proposal_id", None),
        "final_status": final_status,
        "blocked_reason": blocked_reason,
        "implementation_job_id": getattr(result, "implementation_job_id", None),
        "selected_engine": getattr(result, "selected_engine", None),
    }
    if final_status == "promotion_candidate":
        return {"terminal": "completed", **evidence}
    if final_status == "rejected":
        return {"terminal": "failed", **evidence}
    if blocked_reason in _TRANSIENT_BLOCKED_REASONS:
        return {"terminal": "repairable", **evidence}
    return {"terminal": "repairable", **evidence}  # fail closed: never silently "completed" without verified evidence


class NotAProposalWorkItem(ValueError):
    """Raised when the executor is handed a WorkItem with no proposal_id
    in its provenance — a programming error (this bridge's population
    function always sets it), never a normal runtime outcome."""


def make_advance_executor(advance_mod: Any = None) -> Callable[[wg.WorkItem], dict[str, Any]]:
    """Returns an executor_fn matching scheduler.run_scheduler_pass()'s
    contract. Accepts an injectable advance_mod for testing (see module
    docstring); a real caller passes nothing and gets a lazy `from
    evolution import advance`, which only succeeds where its real
    untracked dependencies exist on disk. The returned dict carries
    "__terminal_failed__": True for a genuinely closed/rejected proposal
    (see scheduler.py's handling of that key) rather than raising — a
    raised exception would route through work_graph.mark_repairable_
    failed(), which is wrong for a proposal advance_one() has already
    permanently rejected."""
    def _executor(item: wg.WorkItem) -> dict[str, Any]:
        nonlocal advance_mod
        if advance_mod is None:
            from evolution import advance as advance_mod  # noqa: intentional lazy real import
        proposal_id = item.provenance.get("proposal_id")
        if not proposal_id:
            raise NotAProposalWorkItem(
                f"work item {item.work_id!r} has no proposal_id in provenance — not a proposal-backed item"
            )
        result = advance_mod.advance_one(proposal_id, requested_by="scheduler_cycle_hook")
        classified = _classify_advancement_result(result)
        if classified["terminal"] == "failed":
            return {**classified, "__terminal_failed__": True}
        if classified["terminal"] == "repairable":
            raise RuntimeError(f"proposal {proposal_id} not yet advanceable: {classified.get('blocked_reason')}")
        return classified
    return _executor
