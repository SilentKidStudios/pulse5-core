"""
Continuous Stewardship — WALK-AWAY CONVERGENCE macro campaign, Phase 4
(2026-09-05): "one proposal/mission finished" must never be interpreted as
"the Studio is finished."

ARCHITECTURE: this module makes exactly one decision, every natural
cycle, and never dispatches anything itself (that remains scheduler.py's
job): is there real, currently-actionable work? If yes, do nothing further
— the scheduler's own next pass already handles it, and stewardship must
never preempt or duplicate that. If no, and a legitimate next governing
priority is available (via a pluggable, injectable source — the real
canonical source would be the Founder Top-10 priority governor / OmniRegistry
stewardship selection, both untracked and unavailable from this isolated
worktree, same deferred-wiring pattern as work_discovery.py's Studio
source), register it as a new Mission. If neither, persist that truthful
idle state — never fabricate work merely to appear busy (campaign Phase 4's
own explicit constraint).
"""
from __future__ import annotations

from typing import Any, Callable

import mission
import work_graph as wg


def reconcile_all_missions() -> dict[str, str]:
    """Reconciles truth before any selection decision: calls mission.
    sync_mission_state() for every registered mission. Returns
    {mission_id: resulting_state}."""
    return {m.mission_id: mission.sync_mission_state(m.mission_id).state for m in mission.list_all()}


def has_actionable_work(self_session_id: str = "stewardship") -> bool:
    """True iff at least one MISSION-OWNED WorkItem is genuinely runnable
    right now — scoped to work this reconciliation pass actually owns
    (provenance["mission_id"] set), never the whole WorkGraph.

    WORKGRAPH_IDLE_LIVENESS fix (2026-09-06): the original scope here was
    the entire WorkGraph (bool(wg.runnable_items(...))), which meant
    mission creation could structurally never fire while ANY unrelated
    work existed — proven unreachable in practice by real natural-cycle
    evidence this campaign gathered (this Studio's ordinary proposal-
    backed backlog keeps 90-120 items genuinely RUNNABLE essentially every
    cycle; at 2 dispatched/cycle under the cold-swap bound, draining it
    would take many hours even with zero new proposals arriving in the
    meantime, which never happens in practice). A stewardship pass must
    still never preempt or duplicate real proposal-backed work — scoping
    to mission-owned items achieves that identically (this still never
    dispatches or creates anything itself), while letting reconciliation
    run and a legitimate next priority be registered as a Mission even on
    a cycle where the ordinary backlog is busy — exactly the "lightweight
    control-plane phase, bounded cadence, does not seize scheduler
    capacity" property required, since mission creation costs nothing
    (create_mission()'s own fingerprint idempotency already guarantees
    calling it every cycle with the same governing goal never creates a
    second Mission or duplicates a WorkItem).

    Uses the exact same real, dependency- and ownership-aware eligibility
    check the scheduler itself uses (work_graph.runnable_items()) — never
    a second, looser notion of 'is there work' — just filtered to the
    subset this reconciliation pass actually owns."""
    return any(
        (item.provenance or {}).get("mission_id") is not None
        for item in wg.runnable_items(self_session_id=self_session_id)
    )


# REAL_WORK_CLOSURE session scope guard (2026-09-06): an explicit, narrow
# ALLOWLIST — not an exclusion list — of governing_id values this
# function is authorized to act on. Mechanically confirmed before this
# was added: the mission that already exists this session is Founder
# Top-10 RANK 1 (OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION, explicitly
# off-limits this session), and it ALREADY has real matching proposals
# (including one open founder_gated one) — without this guard,
# ensure_proposal_for_founder_priority_mission() would have linked that
# mission to real OmniSim proposal state the very first time it ran,
# violating the explicit "do not touch OmniSim" boundary. An allowlist
# (rather than trying to enumerate every forbidden id) fails closed by
# construction: a governing_id this session never explicitly reviewed and
# authorized is never acted on, regardless of what future Founder Top-10
# reordering might introduce. RANK 2 (MR_SILENT_APP_COMPLETION) is the
# one real, reviewed, authorized source for this session — selected per
# the exact exclusion order given: not OmniSim/Oracle (rank 1, excluded),
# not Scorpio (rank 6, excluded), not Spatial Avatar (not present in the
# canonical order at all), not owned by another active campaign, and
# mechanically confirmed via evolution.observe.classify_governing_
# priority_proposals() to have zero existing proposal representation at
# all (matches=0) — a real, genuine, currently-unaddressed gap.
#
# DOMAIN_B_FOUNDER_AUTHORIZATION (2026-09-08): explicit Founder decision
# widens this allowlist to ranks 2-10 of the SAME canonical Top-10 order,
# individually verified before adding — never "numbered 2-10, so add it":
# each of the 7 new entries below was checked directly against the live
# mission record (goal text scanned against authority_policy.GATED_
# KEYWORDS: zero hits on all 7; deferred_until: None on all 7; none
# terminal/complete/superseded). Rank 1 (OMNISIM_AND_ORACLE_STUDIO_WIDE_
# ACTIVATION) stays excluded — an explicit, separate, still-active Founder
# gate, untouched by this widening. Rank 6 (SCORPIOS_CORNER) ALSO stays
# excluded despite being numerically inside "2-10" — protected, a
# separate autonomy loop on render-forge-01, and (mechanically confirmed)
# has no Mission record at all; the Founder authorization itself named
# this exact exclusion ("individually confirmed NON-PROTECTED"), it is
# not a gap in applying it.
#
# UNCHANGED SEMANTICS, worth restating precisely (this is what the
# Founder authorization is actually scoped to): adding a governing_id
# here only ever lets ensure_proposal_for_founder_priority_mission()
# link an ALREADY-open/actionable proposal, or — when genuinely nothing
# exists yet — create exactly ONE bounded proposal, ALWAYS risk_score==
# "founder_gated" (see that function's own fallback branch below). Such a
# proposal can never auto-advance (evolution/advance.py::_eligible()
# requires an exact Founder approval for 'founder_gated'), never bypasses
# validation/canary, never touches production, credentials, paid
# resources, or remote execution — this allowlist governs Mission/WorkItem
# VISIBILITY and intake only, never execution authority.
AUTHORIZED_REAL_WORK_GOVERNING_IDS = frozenset({
    "MR_SILENT_APP_COMPLETION",  # rank 2, original authorization (2026-09-06)
    # ranks 3-10 below, minus rank 6 (SCORPIOS_CORNER, protected — excluded
    # by explicit Founder instruction even though numerically in range),
    # individually verified 2026-09-08 — see this module's own docstring
    # comment immediately above for the exact verification performed.
    "MR_SILENT_ADAPTIVE_EVOLUTION_AND_MULTI_BRAIN_WATCH",   # rank 3
    "OMNI_GOD_MODE_INTERFACE_MCP_CLI_CLOSED_LOOP",          # rank 4
    "SILENT_HANDS_WORKER_SWARM",                            # rank 5
    "AUTO_MR_SILENT_SELECTS",                                # rank 7
    "TH3S1L3NTK1D_STUDIOS_WEBSITE",                          # rank 8
    "CONTENT_JOURNEY_OMNIFORGE_PROVIDER_INTEGRATION",        # rank 9
    "PULSEWORLD_REAL_STATE_CONVERGENCE + 3D_4D_MR_SILENT_PHYSICAL_EMBODIMENT",  # rank 10
})


def ensure_proposal_for_founder_priority_mission(m: mission.Mission) -> dict[str, Any]:
    """REAL_WORK_CLOSURE (2026-09-06): the small adapter closing the real
    gap between a Founder-priority-sourced Mission (created by
    autonomous_cycle._founder_top10_priority_source(), provenance.source
    == "founder_top10_priority_governor") and actually-dispatchable work —
    WITHOUT inventing a new WorkItem kind or executor. Deliberately reuses
    THREE already-existing, already-tested, unmodified mechanisms instead
    of building a new one:

      1. evolution.observe.classify_governing_priority_proposals() — the
         exact same real, pure classifier already used every cycle for the
         GOVERNING rank's own founder_priority_unproposed OBSERVE signal
         (observe.py itself is explicitly untouched/withheld this session
         — this only ever CALLS its existing exported function, never
         modifies it);
      2. evolution.proposal.create()/find_open_by_fingerprint() — the
         canonical proposal store, with the SAME founder_gated risk
         classification and dedupe-key convention
         signal_governing_priority_needs_proposal() already uses for the
         governing rank, applied here to this mission's own priority id;
      3. proposal_work_bridge.py's existing "proposal:<id>" WorkItem
         convention and REAL, already-proven advance_one() executor — the
         resulting WorkItem is populated by the population pass that
         ALREADY runs every cycle, with ZERO new dispatch-kind risk (this
         never creates a WorkItem itself; mission.attach_root_work_item()
         only records the eventual "proposal:<id>" reference so mission
         reconciliation tracks it once population materializes it).

    A new proposal this creates is ALWAYS founder_gated — exactly like the
    existing governing-rank signal — so it can never auto-implement
    without an explicit Founder decision; this closes zero governance
    gates, it only makes a real, previously-invisible gap (a Founder
    priority with no representation in the proposal store at all)
    visible and durably tracked, the same way the pre-existing signal
    already does for whichever rank happens to be governing.

    Idempotent and safe to call every cycle: if an actionable or open
    founder_gated proposal already exists for this priority, it is linked
    (never duplicated); if a matching proposal exists but is fingerprint-
    deduped as already open, that one is reused; only when genuinely
    nothing exists or every match is terminal does this create anything
    new, mirroring signal_governing_priority_needs_proposal()'s exact
    "genuinely nothing to wait on" condition."""
    governing_id = (m.provenance or {}).get("governing_id")
    if not governing_id:
        return {"applicable": False, "reason": "mission has no governing_id provenance"}
    if governing_id not in AUTHORIZED_REAL_WORK_GOVERNING_IDS:
        return {"applicable": False, "reason": f"governing_id {governing_id!r} is not in this session's authorized allowlist"}

    import evolution.observe as observe
    from evolution import proposal as proposal_mod

    state = observe.classify_governing_priority_proposals(governing_id)
    linkable = state.actionable + state.founder_gated_open
    if linkable:
        p = linkable[0]
        work_id = f"proposal:{p.proposal_id}"
        already_linked = work_id in m.root_work_item_ids
        mission.attach_root_work_item(m.mission_id, work_id, note=f"linked existing open proposal {p.proposal_id}")
        return {"applicable": True, "action": "noop" if already_linked else "linked_existing", "proposal_id": p.proposal_id}

    if not state.matches or state.terminal_only:
        fp = f"founder_priority_unproposed:{governing_id}"
        existing_open = proposal_mod.find_open_by_fingerprint(fp)
        if existing_open is not None:
            work_id = f"proposal:{existing_open.proposal_id}"
            already_linked = work_id in m.root_work_item_ids
            mission.attach_root_work_item(m.mission_id, work_id, note=f"linked deduped proposal {existing_open.proposal_id}")
            return {"applicable": True, "action": "noop" if already_linked else "linked_existing_deduped",
                    "proposal_id": existing_open.proposal_id}

        p = proposal_mod.create(
            observed_weakness=(
                f"Founder Top-10 governing priority '{governing_id}' has no actionable or pending proposal"
                + (f" (its only {len(state.matches)} prior proposal(s) are all terminal/closed)" if state.matches else "")
            ),
            proposed_upgrade=(
                "Founder/human review required to scope this strategic priority into an actionable, "
                "bounded engineering delta — this proposal deliberately does not scope itself."
            ),
            risk_score="founder_gated", origin="discovered", fingerprint=fp,
        )
        mission.attach_root_work_item(m.mission_id, f"proposal:{p.proposal_id}",
                                       note=f"created new proposal {p.proposal_id} for unaddressed governing priority")
        return {"applicable": True, "action": "created_new", "proposal_id": p.proposal_id}

    return {"applicable": True, "action": "noop", "reason": "matches exist but none linkable (all closed, none actionable/open)"}


def continuous_stewardship_pass(
    *,
    self_session_id: str = "stewardship",
    next_priority_source: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """The Phase 4 reconciliation, safe to call every natural cycle:

      1. reconcile every registered mission's real state;
      1b. REAL_WORK_CLOSURE (2026-09-06): for every ACTIVE, Founder-
          priority-sourced mission that has no root_work_item_ids yet,
          materialize real work for it via ensure_proposal_for_founder_
          priority_mission() — see that function's own docstring. Runs
          unconditionally, regardless of step 2 below: it never dispatches
          or preempts anything itself (only creates/links a durable
          proposal record; actual dispatch remains scheduler.py's own job
          on a later pass), so it is exactly as safe to run every cycle as
          reconcile_all_missions() itself;
      2. if genuinely actionable work already exists, stop here — a
         stewardship pass never dispatches or preempts;
      3. if nothing is actionable and a next_priority_source is
         configured, ask it for the next legitimate registered governing
         priority and durably register it as a new Mission (idempotent —
         mission.create_mission()'s own fingerprint dedup means the same
         priority text returned twice never creates two Missions);
      4. if nothing is actionable and no source is available or it finds
         nothing legitimate, persist that truthful idle state — NOT a
         failure, and never papered over with fabricated work."""
    reconciled = reconcile_all_missions()

    work_ensured: dict[str, Any] = {}
    for mid, state in reconciled.items():
        if state != mission.MissionState.ACTIVE:
            continue
        m = mission.load(mid)
        if m is None or m.root_work_item_ids:
            continue
        try:
            work_ensured[mid] = ensure_proposal_for_founder_priority_mission(m)
        except Exception as e:  # noqa: BLE001 — one broken mission's discovery must never break the whole pass
            work_ensured[mid] = {"applicable": True, "error": repr(e)}

    if has_actionable_work(self_session_id=self_session_id):
        return {"reconciled_missions": reconciled, "actionable_work_exists": True, "next_mission_created": None,
                "work_ensured": work_ensured}

    if next_priority_source is None:
        return {
            "reconciled_missions": reconciled, "actionable_work_exists": False, "next_mission_created": None,
            "work_ensured": work_ensured,
            "reason": "no next_priority_source configured this pass — idle, not a failure",
        }

    try:
        candidate = next_priority_source()
    except Exception as e:  # noqa: BLE001 — a broken priority source must never break stewardship itself
        return {
            "reconciled_missions": reconciled, "actionable_work_exists": False, "next_mission_created": None,
            "work_ensured": work_ensured, "error": repr(e),
        }

    if candidate is None:
        return {
            "reconciled_missions": reconciled, "actionable_work_exists": False, "next_mission_created": None,
            "work_ensured": work_ensured,
            "reason": "next_priority_source found nothing legitimate to pursue — idle, not a failure",
        }

    m = mission.create_mission(
        candidate["goal"], origin="discovered",
        priority=candidate.get("priority", 0.0), provenance=candidate.get("provenance", {}),
    )
    return {"reconciled_missions": reconciled, "actionable_work_exists": False, "next_mission_created": m.mission_id,
            "work_ensured": work_ensured}
