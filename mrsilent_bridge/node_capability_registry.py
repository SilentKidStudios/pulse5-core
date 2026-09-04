"""
Node Capability Registry — durable node health + capability-aware placement
decisions, WALK-AWAY CONVERGENCE gap-closure slice 6/N.

The audit's resource/multi-node sub-report found the existing "node fabric"
(node_fabric/registry/expanded_node_registry.json, services/mr_silent_node_
dispatch_board.py and siblings) is untracked in git AND is theater: each
script's main() writes a hardcoded static Python dict to a JSON file every
run — no live health probe, no real scoring, no actual placement logic. It
is not "canonical authority" in any reviewable sense (no git history, no
diffable provenance) — this module does not extend or duplicate it; it is
a genuine, tracked, tested first implementation of the missing capability,
deliberately kept separate from that untracked/uncertain-ownership area.

Scope, deliberately bounded: this module answers "which registered node
SHOULD this work item run on" (PLACEMENT_DECISION) and tracks node
health/capability metadata (NODE_CAPABILITY_REGISTRY, NODE_HEALTH). It does
NOT implement real remote task submission/execution against any external
host (e.g. render-forge-01, a separate, shared piece of infrastructure this
campaign does not have clear standing to issue real remote jobs against
without separate explicit authorization — see FAIL_CLOSED_IF_NODE_
UNAVAILABLE below, which is exactly the safety property that matters when
no such execution path exists yet). A future slice can wire an actual
REMOTE_EXECUTION_PROVENANCE/RESULT_RETURN transport behind
placement_decision()'s result without changing this module's contract.

FAIL_CLOSED_IF_NODE_UNAVAILABLE: a node with no health record, a stale
health record, or an explicit "unreachable"/"degraded" status is NEVER
selected — placement_decision() falls back to the local node (always
registered from this host's own real dispatch_admission.read_resource_
vector()) or to (None, reason) if even local capacity is insufficient,
rather than ever guessing a remote node is fine.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dispatch_admission as da

BRIDGE_ROOT = Path(__file__).resolve().parent
REGISTRY_ROOT = BRIDGE_ROOT / "node_capability_registry_state"
NODES_DIR = REGISTRY_ROOT / "nodes"

LOCAL_NODE_ID = "local"
HEALTH_STALE_AFTER_S = 300  # a health record older than this is untrustworthy — fail closed

HEALTHY = "healthy"
DEGRADED = "degraded"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


@dataclass
class NodeRecord:
    node_id: str
    capabilities: list[str] = field(default_factory=list)
    endpoint: str | None = None       # informational only — no code here calls it
    health_status: str = UNKNOWN
    resource_vector: dict[str, Any] | None = None
    registered_at: str = ""
    updated_at: str = ""
    last_health_check_at: str = ""


def _path(node_id: str) -> Path:
    return NODES_DIR / f"{node_id}.json"


def load(node_id: str) -> NodeRecord | None:
    p = _path(node_id)
    if not p.exists():
        return None
    try:
        return NodeRecord(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError):
        return None


def list_all() -> list[NodeRecord]:
    if not NODES_DIR.exists():
        return []
    out = []
    for p in sorted(NODES_DIR.glob("*.json")):
        try:
            out.append(NodeRecord(**json.loads(p.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def register_node(node_id: str, *, capabilities: list[str], endpoint: str | None = None) -> NodeRecord:
    """Idempotent — re-registering an existing node updates its
    capabilities/endpoint without resetting its health history."""
    existing = load(node_id)
    now = _now()
    if existing is not None:
        existing.capabilities = sorted(set(capabilities))
        existing.endpoint = endpoint
        existing.updated_at = now
        _atomic_write_json(_path(node_id), asdict(existing))
        return existing
    record = NodeRecord(
        node_id=node_id, capabilities=sorted(set(capabilities)), endpoint=endpoint,
        health_status=UNKNOWN, registered_at=now, updated_at=now, last_health_check_at="",
    )
    _atomic_write_json(_path(node_id), asdict(record))
    return record


def record_health(node_id: str, status: str, *, resource_vector: dict[str, Any] | None = None) -> NodeRecord:
    """NODE_HEALTH: the ONLY way a node's health_status changes — never
    inferred, never assumed. A caller with a real remote probe (future
    slice) calls this after that probe; nothing in this module invents a
    probe result."""
    record = load(node_id)
    if record is None:
        raise ValueError(f"no registered node {node_id!r} — call register_node() first")
    now = _now()
    record.health_status = status
    record.resource_vector = resource_vector
    record.last_health_check_at = now
    record.updated_at = now
    _atomic_write_json(_path(node_id), asdict(record))
    return record


def is_healthy(record: NodeRecord, *, stale_after_s: int = HEALTH_STALE_AFTER_S) -> bool:
    """FAIL_CLOSED_IF_NODE_UNAVAILABLE: True only if the last real health
    check said "healthy" AND that check itself is recent. A never-checked
    node, a stale check, or an explicit non-healthy status are all treated
    identically — not eligible — never guessed as fine."""
    if record.health_status != HEALTHY:
        return False
    if not record.last_health_check_at:
        return False
    try:
        checked = datetime.fromisoformat(record.last_health_check_at)
    except (ValueError, TypeError):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - checked).total_seconds() <= stale_after_s


def ensure_local_node_registered() -> NodeRecord:
    """The local host (this checkout's own machine, e.g. pulse5-core-01) is
    always available as a placement target without needing an external
    health probe — its own dispatch_admission.read_resource_vector() IS its
    real, live health check, taken fresh on every call."""
    vector = da.read_resource_vector()
    record = register_node(LOCAL_NODE_ID, capabilities=["cpu", "local"])
    allowed, _reason = da.admission_decision(vector)
    record_health(
        LOCAL_NODE_ID, HEALTHY if allowed else DEGRADED,
        resource_vector=asdict(vector),
    )
    return load(LOCAL_NODE_ID)


@dataclass
class PlacementDecision:
    node_id: str | None
    reason: str


def placement_decision(
    *,
    required_capabilities: list[str] | None = None,
    required_mem_mb: float = da.DEFAULT_PER_WORKER_MEM_MB,
) -> PlacementDecision:
    """PLACEMENT_DECISION: pick the best HEALTHY node whose capabilities
    are a superset of required_capabilities and whose most recent resource
    vector shows enough headroom, preferring the highest dynamic_safe_
    concurrency among eligible candidates. Ensures the local node's own
    live health snapshot is fresh before deciding, so "no remote node
    registered/healthy" still correctly falls back to a REAL, just-checked
    local capacity answer rather than a stale one.
    FAIL_CLOSED_IF_NODE_UNAVAILABLE: returns (None, reason) rather than
    guessing when nothing eligible exists — callers must treat that as
    "do not dispatch this work anywhere right now", not as a license to
    pick something anyway."""
    required_capabilities = required_capabilities or []
    ensure_local_node_registered()

    best: NodeRecord | None = None
    best_concurrency = -1
    for record in list_all():
        if not set(required_capabilities).issubset(set(record.capabilities)):
            continue
        if not is_healthy(record):
            continue
        if record.resource_vector is None:
            continue
        vector = da.ResourceVector(**record.resource_vector)
        allowed, _reason = da.admission_decision(vector, required_mem_mb=required_mem_mb)
        if not allowed:
            continue
        concurrency = da.dynamic_safe_concurrency(vector, per_worker_mem_mb=required_mem_mb)
        if concurrency > best_concurrency:
            best = record
            best_concurrency = concurrency

    if best is None:
        return PlacementDecision(
            node_id=None,
            reason="no healthy node satisfies required_capabilities with sufficient real headroom",
        )
    return PlacementDecision(node_id=best.node_id, reason=f"selected {best.node_id} (safe_concurrency={best_concurrency})")
