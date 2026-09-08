"""
Durable Work Graph — the canonical scheduler primitive the WALK-AWAY
CONVERGENCE gap audit found missing from the live mrsilent-autonomous-cycle
path. audit's own findings (2026-09-04): the real pipeline is three linked,
shallow dataclasses -- Campaign (objective) -> CampaignStep (depends_on,
backward-index-only, ONE campaign's own array) -> job_ledger record (one
job) -- capped at MAX_STEPS=6 per campaign, advanced ONE campaign per cycle
(MAX_CAMPAIGNS_ADVANCED_PER_CYCLE=1), with no cross-campaign/cross-objective
dependency edge, no cycle-detection algorithm (cycles are merely prevented
structurally by the backward-index constraint), and zero scale evidence at
any tier.

This module is the missing general layer: a durable WorkItem graph with
real PARENT/CHILD/DEPENDENCY edges that can cross objective/campaign
boundaries, explicit BLOCKED_ON_DEPENDENCY state (not just "not yet its
turn"), real cycle detection (not just structural prevention), bounded
fan-out, duplicate-child suppression, and lease/heartbeat crash recovery —
so a scheduler (see the forthcoming scheduler.py) can hold 10/100/1000+
durable work items and know, at any moment, exactly which ones are eligible.

Deliberately does NOT replace campaign.py/proposal.py/job_ledger.py. A
WorkItem may (optionally) reference an existing campaign_id/proposal_id/
job_id in its `provenance` dict as the thing it was derived from or the
thing it will dispatch when it runs — this is the graph/scheduling layer
ON TOP of those stores, not a second copy of them. Session-ownership-aware
scheduling is layered in via session_ownership.classify_path_ownership()
(soft dependency — this module works standalone if that module is absent).

Every write is atomic (temp file + os.replace), matching job_ledger.py's
and session_ownership.py's convention.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BRIDGE_ROOT = Path(__file__).resolve().parent
WORK_GRAPH_ROOT = BRIDGE_ROOT / "work_graph_state"
ITEMS_DIR = WORK_GRAPH_ROOT / "items"
LOCKS_DIR = WORK_GRAPH_ROOT / "locks"
STATE_INDEX_DIR = WORK_GRAPH_ROOT / "state_index"  # see runnable_items()'s own docstring
# (2026-09-08 scale fix) — one subdirectory per WorkState value, each containing zero-byte marker
# files named by work_id. A directory listing (os.listdir/scandir) is O(items in that state), not
# O(total population) — this is what lets runnable_items() avoid a full list_all() scan at scale.

# A RUNNING item whose heartbeat is older than this is presumed to have lost
# its owner (crashed process, killed worker) — see is_stale()/reclaim_stale().
# Same order of magnitude as job_ledger.STALE_AFTER_S for the same reason:
# generous relative to a genuinely slow-but-alive worker.
STALE_AFTER_S = 900

MAX_CHILDREN_PER_PARENT = 25       # bounded fan-out: one parent's direct dependency-children
MAX_RECURSIVE_DEPTH = 12           # bounded recursive dependency-discovery depth
MAX_RESUME_ATTEMPTS = 1            # a crashed RUNNING item may be auto-requeued at most once
MAX_REPAIR_ATTEMPTS = 3            # a REPAIRABLE_FAILED item may be auto-retried this many times
                                    # before Founder escalation (campaign section 14)


class WorkState:
    RUNNABLE = "runnable"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    BLOCKED_FOUNDER = "blocked_founder"
    BLOCKED_EXTERNAL_SESSION = "blocked_external_session"
    BLOCKED_DEFERRED = "blocked_deferred"  # DEFERRED-CHURN gap-closure (2026-09-06): parks a WorkItem
    # backing a proposal whose canonical status is DEFERRED — advance_one() never accepts DEFERRED
    # as an eligible starting status (only OBSERVED/PROPOSED), so leaving such an item RUNNABLE
    # produced permanent REPAIRABLE_FAILED/retry churn for work that can never actually advance.
    # Distinct from BLOCKED_FOUNDER (a different, risk-classification axis) so status snapshots
    # stay truthful about WHY an item isn't running. See proposal_work_bridge.py's population logic.
    BLOCKED_REFINEMENT = "blocked_refinement"  # PROPOSAL COMPLETENESS GATE (2026-09-07): parks a WorkItem
    # backing a proposal that is risk_score=='founder_gated' but not yet decision-ready (see
    # evolution/proposal.py::proposal_completeness()) — i.e. it has NOT yet been surfaced to the
    # Founder at all. Distinct from BLOCKED_FOUNDER, which means "a complete, decision-ready
    # proposal is genuinely awaiting an explicit Founder decision" — conflating the two would mean
    # an incomplete, un-scoped proposal (like a bare "this priority needs scoping" placeholder)
    # shows up in the same Founder-visible queue as a real, reviewable decision, exactly the
    # babysitting-load problem this gate exists to remove. See proposal_work_bridge.py.
    RUNNING = "running"
    VALIDATING = "validating"
    REPAIRABLE_FAILED = "repairable_failed"
    COMPLETED = "completed"
    TERMINAL_FAILED = "terminal_failed"


TERMINAL_STATES = frozenset({WorkState.COMPLETED, WorkState.TERMINAL_FAILED})
BLOCKED_STATES = frozenset({
    WorkState.BLOCKED_DEPENDENCY, WorkState.BLOCKED_FOUNDER, WorkState.BLOCKED_EXTERNAL_SESSION,
    WorkState.BLOCKED_DEFERRED, WorkState.BLOCKED_REFINEMENT,
})


class CycleDetected(RuntimeError):
    """Raised by add_dependency() when the requested edge would create a
    dependency cycle — never silently dropped or ignored."""


class FanOutBudgetExceeded(RuntimeError):
    """Raised by create_child_for_dependency() when a parent has already
    reached MAX_CHILDREN_PER_PARENT direct dependency-children."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def objective_fingerprint(text: str) -> str:
    """Stable identity for 'is this the same objective/dependency already
    represented', independent of who asked — mirrors job_ledger.task_
    fingerprint()'s exact reasoning, applied at the work-graph layer."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _all_known_states() -> tuple[str, ...]:
    """Introspects WorkState's own string attributes rather than a
    hand-maintained list — so a future new state (like BLOCKED_REFINEMENT,
    added 2026-09-07) is automatically covered by index cleanup without a
    second place needing to be remembered and updated."""
    return tuple(v for k, v in vars(WorkState).items() if not k.startswith("_") and isinstance(v, str))


def _index_mark_state(work_id: str, state: str, *, clear_others: bool = True) -> None:
    """Records work_id as CURRENTLY in `state` for the fast runnable-lookup
    index (see STATE_INDEX_DIR). By default removes any marker from every
    OTHER known state's directory first — unconditionally, correct
    regardless of what the item's previous state actually was, so this
    never depends on tracking a 'previous state' anywhere.

    `clear_others=False` (2026-09-08, write-overhead fix): a real,
    measured cost — ~10 unlink(missing_ok=True) syscalls per save, which
    is operationally negligible at real natural-cycle write volumes (a
    handful of state changes per cycle) but became the dominant cost in a
    synthetic bulk-creation benchmark (10K+ sequential creates). The two
    callers that can PROVE no stale marker could possibly exist —
    create() (a brand-new work_id has never been indexed before) and
    rebuild_state_index() (the whole index directory was just wiped) —
    pass this to skip the unconditionally-safe-but-wasteful cleanup loop.
    _save() (the only path where an item's PREVIOUS indexed state is
    genuinely unknown here) always uses the default True.

    Best-effort throughout: an index write failure is swallowed — the
    authoritative WorkItem file (already written by the caller before
    this runs) remains correct either way; runnable_items() falls back to
    a full scan whenever the index can't be trusted (see its own
    docstring), so a missed/corrupt marker degrades to the pre-existing
    O(n) behavior, never to a wrong answer."""
    try:
        if clear_others:
            for s in _all_known_states():
                if s != state:
                    (STATE_INDEX_DIR / s / work_id).unlink(missing_ok=True)
        target_dir = STATE_INDEX_DIR / state
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / work_id).touch()
    except OSError:
        pass


def _index_built_marker() -> Path:
    return STATE_INDEX_DIR / ".built"


def rebuild_state_index() -> dict[str, int]:
    """One-time (or ad-hoc repair) full rebuild of STATE_INDEX_DIR from the
    real, authoritative ITEMS_DIR — the only place this module ever does a
    full list_all() scan for index purposes, deliberately: this is how a
    population that existed before this index existed (or an index that
    somehow drifted) gets a trustworthy index without ever guessing.
    Idempotent and safe to run any time; wipes and rewrites the whole
    index directory from ground truth. Returns {state: count}.

    Writes a '.built' sentinel on success — see _indexed_runnable_
    candidates()'s own docstring for the real bug this closed (2026-09-08):
    create()/_save() alone create ONLY the specific state subdirectory an
    item is currently in, so a state with zero current members (e.g. no
    item has ever been BLOCKED_EXTERNAL_SESSION) never gets a directory at
    all — checking 'does every relevant subdirectory exist' as the trust
    signal made the index silently untrustworthy (permanent full-scan
    fallback) in that common, real case. The sentinel is the correct
    signal instead: it means 'every item that existed at rebuild time is
    accounted for, and every write since has incrementally maintained
    that' — at which point an ABSENT state subdirectory is trustworthy
    ground truth ('zero items', not 'never indexed')."""
    import shutil
    if STATE_INDEX_DIR.exists():
        shutil.rmtree(STATE_INDEX_DIR)
    counts: dict[str, int] = {}
    for record in list_all():
        _index_mark_state(record.work_id, record.state, clear_others=False)  # directory just wiped above
        counts[record.state] = counts.get(record.state, 0) + 1
    STATE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _index_built_marker().write_text(_now())
    return counts


@dataclass
class WorkItem:
    work_id: str
    kind: str                       # "objective" | "task" | "dependency" | "repair"
    description: str
    fingerprint: str
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    state: str = WorkState.RUNNABLE
    priority: float = 0.0           # higher = more important; see effective_priority() for aging
    owner_path: str | None = None   # optional repo path this item's execution will touch
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)  # e.g. {"created_because": "...", "campaign_id": "...", "proposal_id": "..."}
    result: dict[str, Any] | None = None
    owner: str | None = None        # session/worker id currently holding the RUNNING lease
    pid: int = 0
    hostname: str = ""
    resume_count: int = 0
    repair_attempts: int = 0        # bounded auto-retry count for REPAIRABLE_FAILED — see retry_repairable_failed()
    next_retry_at: str = ""         # deterministic backoff floor before the next auto-retry may fire
    created_at: str = ""
    updated_at: str = ""
    heartbeat: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


def _path(work_id: str) -> Path:
    return ITEMS_DIR / f"{work_id}.json"


def _lock_path(work_id: str) -> Path:
    return LOCKS_DIR / f"{work_id}.lock"


def create(
    work_id: str,
    *,
    kind: str,
    description: str,
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    priority: float = 0.0,
    owner_path: str | None = None,
    resource_requirements: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> WorkItem:
    now = _now()
    # every real caller (mission.py, proposal_work_bridge.py) already
    # guards with load(work_id) is None before calling create() — this is
    # what makes clear_others=False below provably safe in the common
    # case. Checked here too, defensively: a future caller that violates
    # that convention and re-creates an existing work_id still gets the
    # safe (if slower) clear_others=True path, never a silently-stale
    # marker left behind in the item's PRIOR state's index directory.
    pre_existing = _path(work_id).exists()
    fp = objective_fingerprint(f"{kind}:{description}")
    depends_on = list(depends_on or [])
    for dep_id in depends_on:
        if _would_cycle(work_id, dep_id):
            raise CycleDetected(f"creating {work_id!r} depending on {dep_id!r} would form a cycle")
    record = WorkItem(
        work_id=work_id, kind=kind, description=description, fingerprint=fp,
        parent_id=parent_id, depends_on=depends_on,
        state=WorkState.BLOCKED_DEPENDENCY if depends_on else WorkState.RUNNABLE,
        priority=priority, owner_path=owner_path,
        resource_requirements=resource_requirements or {},
        provenance=provenance or {},
        created_at=now, updated_at=now, heartbeat=now,
        history=[{"at": now, "state": WorkState.RUNNABLE, "note": "created"}],
    )
    _atomic_write_json(_path(work_id), asdict(record))
    _index_mark_state(work_id, record.state, clear_others=pre_existing)
    return record


def load(work_id: str) -> WorkItem | None:
    p = _path(work_id)
    if not p.exists():
        return None
    try:
        return WorkItem(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError):
        return None


def list_all() -> list[WorkItem]:
    if not ITEMS_DIR.exists():
        return []
    out = []
    for p in sorted(ITEMS_DIR.glob("*.json")):
        try:
            out.append(WorkItem(**json.loads(p.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _save(record: WorkItem, *, note: str = "") -> WorkItem:
    now = _now()
    record.updated_at = now
    record.heartbeat = now
    if note:
        record.history.append({"at": now, "state": record.state, "note": note})
    _atomic_write_json(_path(record.work_id), asdict(record))
    _index_mark_state(record.work_id, record.state)
    return record


def find_active_by_fingerprint(fingerprint: str) -> WorkItem | None:
    """Idempotency/dedup check mirroring job_ledger.find_active_by_
    fingerprint(): only non-terminal items count as "already exists"."""
    for r in list_all():
        if r.fingerprint == fingerprint and r.state not in TERMINAL_STATES:
            return r
    return None


# ---- dependency graph -------------------------------------------------------

def _would_cycle(work_id: str, new_dep_id: str, *, _visited: set[str] | None = None) -> bool:
    """True iff new_dep_id already (transitively) depends on work_id — i.e.
    adding the edge work_id -> new_dep_id would close a cycle. Real DFS over
    the durable graph, not structural prevention (the campaign audit's own
    finding: the existing campaign.py model prevents cycles only by
    forbidding forward references, which cannot express cross-objective
    dependencies at all)."""
    if new_dep_id == work_id:
        return True
    _visited = _visited or set()
    if new_dep_id in _visited:
        return False  # already explored this branch (graph, not necessarily tree)
    _visited.add(new_dep_id)
    dep_record = load(new_dep_id)
    if dep_record is None:
        return False
    for further in dep_record.depends_on:
        if _would_cycle(work_id, further, _visited=_visited):
            return True
    return False


def add_dependency(work_id: str, depends_on_id: str) -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    if depends_on_id in record.depends_on:
        return record  # already present — idempotent
    if _would_cycle(work_id, depends_on_id):
        raise CycleDetected(f"adding dependency {depends_on_id!r} to {work_id!r} would form a cycle")
    record.depends_on.append(depends_on_id)
    if record.state == WorkState.RUNNABLE:
        record.state = WorkState.BLOCKED_DEPENDENCY
    return _save(record, note=f"dependency added: {depends_on_id}")


def unmet_dependencies(work_id: str) -> list[str]:
    record = load(work_id)
    if record is None:
        return []
    unmet = []
    for dep_id in record.depends_on:
        dep = load(dep_id)
        if dep is None or dep.state != WorkState.COMPLETED:
            unmet.append(dep_id)
    return unmet


def create_child_for_dependency(
    parent_id: str,
    *,
    kind: str,
    description: str,
    priority: float | None = None,
    resource_requirements: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> WorkItem:
    """DEPENDENCY_DISCOVERY + DUPLICATE_CHILD_SUPPRESSION + bounded fan-out
    in one call: if a non-terminal child with the same fingerprint already
    exists as a dependency of this exact parent, returns it unchanged
    (never creates a second one for the same discovered need). Otherwise
    creates a new child, wires parent -> child as a dependency (parent
    becomes/stays BLOCKED_DEPENDENCY), and stamps provenance recording WHY
    the child exists (PROVENANCE requirement)."""
    parent = load(parent_id)
    if parent is None:
        raise ValueError(f"no work item {parent_id!r}")
    fp = objective_fingerprint(f"{kind}:{description}")
    for existing_id in parent.depends_on:
        existing = load(existing_id)
        if existing is not None and existing.fingerprint == fp and existing.state not in TERMINAL_STATES:
            return existing  # duplicate-child suppression
    if len(parent.depends_on) >= MAX_CHILDREN_PER_PARENT:
        raise FanOutBudgetExceeded(
            f"{parent_id!r} already has {len(parent.depends_on)} dependency-children "
            f"(MAX_CHILDREN_PER_PARENT={MAX_CHILDREN_PER_PARENT})"
        )
    child_id = f"{parent_id}.dep.{fp}"
    child = load(child_id)
    if child is None:
        prov = dict(provenance or {})
        prov.setdefault("created_because", f"missing dependency of {parent_id}")
        prov.setdefault("parent_id", parent_id)
        child = create(
            child_id, kind=kind, description=description,
            priority=priority if priority is not None else parent.priority,
            resource_requirements=resource_requirements, provenance=prov,
            parent_id=parent_id,
        )
    add_dependency(parent_id, child_id)
    return child


def recursive_depth(work_id: str, *, _seen: set[str] | None = None) -> int:
    """Depth of the dependency chain rooted at work_id — used to enforce
    MAX_RECURSIVE_DEPTH and prevent unbounded dependency-of-dependency
    expansion (campaign section 3's "no infinite dependency expansion")."""
    _seen = _seen or set()
    if work_id in _seen:
        return 0  # cycle already prevented at write time; defensive floor here
    _seen.add(work_id)
    record = load(work_id)
    if record is None or not record.depends_on:
        return 0
    return 1 + max((recursive_depth(d, _seen=_seen) for d in record.depends_on), default=0)


# ---- state transitions -------------------------------------------------------

def mark_blocked_founder(work_id: str, *, note: str = "") -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.BLOCKED_FOUNDER
    return _save(record, note=note or "blocked on Founder gate")


def mark_blocked_deferred(work_id: str, *, note: str = "") -> WorkItem:
    """Parks a WorkItem whose backing proposal is currently DEFERRED —
    see WorkState.BLOCKED_DEFERRED's docstring. Never removes the item or
    its provenance; the very next population pass that finds the proposal's
    status has changed away from DEFERRED will release it exactly like
    mark_blocked_founder()'s Founder-decision-resolved release does."""
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.BLOCKED_DEFERRED
    return _save(record, note=note or "blocked: backing proposal is deferred")


def mark_blocked_refinement(work_id: str, *, note: str = "") -> WorkItem:
    """Parks a WorkItem backing a founder_gated proposal that is not yet
    decision-ready (see evolution/proposal.py::proposal_completeness()) —
    deliberately NEVER routes through BLOCKED_FOUNDER, so it never appears
    in the Founder-visible blocked_founder rollup. The very next population
    pass that finds the proposal has become decision-ready releases it to
    BLOCKED_FOUNDER (if still genuinely founder_gated) exactly like
    mark_blocked_deferred()'s release does — see proposal_work_bridge.py."""
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.BLOCKED_REFINEMENT
    return _save(record, note=note or "blocked: backing proposal is not yet decision-ready")


def mark_completed(work_id: str, *, result: dict[str, Any] | None = None) -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.COMPLETED
    record.result = result
    record = _save(record, note="completed")
    resume_eligible_parents()
    return record


def mark_terminal_failed(work_id: str, *, result: dict[str, Any] | None = None) -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.TERMINAL_FAILED
    record.result = result
    return _save(record, note="terminal_failed")


def _retry_backoff_at(attempt: int, *, now: datetime | None = None) -> str:
    """Deterministic backoff, same shape as job_ledger.retry_ready_at(): a
    pure function of attempt count in [60, 120] seconds — never a raw
    sleep, never unbounded, never dependent on how often the caller
    happens to check."""
    now = now or datetime.now(timezone.utc)
    backoff_seconds = 60 + ((attempt - 1) % 61)
    from datetime import timedelta
    return (now + timedelta(seconds=backoff_seconds)).isoformat()


def _retry_is_ready(ready_at: str, *, now: datetime | None = None) -> bool:
    if not ready_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        dt_ready = datetime.fromisoformat(ready_at)
    except (ValueError, TypeError):
        return True  # unreadable deadline — fail open to "ready" rather than stuck forever
    if dt_ready.tzinfo is None:
        dt_ready = dt_ready.replace(tzinfo=timezone.utc)
    return now >= dt_ready


def mark_repairable_failed(work_id: str, *, result: dict[str, Any] | None = None) -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.REPAIRABLE_FAILED
    record.result = result
    record.next_retry_at = _retry_backoff_at(record.repair_attempts + 1)
    return _save(record, note="repairable_failed — eligible for governed repair/reproposal")


def retry_repairable_failed() -> list[str]:
    """FAILURE_REPAIR_OR_REPROPOSAL_AUTONOMOUS + RETRY_BUDGETING (campaign
    section 14): a REPAIRABLE_FAILED item is automatically requeued to
    RUNNABLE once its deterministic backoff window elapses, up to
    MAX_REPAIR_ATTEMPTS times — no caller has to manually re-dispatch an
    ordinary repairable failure. Once that budget is exhausted, the item is
    NOT silently dropped — it moves to BLOCKED_FOUNDER (the same state a
    genuine authority gate uses), so a real Founder escalation happens
    ("Founder escalation when genuinely required") instead of the item
    quietly vanishing. Safe to call every scheduling pass — only items
    whose backoff has actually elapsed are touched."""
    now = datetime.now(timezone.utc)
    requeued = []
    for record in list_all():
        if record.state != WorkState.REPAIRABLE_FAILED:
            continue
        if not _retry_is_ready(record.next_retry_at, now=now):
            continue
        if record.repair_attempts >= MAX_REPAIR_ATTEMPTS:
            record.state = WorkState.BLOCKED_FOUNDER
            _save(record, note=f"repair budget exhausted ({record.repair_attempts} attempts) — escalated to Founder")
            continue
        record.repair_attempts += 1
        record.state = WorkState.RUNNABLE
        _save(record, note=f"repair attempt {record.repair_attempts}/{MAX_REPAIR_ATTEMPTS} — requeued")
        requeued.append(record.work_id)
    return requeued


def resume_eligible_parents() -> list[str]:
    """PARENT_AUTOMATIC_RESUMPTION: scan every BLOCKED_DEPENDENCY item and
    promote it back to RUNNABLE (or BLOCKED_FOUNDER/BLOCKED_EXTERNAL_SESSION
    stay untouched — only a pure dependency block is auto-lifted) once ALL
    of its dependencies are COMPLETED. No caller has to remember to say
    "now finish C", "go back to B" — this is called after every
    mark_completed() and is safe to call redundantly at any time (pure
    re-evaluation, not a one-shot event)."""
    resumed = []
    for record in list_all():
        if record.state != WorkState.BLOCKED_DEPENDENCY:
            continue
        if unmet_dependencies(record.work_id):
            continue
        record.state = WorkState.RUNNABLE
        _save(record, note="all dependencies completed — auto-resumed")
        resumed.append(record.work_id)
    return resumed


# ---- fairness / anti-starvation ---------------------------------------------

def effective_priority(record: WorkItem, *, now: datetime | None = None, aging_rate: float = 0.01) -> float:
    """base priority + a small monotonic age bonus, so a continuous stream
    of higher-ranked small jobs cannot starve an older, lower-priority
    eligible item forever (campaign section 10). aging_rate is per minute
    of age; deliberately small so genuine priority ordering still dominates
    for any item younger than roughly hours old, and only very old,
    perpetually-passed-over items eventually outrank fresh high-priority
    ones."""
    now = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(record.created_at)
    except (ValueError, TypeError):
        return record.priority
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_minutes = max(0.0, (now - created).total_seconds() / 60.0)
    return record.priority + aging_rate * age_minutes


FRESH_WORK_MIN_AGE_GAP_MINUTES = 5.0
# The minimum real age gap (in minutes) between a candidate and the item it
# would displace before scheduler.py's fresh-work reservation (see
# freshest_eligible()) actually applies. Without this, "pick whichever
# eligible item is nominally most recently created" fires on pure
# microsecond-scale creation-order noise between items created in the same
# instant (proven by this session's own test regression: a synthetic 3-item
# batch created back-to-back in one test run, where "freshest" was
# meaningless and displaced a deliberately-different test item that should
# have been dispatched). 5 minutes is comfortably smaller than the real
# gap this fix targets (real backlog items are days-to-weeks old vs.
# genuinely new arrivals) while comfortably larger than any burst of
# items created together in the same pass/test.


def is_meaningfully_fresher(candidate: WorkItem, displaced: WorkItem, *, now: datetime | None = None) -> bool:
    """True iff `candidate` was created at least FRESH_WORK_MIN_AGE_GAP_
    MINUTES more recently than `displaced` — i.e. there is a real,
    meaningful "old backlog vs. new arrival" story to protect, not just
    creation-order noise between near-simultaneous items. Fails closed to
    False or either created_at is unparseable — an ambiguous comparison
    never triggers the reservation."""
    try:
        c = datetime.fromisoformat(candidate.created_at)
        d = datetime.fromisoformat(displaced.created_at)
    except (ValueError, TypeError):
        return False
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    gap_minutes = (c - d).total_seconds() / 60.0  # positive iff candidate is newer (more recent) than displaced
    return gap_minutes >= FRESH_WORK_MIN_AGE_GAP_MINUTES


def freshest_eligible(items: list[WorkItem]) -> WorkItem | None:
    """The single most-recently-created item among `items` (by real
    created_at, not effective_priority) — or None for an empty list.

    FRESH_WORK_LIVENESS (2026-09-06): effective_priority()'s aging bonus
    only protects OLD work from being starved by a continuous stream of
    NEW high-priority arrivals (campaign section 10) — it provides no
    symmetric guarantee in the other direction. A long-lived, continuously
    replenished backlog (this Studio's ordinary proposal queue — real
    natural evidence this campaign gathered showed 85-120+ items RUNNABLE
    essentially every cycle, most weeks old) can keep every genuinely
    fresh item permanently behind it: a brand-new item starts its own age
    bonus from zero and can never catch up to items that have already
    been aging for weeks, at 0.01 priority-equivalent per minute of age.
    Used by scheduler.py to reserve exactly one bounded dispatch slot per
    pass for the freshest eligible item, without touching effective_
    priority()'s own ordering or the "aging eventually outranks fresh
    high-priority work" guarantee for old low-priority items (see
    test_anti_starvation_aging_eventually_outranks_higher_priority) — this
    is a SEPARATE, narrow, symmetric guarantee for the opposite failure
    direction, not a replacement for the existing mechanism.

    An item with unparseable created_at is never treated as "freshest" —
    fails closed to being ignored by this specific selection, exactly
    like effective_priority()'s own fallback for the same case."""
    best: WorkItem | None = None
    best_epoch = float("-inf")
    for item in items:
        try:
            created = datetime.fromisoformat(item.created_at)
        except (ValueError, TypeError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        epoch = created.timestamp()
        if epoch > best_epoch:
            best_epoch = epoch
            best = item
    return best


def _indexed_runnable_candidates() -> list[WorkItem] | None:
    """Fast candidate source for runnable_items() (2026-09-08 scale fix):
    reads STATE_INDEX_DIR/runnable/ and STATE_INDEX_DIR/blocked_external_
    session/ (the only two states runnable_items() itself ever cares
    about) via a plain directory listing — O(items in those two states),
    never O(total population) — then load()s just those specific items.

    Returns None (never a guess) whenever the index cannot be trusted, so
    the caller falls back to the original full list_all() scan: ONLY when
    rebuild_state_index()'s own '.built' sentinel is absent (see that
    function's docstring for the real bug this fixes — 2026-09-08:
    requiring EVERY relevant subdirectory to individually exist was wrong,
    since create()/_save() alone never create a directory for a state with
    zero current members, e.g. a system where nothing has ever been
    BLOCKED_EXTERNAL_SESSION — that made the index permanently distrust
    itself, silently falling back to full-scan forever, in exactly the
    common case this fix exists to speed up). Once the sentinel exists, a
    missing state subdirectory correctly means 'zero items in that state
    right now', not 'never indexed' — the two are different facts, and the
    sentinel is what lets this function tell them apart. A marker whose
    underlying item no longer loads (deleted/corrupt) is silently skipped
    — the SAME fail-safe list_all() itself already applies to a corrupt
    item file, not a new risk this introduces."""
    if not _index_built_marker().exists():
        return None
    runnable_dir = STATE_INDEX_DIR / WorkState.RUNNABLE
    blocked_ext_dir = STATE_INDEX_DIR / WorkState.BLOCKED_EXTERNAL_SESSION
    candidate_ids: set[str] = set()
    for d in (runnable_dir, blocked_ext_dir):
        if not d.exists():
            continue  # trustworthy: sentinel present means this genuinely means zero, not unindexed
        try:
            candidate_ids.update(p.name for p in d.iterdir())
        except OSError:
            return None
    candidates = []
    for work_id in candidate_ids:
        record = load(work_id)
        if record is not None:
            candidates.append(record)
    return candidates


def _filter_runnable(candidates: list[WorkItem], *, self_session_id: str | None = None) -> list[WorkItem]:
    """The actual eligibility filter — state==RUNNABLE, dependencies met,
    not externally blocked — shared verbatim by BOTH the indexed fast path
    and the full-scan fallback in runnable_items(), so the two candidate
    SOURCES can never silently diverge in FILTERING semantics; only one
    filtering implementation exists. See runnable_items()'s own docstring
    for the full behavioral contract this implements."""
    try:
        import session_ownership
    except ImportError:
        session_ownership = None  # soft dependency — degrade to "always available"

    out = []
    for record in candidates:
        if record.state == WorkState.BLOCKED_EXTERNAL_SESSION:
            # re-check: has the external lease cleared since we last looked?
            if record.owner_path and session_ownership is not None:
                status, _owner = session_ownership.classify_path_ownership(
                    record.owner_path, self_session_id or "unknown-self")
                if status != session_ownership.STATUS_BLOCKED_EXTERNAL:
                    record.state = WorkState.RUNNABLE
                    _save(record, note="external session ownership cleared — resumed")
                else:
                    continue
            else:
                continue
        if record.state != WorkState.RUNNABLE:
            continue
        if unmet_dependencies(record.work_id):
            continue
        if record.owner_path and session_ownership is not None:
            status, owner = session_ownership.classify_path_ownership(
                record.owner_path, self_session_id or "unknown-self")
            if status == session_ownership.STATUS_BLOCKED_EXTERNAL:
                record.state = WorkState.BLOCKED_EXTERNAL_SESSION
                _save(record, note=f"blocked: path owned by external session {owner}")
                continue
        out.append(record)
    out.sort(key=lambda r: effective_priority(r), reverse=True)
    return out


def runnable_items(*, self_session_id: str | None = None) -> list[WorkItem]:
    """Every item currently eligible to run: state==RUNNABLE, all
    dependencies satisfied (should already be true given resume_eligible_
    parents(), re-checked defensively), and — if session_ownership is
    importable and the item declares an owner_path — not
    BLOCKED_EXTERNAL_ACTIVE_SESSION. Ordered by effective_priority()
    descending so aging/fairness is applied consistently at the one place
    a scheduler asks "what can I run next". A path blocked by another
    session is transitioned to BLOCKED_EXTERNAL_SESSION as a side effect (so
    it's visible in scale/status snapshots) but never removed from the graph
    — it resumes automatically the moment classify_path_ownership() clears
    (UNRELATED_WORK_CONTINUES_WHILE_EXTERNAL_BLOCK_EXISTS: this function
    simply excludes it from the returned list; every other eligible item is
    still returned).

    SCALE FIX (2026-09-08): sources its candidate set from STATE_INDEX_DIR
    (see _indexed_runnable_candidates()) instead of a full list_all() scan
    whenever the index can be trusted — proven, via a real A/B oracle
    against the exact previous full-scan implementation, to return
    identical results (see tests/test_work_graph_runnable_index.py). Falls
    back to the original full-scan behavior, unchanged, whenever the index
    is missing/unbuilt — never a behavior change, only a faster source for
    the SAME filter (_filter_runnable(), shared by both paths)."""
    candidates = _indexed_runnable_candidates()
    if candidates is None:
        candidates = list_all()
    return _filter_runnable(candidates, self_session_id=self_session_id)


# ---- lease/heartbeat crash recovery (mirrors job_ledger.py exactly) --------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def claim(work_id: str, *, owner: str) -> bool:
    """Atomic exclusive claim (O_CREAT|O_EXCL) — the same idempotent-
    dispatch guarantee job_ledger.claim() gives a single job, applied to a
    work-graph item: two schedulers (or two threads in one) racing to start
    the same RUNNABLE item can never both win."""
    p = _lock_path(work_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "owner": owner, "pid": os.getpid(), "hostname": socket.gethostname(), "claimed_at": _now(),
    }).encode()
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except FileExistsError:
        return False
    record = load(work_id)
    if record is not None:
        record.state = WorkState.RUNNING
        record.owner = owner
        record.pid = os.getpid()
        record.hostname = socket.gethostname()
        _save(record, note=f"claimed by {owner}")
    return True


def release(work_id: str, *, owner: str) -> bool:
    p = _lock_path(work_id)
    if not p.exists():
        return False
    try:
        info = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    if info.get("pid") != os.getpid():
        return False
    p.unlink(missing_ok=True)
    return True


def lock_status(work_id: str) -> dict[str, Any]:
    p = _lock_path(work_id)
    if not p.exists():
        return {"held": False}
    try:
        info = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"held": True, "corrupt": True}
    alive = _pid_alive(info.get("pid", -1))
    return {"held": True, "owner": info.get("owner"), "pid": info.get("pid"),
            "hostname": info.get("hostname"), "claimed_at": info.get("claimed_at"), "alive": alive}


def is_stale(record: WorkItem, *, stale_after_s: int = STALE_AFTER_S) -> bool:
    if record.state != WorkState.RUNNING:
        return False
    lock = lock_status(record.work_id)
    if lock.get("held"):
        return not lock.get("alive", True)
    try:
        hb = datetime.fromisoformat(record.heartbeat)
    except (ValueError, TypeError):
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb).total_seconds() > stale_after_s


def reclaim_stale() -> list[str]:
    """CRASH_RESTART_RECOVERY + no-duplicate-dispatch: a RUNNING item whose
    lock is provably dead is requeued to RUNNABLE at most MAX_RESUME_ATTEMPTS
    times, then TERMINAL_FAILED — never left permanently ghost-running,
    never silently re-run unbounded. Mirrors job_ledger's RecoveryPolicy
    reasoning at the work-graph layer."""
    reclaimed = []
    for record in list_all():
        if record.state != WorkState.RUNNING or not is_stale(record):
            continue
        _lock_path(record.work_id).unlink(missing_ok=True)
        if record.resume_count >= MAX_RESUME_ATTEMPTS:
            record.state = WorkState.TERMINAL_FAILED
            record.result = {"error": "stale_resume_exhausted"}
            _save(record, note="reclaim: resume budget exhausted — terminal_failed")
        else:
            record.resume_count += 1
            record.state = WorkState.RUNNABLE
            record.owner = None
            _save(record, note="reclaim: stale RUNNING lock — requeued")
        reclaimed.append(record.work_id)
    return reclaimed


def touch_heartbeat(work_id: str) -> None:
    record = load(work_id)
    if record is None:
        return
    record.heartbeat = _now()
    _atomic_write_json(_path(work_id), asdict(record))


# ---- status snapshot (section 5's required per-tier fields) ----------------

def status_counts() -> dict[str, int]:
    counts: dict[str, int] = {
        "TASK_COUNT": 0, "RUNNABLE": 0, "RUNNING": 0, "BLOCKED_DEPENDENCY": 0,
        "BLOCKED_FOUNDER": 0, "BLOCKED_EXTERNAL_SESSION": 0, "BLOCKED_DEFERRED": 0,
        "BLOCKED_REFINEMENT": 0, "VALIDATING": 0,
        "REPAIRABLE_FAILED": 0, "COMPLETED": 0, "TERMINAL": 0,
    }
    for r in list_all():
        counts["TASK_COUNT"] += 1
        if r.state == WorkState.RUNNABLE:
            counts["RUNNABLE"] += 1
        elif r.state == WorkState.RUNNING:
            counts["RUNNING"] += 1
        elif r.state == WorkState.BLOCKED_DEPENDENCY:
            counts["BLOCKED_DEPENDENCY"] += 1
        elif r.state == WorkState.BLOCKED_FOUNDER:
            counts["BLOCKED_FOUNDER"] += 1
        elif r.state == WorkState.BLOCKED_EXTERNAL_SESSION:
            counts["BLOCKED_EXTERNAL_SESSION"] += 1
        elif r.state == WorkState.BLOCKED_DEFERRED:
            counts["BLOCKED_DEFERRED"] += 1
        elif r.state == WorkState.BLOCKED_REFINEMENT:
            counts["BLOCKED_REFINEMENT"] += 1
        elif r.state == WorkState.VALIDATING:
            counts["VALIDATING"] += 1
        elif r.state == WorkState.REPAIRABLE_FAILED:
            counts["REPAIRABLE_FAILED"] += 1
        elif r.state == WorkState.COMPLETED:
            counts["COMPLETED"] += 1
        elif r.state == WorkState.TERMINAL_FAILED:
            counts["TERMINAL"] += 1
    return counts
