"""Founder Priority Backlog Discovery — Studio-wide Walk-Away, discovery
source #7 (2026-09-08).

Real canonical source: the SAME durable_priority_queue autonomous_cycle.py
already reads every cycle (mr_silent_spine/action_queue/ACTION-priority-
governor-<date>.json — path taken LIVE from autonomous_cycle.
_CANONICAL_ACTION_AUTHORITY_PATH, never hardcoded/duplicated here, so this
adapter never drifts if that dated canonical record is ever superseded).

autonomous_cycle.py's own _founder_top10_priority_source() (Phase U's
Mission feed) only ever surfaces the SINGLE current governing rank (rank
1) to Mission creation; _consume_founder_top10() separately reads and
durably records the FULL ranked order every cycle, but nothing feeds
ranks 2-13 to Mission creation. Confirmed live 2026-09-08: of the queue's
13 real entries, only rank 1 (OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION)
and rank 2 (MR_SILENT_APP_COMPLETION) have an existing Mission; ranks
3,4,5,7,8,9,10,11,12,13 have none — legitimate, Founder-ranked, currently
invisible Studio work.

Rank 6 (SCORPIOS_CORNER) is deliberately excluded from automatic Mission
creation here even though it also has no Mission record: Scorpio's Corner
runs its own separate, protected, dedicated autonomy loop (scorpio-
mrsilent-autonomy.service on render-forge-01), and this campaign's own
boundaries prohibit touching Scorpio production — creating a competing
tracking record for it is out of THIS campaign's scope, not a technical
gap. Excluded explicitly and durably, never silently.

create_missions_for_unrepresented_ranks() reuses mission.py's EXISTING
create_mission() (idempotent via mission_fingerprint) — never a parallel
roadmap/priority framework. Creating a bare Mission record creates no
WorkItem, dispatches nothing, and never engages the scheduler (see
mission.create_mission()'s own docstring / attach_root_work_item()) —
registration/visibility only, matching Phase U's own documented boundary
that priority-source Missions are never auto-decomposed here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import autonomous_cycle as ac
import mission

# Protected/separately-owned rank IDs this campaign must never create a
# competing Mission record for — see module docstring.
EXCLUDED_RANK_IDS = frozenset({"SCORPIOS_CORNER"})


@dataclass
class PriorityBacklogEntry:
    rank: int | None
    rank_id: str
    goal_text: str
    priority: float
    existing_mission_id: str | None
    excluded: bool


def read_full_priority_queue() -> list[dict[str, Any]]:
    """Real source: the SAME canonical file autonomous_cycle.py reads, at
    whatever path it currently resolves to. Never re-derives or caches a
    second copy of the path."""
    path = ac._CANONICAL_ACTION_AUTHORITY_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return raw.get("durable_priority_queue", [])


def discover_backlog() -> list[PriorityBacklogEntry]:
    """Cross-references every real queue entry against mission.py's own
    fingerprint-based active-mission lookup — never guesses whether
    something is already tracked."""
    entries = []
    for e in read_full_priority_queue():
        rank = e.get("rank")
        rank_id = e.get("id")
        if not rank_id:
            continue
        goal_text = e.get("note") or f"Founder Top-10 rank {rank}: {rank_id}"
        fp = mission.mission_fingerprint(goal_text)
        existing = mission.find_active_by_fingerprint(fp)
        entries.append(PriorityBacklogEntry(
            rank=rank, rank_id=rank_id, goal_text=goal_text,
            priority=float(11 - rank) if rank else 0.0,
            existing_mission_id=existing.mission_id if existing else None,
            excluded=rank_id in EXCLUDED_RANK_IDS,
        ))
    return entries


def create_missions_for_unrepresented_ranks() -> list[mission.Mission]:
    """For every backlog entry with no existing Mission and not excluded,
    creates ONE real Mission via the existing, idempotent mission.
    create_mission() — a bare registration record: no WorkItem, no
    dispatch, no scheduler engagement. Safe to call every cycle: an
    already-represented or excluded rank is always a no-op, never a
    duplicate (mission.create_mission()'s own fingerprint idempotency)."""
    created = []
    for entry in discover_backlog():
        if entry.existing_mission_id is not None or entry.excluded:
            continue
        m = mission.create_mission(
            entry.goal_text,
            origin="founder_top10_priority_backlog",
            priority=entry.priority,
            provenance={
                "source": "founder_top10_priority_governor_full_queue",
                "canonical_action_authority": ac._CANONICAL_ACTION_AUTHORITY_ID,
                "rank": entry.rank,
                "rank_id": entry.rank_id,
            },
        )
        created.append(m)
    return created
