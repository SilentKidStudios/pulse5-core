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

# A RUNNING item whose heartbeat is older than this is presumed to have lost
# its owner (crashed process, killed worker) — see is_stale()/reclaim_stale().
# Same order of magnitude as job_ledger.STALE_AFTER_S for the same reason:
# generous relative to a genuinely slow-but-alive worker.
STALE_AFTER_S = 900

MAX_CHILDREN_PER_PARENT = 25       # bounded fan-out: one parent's direct dependency-children
MAX_RECURSIVE_DEPTH = 12           # bounded recursive dependency-discovery depth
MAX_RESUME_ATTEMPTS = 1            # a crashed RUNNING item may be auto-requeued at most once


class WorkState:
    RUNNABLE = "runnable"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    BLOCKED_FOUNDER = "blocked_founder"
    BLOCKED_EXTERNAL_SESSION = "blocked_external_session"
    RUNNING = "running"
    VALIDATING = "validating"
    REPAIRABLE_FAILED = "repairable_failed"
    COMPLETED = "completed"
    TERMINAL_FAILED = "terminal_failed"


TERMINAL_STATES = frozenset({WorkState.COMPLETED, WorkState.TERMINAL_FAILED})
BLOCKED_STATES = frozenset({
    WorkState.BLOCKED_DEPENDENCY, WorkState.BLOCKED_FOUNDER, WorkState.BLOCKED_EXTERNAL_SESSION,
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


def mark_repairable_failed(work_id: str, *, result: dict[str, Any] | None = None) -> WorkItem:
    record = load(work_id)
    if record is None:
        raise ValueError(f"no work item {work_id!r}")
    record.state = WorkState.REPAIRABLE_FAILED
    record.result = result
    return _save(record, note="repairable_failed — eligible for governed repair/reproposal")


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
    still returned)."""
    try:
        import session_ownership
    except ImportError:
        session_ownership = None  # soft dependency — degrade to "always available"

    out = []
    for record in list_all():
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
        "BLOCKED_FOUNDER": 0, "BLOCKED_EXTERNAL_SESSION": 0, "VALIDATING": 0,
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
        elif r.state == WorkState.VALIDATING:
            counts["VALIDATING"] += 1
        elif r.state == WorkState.REPAIRABLE_FAILED:
            counts["REPAIRABLE_FAILED"] += 1
        elif r.state == WorkState.COMPLETED:
            counts["COMPLETED"] += 1
        elif r.state == WorkState.TERMINAL_FAILED:
            counts["TERMINAL"] += 1
    return counts
