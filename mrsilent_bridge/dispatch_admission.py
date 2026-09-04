"""
Dispatch Admission Control — dynamic, resource-aware concurrency for the
general work-graph dispatcher (see scheduler.py).

WALK-AWAY CONVERGENCE gap-closure, slice 3/N. The audit's resource/
concurrency sub-report found real host-resource checking exists ONLY inside
omni_device_lab/android/resource_admission.py (untracked, scoped to one
Android certification harness — a boolean go/no-go on a single fixed MB
floor) — the general dispatch path (evolution/advance.py's advance_eligible,
a plain serial `for` loop) has NO resource check before starting a job at
all, on a host the same audit found with swap fully exhausted and disk at
86% used. This module is the general, tracked, dependency-minimal
replacement gap: not a copy of that Android-scoped file (deliberately a
different name/path/purpose — that file is untracked and of uncertain
concurrent-edit ownership; this module never imports or depends on it), a
genuine "how many independent jobs can I safely run RIGHT NOW" computation,
usable by any dispatcher, not just one Android harness.

Uses only the standard library (no psutil dependency) — /proc parsing and
os.getloadavg()/shutil.disk_usage(), the same primitives the Android-scoped
harness used, reimplemented generally.

Read functions (read_resource_vector, and the /proc parsers it calls) touch
only the live host's real kernel-exposed state — never the repo checkout —
so they carry no worktree-isolation risk; only the pure decision functions
below are unit-tested directly with synthetic vectors, since a test cannot
control (and must not fight to control) the real live /proc/meminfo.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

# Conservative defaults, chosen against THIS project's own observed reality
# (2026-09-04 audit): an 8-vCPU host with swap fully exhausted and disk at
# 86%. Deliberately strict — false-negative (declining to admit one more
# job when it would actually have been fine) is far cheaper here than
# false-positive (admitting a job that tips a resource-starved host into
# OOM or fills the root disk).
MIN_FREE_MEM_MB = 1024
MAX_LOAD_PER_CPU = 1.5
MAX_DISK_USED_PCT = 90.0
MAX_SWAP_USED_PCT = 90.0
DEFAULT_PER_WORKER_MEM_MB = 512
RESERVE_CPUS = 1                 # never plan to use every last core for job workers
MAX_ABSOLUTE_CONCURRENCY = 64    # sanity ceiling; raise this constant once real
                                  # many-core/supercomputer hardware is registered —
                                  # a config change, not an architectural one.

QUEUE_HIGH_WATER = 40
QUEUE_LOW_WATER = 5


@dataclass
class ResourceVector:
    cpu_count: int
    load_avg_1m: float
    mem_total_mb: float
    mem_available_mb: float
    swap_total_mb: float
    swap_used_mb: float
    disk_total_gb: float
    disk_free_gb: float
    disk_used_pct: float


def _read_meminfo() -> dict[str, float]:
    """Parses /proc/meminfo into {key: value_in_mb}. Every value in
    /proc/meminfo is reported in kB regardless of the trailing unit label
    actually printed, per `man proc`."""
    values: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                num = parts[1].strip().split()[0]
                try:
                    values[key] = float(num) / 1024.0  # kB -> MB
                except ValueError:
                    continue
    except OSError:
        pass
    return values


def read_resource_vector(*, path: str = "/") -> ResourceVector:
    meminfo = _read_meminfo()
    mem_total = meminfo.get("MemTotal", 0.0)
    mem_available = meminfo.get("MemAvailable", 0.0)
    swap_total = meminfo.get("SwapTotal", 0.0)
    swap_free = meminfo.get("SwapFree", 0.0)
    swap_used = max(0.0, swap_total - swap_free)
    try:
        load1, _, _ = os.getloadavg()
    except OSError:
        load1 = 0.0
    cpu_count = os.cpu_count() or 1
    usage = shutil.disk_usage(path)
    disk_total_gb = usage.total / (1024 ** 3)
    disk_free_gb = usage.free / (1024 ** 3)
    disk_used_pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return ResourceVector(
        cpu_count=cpu_count, load_avg_1m=load1,
        mem_total_mb=mem_total, mem_available_mb=mem_available,
        swap_total_mb=swap_total, swap_used_mb=swap_used,
        disk_total_gb=disk_total_gb, disk_free_gb=disk_free_gb, disk_used_pct=disk_used_pct,
    )


def admission_decision(
    vector: ResourceVector,
    *,
    required_mem_mb: float = DEFAULT_PER_WORKER_MEM_MB,
) -> tuple[bool, str]:
    """Pure boolean go/no-go for admitting ONE more job right now, given a
    resource snapshot. Fails closed: any condition below refuses admission
    with a specific, loggable reason rather than a bare False."""
    if vector.mem_available_mb - required_mem_mb < MIN_FREE_MEM_MB:
        return False, (
            f"insufficient free memory: {vector.mem_available_mb:.0f}MB available, "
            f"needs {required_mem_mb:.0f}MB plus a {MIN_FREE_MEM_MB}MB floor"
        )
    if vector.swap_total_mb > 0:
        swap_used_pct = vector.swap_used_mb / vector.swap_total_mb * 100.0
        if swap_used_pct >= MAX_SWAP_USED_PCT:
            return False, f"swap pressure: {swap_used_pct:.0f}% used (limit {MAX_SWAP_USED_PCT:.0f}%)"
    if vector.disk_used_pct >= MAX_DISK_USED_PCT:
        return False, f"disk pressure: {vector.disk_used_pct:.0f}% used (limit {MAX_DISK_USED_PCT:.0f}%)"
    if vector.cpu_count > 0 and (vector.load_avg_1m / vector.cpu_count) >= MAX_LOAD_PER_CPU:
        return False, (
            f"load pressure: {vector.load_avg_1m:.2f} 1m-load / {vector.cpu_count} cpus "
            f"= {vector.load_avg_1m / vector.cpu_count:.2f} (limit {MAX_LOAD_PER_CPU:.2f})"
        )
    return True, "admitted"


def dynamic_safe_concurrency(
    vector: ResourceVector,
    *,
    per_worker_mem_mb: float = DEFAULT_PER_WORKER_MEM_MB,
    reserve_cpus: int = RESERVE_CPUS,
) -> int:
    """DYNAMIC_MAX_SAFE_CONCURRENCY: how many additional independent workers
    can safely start RIGHT NOW, given this exact resource snapshot — not a
    hardcoded constant. Memory-bound and CPU-bound estimates are both
    computed; the tighter one wins (a host can be constrained by either).
    Floors at 0 (never negative), caps at MAX_ABSOLUTE_CONCURRENCY (a
    documented, easily-raised sanity ceiling — see module docstring — not
    an architectural limit: this same function keeps working unchanged on
    a much larger box, it will simply compute a much larger number once the
    ceiling is raised to match)."""
    if vector.swap_total_mb > 0 and (vector.swap_used_mb / vector.swap_total_mb * 100.0) >= MAX_SWAP_USED_PCT:
        return 0  # real swap pressure: do not plan any new concurrent work
    if vector.disk_used_pct >= MAX_DISK_USED_PCT:
        return 0
    usable_mem_mb = max(0.0, vector.mem_available_mb - MIN_FREE_MEM_MB)
    mem_bound = int(usable_mem_mb // per_worker_mem_mb) if per_worker_mem_mb > 0 else MAX_ABSOLUTE_CONCURRENCY
    usable_cpus = max(0, vector.cpu_count - reserve_cpus)
    headroom_per_cpu = max(0.0, MAX_LOAD_PER_CPU - (vector.load_avg_1m / vector.cpu_count if vector.cpu_count else 0.0))
    cpu_bound = int(usable_cpus * (headroom_per_cpu / MAX_LOAD_PER_CPU)) if vector.cpu_count else 0
    return max(0, min(mem_bound, cpu_bound, MAX_ABSOLUTE_CONCURRENCY))


def concurrency_limit_reason(vector: ResourceVector, *, per_worker_mem_mb: float = DEFAULT_PER_WORKER_MEM_MB, reserve_cpus: int = RESERVE_CPUS) -> str:
    """Human-readable explanation of which resource is currently the
    binding constraint on dynamic_safe_concurrency()'s result — required by
    campaign section 7 (CONCURRENCY_LIMIT_REASON)."""
    if vector.swap_total_mb > 0 and (vector.swap_used_mb / vector.swap_total_mb * 100.0) >= MAX_SWAP_USED_PCT:
        return "swap_exhausted"
    if vector.disk_used_pct >= MAX_DISK_USED_PCT:
        return "disk_exhausted"
    usable_mem_mb = max(0.0, vector.mem_available_mb - MIN_FREE_MEM_MB)
    mem_bound = int(usable_mem_mb // per_worker_mem_mb) if per_worker_mem_mb > 0 else MAX_ABSOLUTE_CONCURRENCY
    usable_cpus = max(0, vector.cpu_count - reserve_cpus)
    headroom_per_cpu = max(0.0, MAX_LOAD_PER_CPU - (vector.load_avg_1m / vector.cpu_count if vector.cpu_count else 0.0))
    cpu_bound = int(usable_cpus * (headroom_per_cpu / MAX_LOAD_PER_CPU)) if vector.cpu_count else 0
    if mem_bound <= cpu_bound and mem_bound < MAX_ABSOLUTE_CONCURRENCY:
        return "memory_bound"
    if cpu_bound < MAX_ABSOLUTE_CONCURRENCY:
        return "cpu_bound"
    return "absolute_ceiling"


def backpressure_status(*, queue_depth: int, admitted_rate: float = 0.0, completion_rate: float = 0.0) -> dict:
    """RESOURCE_BACKPRESSURE required fields (campaign section 16): never
    drops work, never explodes processes — just reports whether the queue
    is deep enough that admission should slow relative to completion.
    BACKPRESSURE_ACTIVE is purely queue-depth-driven (high/low watermark,
    the same watermark pattern the audit found already live for proposal
    reseed in mr_silent_autonomy/auto_reseed.py) — resource exhaustion
    itself is handled by admission_decision()/dynamic_safe_concurrency()
    refusing new admits, not by this function."""
    return {
        "QUEUE_DEPTH": queue_depth,
        "ADMISSION_RATE": admitted_rate,
        "COMPLETION_RATE": completion_rate,
        "RESOURCE_HIGH_WATER_MARK": QUEUE_HIGH_WATER,
        "RESOURCE_LOW_WATER_MARK": QUEUE_LOW_WATER,
        "BACKPRESSURE_ACTIVE": queue_depth >= QUEUE_HIGH_WATER,
    }
