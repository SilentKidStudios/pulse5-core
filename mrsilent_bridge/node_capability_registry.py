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
health/capability metadata (NODE_CAPABILITY_REGISTRY, NODE_HEALTH).

2026-09-07 Founder authorization (narrow, explicit): probe_ssh_node_
health() may connect to a remote node (e.g. render-forge-01) over SSH and
run READ-ONLY hostname/CPU/RAM/GPU/VRAM/disk commands for capability
observation only. That authorization explicitly does NOT cover task
dispatch, remote writes, service restarts, package/model changes, or
credential changes — see probe_ssh_node_health()'s own docstring. This
module still does NOT implement real remote task submission/execution
against any external host; render-forge-01 remains a separate, shared
piece of production infrastructure (it runs Scorpio's Corner, which is
protected and untouched by anything here) this campaign does not have
standing to issue real remote jobs against without further separate
authorization. A future slice can wire an actual REMOTE_EXECUTION_
PROVENANCE/RESULT_RETURN transport behind placement_decision()'s result
without changing this module's contract.

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
import subprocess
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
    # Non-CPU/mem/disk observation data (e.g. remote hostname, GPU/VRAM) that
    # doesn't fit da.ResourceVector's admission-decision contract. Additive
    # and optional so existing serialized records/tests are unaffected.
    extra: dict[str, Any] | None = None
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


def record_health(
    node_id: str, status: str, *,
    resource_vector: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> NodeRecord:
    """NODE_HEALTH: the ONLY way a node's health_status changes — never
    inferred, never assumed. A caller with a real remote probe calls this
    after that probe; nothing in this module invents a probe result."""
    record = load(node_id)
    if record is None:
        raise ValueError(f"no registered node {node_id!r} — call register_node() first")
    now = _now()
    record.health_status = status
    record.resource_vector = resource_vector
    record.extra = extra
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


SSH_PROBE_TIMEOUT_S = 10

# Founder-authorized (2026-09-07), READ-ONLY CAPABILITY OBSERVATION ONLY:
# hostname/CPU/RAM/GPU/VRAM/disk visibility for placement scoring. This
# authorization explicitly does NOT extend to task dispatch, remote writes,
# service control, model/package changes, or credential changes — see
# probe_ssh_node_health()'s docstring. Nothing below issues a mutating
# remote command.
_REMOTE_READ_ONLY_PROBE_SCRIPT = (
    "hostname; "
    "nproc; "
    "cat /proc/loadavg; "
    "free -m | awk '/^Mem:/{print $2, $7}'; "
    "free -m | awk '/^Swap:/{print $2, $3}'; "
    "df -BG / | awk 'NR==2{gsub(\"G\",\"\",$2); gsub(\"G\",\"\",$4); print $2, $4}'; "
    "nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu "
    "--format=csv,noheader,nounits 2>/dev/null || echo NO_GPU"
)


def probe_ssh_node_health(
    node_id: str,
    *,
    ssh_target: str,
    ssh_key: str | None = None,
    capabilities: list[str] | None = None,
    timeout_s: int = SSH_PROBE_TIMEOUT_S,
) -> NodeRecord:
    """Real, READ-ONLY remote health probe over SSH — capability
    observation only. Founder authorization for this function covers
    exactly: connecting via existing SSH credentials, running read-only
    hostname/CPU/RAM/GPU/VRAM/disk commands, and recording the result.
    It never dispatches work, never writes to the remote host, never
    restarts anything, and never touches credentials. A future slice
    would need separate authorization to add any of that.

    FAIL_CLOSED_IF_NODE_UNAVAILABLE applies identically to remote nodes:
    any failure (unreachable host, auth failure, timeout, unparseable
    output) marks the node UNREACHABLE rather than guessing, and a
    strict subprocess timeout ensures an unreachable/slow remote host
    cannot stall the caller.

    Never logs ssh_key or command output — only the parsed, structured
    result is returned/persisted.
    """
    if capabilities is not None:
        register_node(node_id, capabilities=capabilities, endpoint=ssh_target)
    elif load(node_id) is None:
        register_node(node_id, capabilities=[], endpoint=ssh_target)

    ssh_cmd = [
        "ssh", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout_s}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if ssh_key:
        ssh_cmd += ["-i", ssh_key]
    ssh_cmd += [ssh_target, _REMOTE_READ_ONLY_PROBE_SCRIPT]

    try:
        result = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout_s + 5,
        )
        if result.returncode != 0:
            return record_health(node_id, UNREACHABLE)
        lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
        if len(lines) < 6:
            return record_health(node_id, UNREACHABLE)

        remote_hostname = lines[0]
        cpu_count = int(lines[1])
        load_avg_1m = float(lines[2].split()[0])
        mem_total_mb, mem_available_mb = (float(x) for x in lines[3].split())
        swap_total_mb, swap_used_mb = (float(x) for x in lines[4].split())
        disk_total_gb, disk_free_gb = (float(x) for x in lines[5].split())
        disk_used_pct = (
            (disk_total_gb - disk_free_gb) / disk_total_gb * 100.0 if disk_total_gb else 0.0
        )

        gpu_info: dict[str, Any] = {"present": False}
        if len(lines) >= 7 and lines[6] != "NO_GPU":
            parts = [p.strip() for p in lines[6].split(",")]
            if len(parts) == 4:
                gpu_info = {
                    "present": True,
                    "name": parts[0],
                    "vram_total_mb": float(parts[1]),
                    "vram_used_mb": float(parts[2]),
                    "utilization_pct": float(parts[3]),
                }

        vector = da.ResourceVector(
            cpu_count=cpu_count, load_avg_1m=load_avg_1m,
            mem_total_mb=mem_total_mb, mem_available_mb=mem_available_mb,
            swap_total_mb=swap_total_mb, swap_used_mb=swap_used_mb,
            disk_total_gb=disk_total_gb, disk_free_gb=disk_free_gb,
            disk_used_pct=disk_used_pct,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        return record_health(node_id, UNREACHABLE)

    allowed, _reason = da.admission_decision(vector)
    return record_health(
        node_id, HEALTHY if allowed else DEGRADED,
        resource_vector=asdict(vector),
        extra={"hostname": remote_hostname, "gpu": gpu_info},
    )


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
