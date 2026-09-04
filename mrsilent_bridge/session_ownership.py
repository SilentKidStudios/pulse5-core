"""
Session/Campaign Ownership Registry — the durable layer that lets many
autonomous workers/Claude sessions/background agents share ONE canonical
Studio checkout without one of them silently mutating or redirecting work
another one actively owns.

Why this exists (2026-09-04, WALK-AWAY CONVERGENCE gap-closure): a read-only
gap audit of the mrsilent-autonomous-cycle.timer -> run_cycle() live path
found NO durable concept anywhere in the codebase for "which session/
campaign currently owns this path/objective" — job_ledger.py's claim()/
release() locks a single job's directory, and evolution/proposal.py's
find_open_by_fingerprint() suppresses a duplicate proposal, but nothing
records that a whole external Claude session (a different tmux pane, a
different Remote Control run) is actively working a given area of the tree.
That gap was not theoretical: while conducting this same audit, `ListAgents`
showed a peer session busy in a tmux pane literally named
"mrsilent-walkaway-final-gap" — i.e. another live session apparently
converging on this exact campaign concurrently — with several core
governance files already modified-but-uncommitted in the shared checkout.
This module is the bounded, additive fix: a session registers the path
prefixes it owns, other sessions/tasks check before touching an overlapping
path, and a lease that goes quiet is automatically recoverable rather than
blocking that area forever.

This is NOT a second job/proposal/campaign store. It answers a different
question than job_ledger/proposal/campaign do: not "is this exact job/
proposal/campaign already in flight" but "does some OTHER session already
own this area of the tree/objective space, and should I stay out of it".

Every write is atomic (temp file + os.replace), matching job_ledger.py's
convention — a checkpoint interrupted mid-write can never leave a torn
lease file behind.
"""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BRIDGE_ROOT = Path(__file__).resolve().parent
OWNERSHIP_ROOT = BRIDGE_ROOT / "session_ownership_state"
LEASES_DIR = OWNERSHIP_ROOT / "leases"

# A lease's heartbeat older than this is stale — the session that registered
# it is presumed gone (crashed, exited, or simply never releases explicitly).
# Deliberately generous relative to the ~15-minute mrsilent-autonomous-cycle
# cadence (job_ledger.STALE_AFTER_S=900 for a single job) since a session
# lease spans many cycles/turns of genuinely-still-active human/Claude work,
# not one bounded job — see is_stale() for the full liveness reasoning.
STALE_AFTER_S = 1800  # 30 minutes

AGENT_MODE_READ_ONLY = "read_only"
AGENT_MODE_READ_WRITE = "read_write"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)  # atomic on the same filesystem — no torn writes


def _safe_id(session_id: str) -> str:
    """Filesystem-safe stem for a lease file — session ids may contain
    characters (':', '/', spaces) that are not safe path components."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)


def _lease_path(session_id: str) -> Path:
    return LEASES_DIR / f"{_safe_id(session_id)}.json"


def _normalize_path(path: str) -> str:
    return path.strip().strip("/")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still alive
    except OSError:
        return False
    return True


@dataclass
class OwnershipLease:
    session_id: str
    campaign_label: str
    owned_paths: list[str] = field(default_factory=list)
    agent_mode: str = AGENT_MODE_READ_WRITE
    fingerprint: str | None = None  # DUPLICATE_CAMPAIGN_WORK_SUPPRESSION key
    status: str = "active"  # active | stale | released
    pid: int = 0
    hostname: str = ""
    created_at: str = ""
    updated_at: str = ""
    heartbeat: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


def load(session_id: str) -> OwnershipLease | None:
    p = _lease_path(session_id)
    if not p.exists():
        return None
    try:
        return OwnershipLease(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError):
        return None


def list_all() -> list[OwnershipLease]:
    if not LEASES_DIR.exists():
        return []
    out = []
    for p in sorted(LEASES_DIR.glob("*.json")):
        try:
            out.append(OwnershipLease(**json.loads(p.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def register(
    session_id: str,
    campaign_label: str,
    owned_paths: list[str],
    *,
    agent_mode: str = AGENT_MODE_READ_WRITE,
    fingerprint: str | None = None,
) -> OwnershipLease:
    """Idempotent: re-registering the same session_id updates (merges) its
    owned_paths and refreshes the heartbeat rather than creating a second
    record — a session that discovers it needs to own one more path claims
    it without losing what it already held."""
    now = _now()
    existing = load(session_id)
    if existing is not None and existing.status != "released":
        merged_paths = sorted(set(existing.owned_paths) | {_normalize_path(p) for p in owned_paths})
        existing.owned_paths = merged_paths
        existing.campaign_label = campaign_label
        existing.agent_mode = agent_mode
        existing.fingerprint = fingerprint or existing.fingerprint
        existing.status = "active"
        existing.updated_at = now
        existing.heartbeat = now
        existing.pid = os.getpid()
        existing.hostname = socket.gethostname()
        existing.history.append({"at": now, "note": "re-registered/updated"})
        _atomic_write_json(_lease_path(session_id), asdict(existing))
        return existing
    record = OwnershipLease(
        session_id=session_id,
        campaign_label=campaign_label,
        owned_paths=sorted({_normalize_path(p) for p in owned_paths}),
        agent_mode=agent_mode,
        fingerprint=fingerprint,
        status="active",
        pid=os.getpid(),
        hostname=socket.gethostname(),
        created_at=now,
        updated_at=now,
        heartbeat=now,
        history=[{"at": now, "note": "registered"}],
    )
    _atomic_write_json(_lease_path(session_id), asdict(record))
    return record


def heartbeat(session_id: str) -> OwnershipLease | None:
    record = load(session_id)
    if record is None or record.status == "released":
        return None
    record.heartbeat = _now()
    record.updated_at = record.heartbeat
    _atomic_write_json(_lease_path(session_id), asdict(record))
    return record


def release(session_id: str) -> bool:
    record = load(session_id)
    if record is None or record.status == "released":
        return False
    now = _now()
    record.status = "released"
    record.updated_at = now
    record.history.append({"at": now, "note": "released"})
    _atomic_write_json(_lease_path(session_id), asdict(record))
    return True


def is_stale(lease: OwnershipLease, *, stale_after_s: int = STALE_AFTER_S) -> bool:
    """A released lease is never "stale" — it is simply gone, and
    classify_path_ownership() already skips released leases outright. A
    lease whose hostname matches this host gets a strictly-stronger
    pid-liveness check (mirrors job_ledger.is_stale()'s reasoning: a live
    pid overrides a merely-old-looking heartbeat in both directions). A
    lease from a different host (or a hostname we can't compare, e.g. a
    Remote Control/cloud session) falls back to heartbeat age alone — we
    have no local pid to check."""
    if lease.status == "released":
        return False
    if lease.hostname and lease.hostname == socket.gethostname() and lease.pid:
        return not _pid_alive(lease.pid)
    try:
        hb = datetime.fromisoformat(lease.heartbeat)
    except (ValueError, TypeError):
        return True  # unreadable heartbeat on a non-released lease is itself a red flag
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb).total_seconds() > stale_after_s


def sweep_stale(*, stale_after_s: int = STALE_AFTER_S) -> list[str]:
    """STALE_OWNERSHIP_DETECTION + STALE_OWNERSHIP_RECOVERY in one pass:
    marks every active lease that is_stale() as status="stale" so its
    owned_paths become schedulable again on the very next
    classify_path_ownership() call — no separate "recovery" step exists
    anywhere else; recovery IS the next classification check succeeding.
    Returns the session_ids swept, for callers that want to log/audit it."""
    swept = []
    for lease in list_all():
        if lease.status != "active":
            continue
        if not is_stale(lease, stale_after_s=stale_after_s):
            continue
        lease.status = "stale"
        now = _now()
        lease.updated_at = now
        lease.history.append({"at": now, "note": "swept: heartbeat/pid stale"})
        _atomic_write_json(_lease_path(lease.session_id), asdict(lease))
        swept.append(lease.session_id)
    return swept


def _path_conflicts(a: str, b: str) -> bool:
    """True iff normalized path prefixes a and b overlap — one is an
    ancestor of (or identical to) the other. Prefix semantics: registering
    "mrsilent_bridge/evolution" conflicts with a check against
    "mrsilent_bridge/evolution/founder_request.py", but NOT against the
    unrelated sibling "mrsilent_bridge/session_ownership_state"."""
    a, b = _normalize_path(a), _normalize_path(b)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


# AVAILABLE: no active, non-stale lease (other than the caller's own) claims
# an overlapping path.
STATUS_AVAILABLE = "AVAILABLE"
# OWNED_BY_SELF: the caller's own session already leases an overlapping path.
STATUS_OWNED_BY_SELF = "OWNED_BY_SELF"
# BLOCKED_EXTERNAL_ACTIVE_SESSION: a DIFFERENT, active, non-stale lease
# conflicts — park this branch of work, do not touch the path, keep working
# unrelated eligible work (see module docstring / campaign section 9/26).
STATUS_BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL_ACTIVE_SESSION"


def classify_path_ownership(path: str, self_session_id: str) -> tuple[str, str | None]:
    """Returns (status, owner_session_id_or_None). Sweeps stale leases
    first, so a dead session's old claim can never block a path forever —
    the caller never needs to remember to call sweep_stale() separately."""
    sweep_stale()
    for lease in list_all():
        if lease.status != "active":
            continue
        if not any(_path_conflicts(path, p) for p in lease.owned_paths):
            continue
        if lease.session_id == self_session_id:
            return STATUS_OWNED_BY_SELF, lease.session_id
        return STATUS_BLOCKED_EXTERNAL, lease.session_id
    return STATUS_AVAILABLE, None


class MutationNotAllowed(RuntimeError):
    """Raised by assert_mutation_allowed() — see its docstring."""


def assert_mutation_allowed(session_id: str, path: str) -> None:
    """READ_ONLY_AGENT_MUTATION_PREVENTION: a session registered with
    agent_mode="read_only" (e.g. a background audit fork whose mandate is
    explicitly read-only) can never mutate anything through this guard,
    regardless of what the code it spawns tries to do. Call this at the top
    of any function that writes/mutates `path` on behalf of a registered
    session. The only way to lift the restriction is a fresh register()
    call with agent_mode="read_write" — never something the guarded code
    path itself can toggle from inside a read-only mandate."""
    lease = load(session_id)
    if lease is not None and lease.status != "released" and lease.agent_mode == AGENT_MODE_READ_ONLY:
        raise MutationNotAllowed(
            f"session {session_id!r} is registered read_only; refuses to mutate {path!r}"
        )


def find_active_lease_by_fingerprint(fingerprint: str) -> OwnershipLease | None:
    """DUPLICATE_CAMPAIGN_WORK_SUPPRESSION: is a campaign/objective with
    this exact fingerprint already actively (non-stale, non-released) owned
    by ANY session, including the caller? Mirrors job_ledger.
    find_active_by_fingerprint() / evolution.proposal.find_open_by_
    fingerprint()'s "active work only, not all history" semantics, applied
    at the session-ownership layer instead of the job/proposal layer."""
    sweep_stale()
    for lease in list_all():
        if lease.status != "active":
            continue
        if lease.fingerprint == fingerprint:
            return lease
    return None
