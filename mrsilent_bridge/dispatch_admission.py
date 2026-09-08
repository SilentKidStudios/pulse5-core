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

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

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

# COLD-SWAP REFINEMENT (2026-09-05, WALK-AWAY CONVERGENCE closure pass):
# real host evidence this campaign found swap sitting at ~100% used for
# many hours with vmstat si/so at zero the entire time and 8+GB of
# genuinely available RAM — a hard "swap >= 90%" veto treats that
# identically to active thrashing, which it is not. This override applies
# ONLY when swap usage is stale (no pswpin/pswpout movement since the last
# durable sample) AND available memory is comfortably healthy; it still
# refuses outright whenever swap is genuinely active, and even when both
# conditions hold it never lifts the gate to full concurrency — only a
# small, explicitly bounded allowance (see COLD_SWAP_BOUNDED_CONCURRENCY_
# CAP). Ambiguous evidence (no prior durable sample, or a stale/corrupt
# one) fails closed to "cannot confirm cold — treat as active" exactly
# like every other fail-closed default in this module.
SWAP_ACTIVITY_STATE_PATH = Path(__file__).resolve().parent / "dispatch_admission_state" / "swap_activity.json"
MIN_HEALTHY_AVAILABLE_MB_FOR_COLD_SWAP_OVERRIDE = 2048
COLD_SWAP_BOUNDED_CONCURRENCY_CAP = 2
SWAP_SAMPLE_MAX_AGE_S = 3600  # a durable sample older than this is too stale to trust as "no change since"

# SWAP-ACTIVITY RATE NORMALIZATION (2026-09-06 fix): a natural certified
# timer cycle mechanically proved the previous "any nonzero cumulative
# pswpin/pswpout delta = active" check false-positives on trivial incidental
# movement. Real host evidence from that cycle: a durable sample vs. the
# next read showed +11 pswpin / +34 pswpout (45 pages, ~180KB) accumulated
# over an interval up to SWAP_SAMPLE_MAX_AGE_S, while every bounded 10s
# vmstat si/so sample read exactly zero and memory PSI (some/full avg10)
# read 0.00 — unambiguous evidence of no real ongoing paging. A single
# equality check cannot distinguish that from genuine thrashing, so the
# comparison is now normalized by elapsed time into a combined
# pswpin+pswpout pages-per-second rate, averaged over the full validated
# (non-stale, non-skewed) sample interval. Threshold rationale: 1 page/s
# sustained (4KB/s, ~14MB/hour) is roughly two orders of magnitude above
# the measured incidental noise above, yet far below what real swap
# thrashing produces (sustained multi-MB/s paging, i.e. hundreds+ pages/s).
# It stays a strict >= comparison so it remains conservative (biased
# toward ACTIVE, matching the module's fail-closed doctrine) while no
# longer flagging single incidental pages moved over a long interval.
SWAP_ACTIVITY_RATE_THRESHOLD_PAGES_PER_S = 1.0

# Two ADDITIONAL required conditions for the cold-swap override, closing a
# real gap found during Founder review (2026-09-05): "swap is cold + memory
# looks available" alone does not prove the host is genuinely healthy — PSI
# (kernel-measured actual task-stalling) and recent OOM-kill history are
# stronger, more direct distress signals that must ALSO be clear. Both
# fail closed (treated as unhealthy) on any read failure — an older kernel
# without PSI support, or no permission/binary for journalctl, must never
# be silently treated as "therefore healthy". Scoped ONLY to the cold-swap
# override decision, not general admission — a host with no PSI support at
# all simply never gets the override (falls back to the pre-existing,
# already-proven "refuse under high swap" behavior), rather than every
# admission decision on such a host being blocked.
MAX_PSI_MEMORY_AVG10_FOR_COLD_SWAP_PCT = 5.0
OOM_KILL_LOOKBACK_S = 1200  # 20 minutes -- comfortably covers a 15-minute natural-cycle gap


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
    swap_active: bool = True        # fail-closed default: unknown/unmeasured swap activity is treated as active
    psi_memory_avg10: float = 100.0  # fail-closed default: unreadable PSI is treated as maximally stalled
    oom_kill_recent: bool = True     # fail-closed default: unconfirmed OOM history is treated as "assume recent"


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


def _read_vmstat_swap_counters() -> tuple[int, int] | None:
    """Reads /proc/vmstat's cumulative pswpin/pswpout counters. Returns
    None if unreadable/malformed — callers must fail closed on that, never
    guess a value."""
    try:
        pswpin = pswpout = None
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("pswpin "):
                    pswpin = int(line.split()[1])
                elif line.startswith("pswpout "):
                    pswpout = int(line.split()[1])
        if pswpin is None or pswpout is None:
            return None
        return pswpin, pswpout
    except (OSError, ValueError, IndexError):
        return None


def detect_swap_activity(*, state_path: Path | None = None) -> bool:
    """DURABLE cross-cycle swap-activity detection: compares the current
    cumulative pswpin/pswpout counters against the last durably-recorded
    sample (persisted across process restarts — each natural cycle is a
    fresh process, so an in-memory-only comparison would never see a
    "previous" sample at all), normalized by elapsed time into a combined
    pages/second rate (see SWAP_ACTIVITY_RATE_THRESHOLD_PAGES_PER_S) rather
    than a bare "did either counter move at all" equality check — a single
    incidental page swapped over a long interval must not read the same as
    sustained thrashing. Returns True (active) only when a valid, recent,
    non-corrupt prior sample exists AND the normalized rate meets or
    exceeds the threshold; False (cold) when a valid prior sample shows
    exactly zero movement, or nonzero movement at a rate below threshold.
    Fails closed to True (assume active) for every ambiguous case:
    unreadable /proc/vmstat, no prior sample yet, a corrupt/malformed prior
    sample, a prior sample too old to trust (SWAP_SAMPLE_MAX_AGE_S),
    clock skew (negative elapsed time), or counters that moved backwards
    (a reboot or counter reset invalidates the comparison) — never guesses
    "cold" without real, trustworthy evidence."""
    state_path = state_path if state_path is not None else SWAP_ACTIVITY_STATE_PATH
    current = _read_vmstat_swap_counters()
    now = time.time()
    if current is None:
        return True  # cannot read real evidence — fail closed to active
    pswpin, pswpout = current

    previous = None
    try:
        if state_path.exists():
            loaded = json.loads(state_path.read_text())
            if isinstance(loaded, dict):
                previous = loaded
    except (OSError, json.JSONDecodeError):
        previous = None

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps({"pswpin": pswpin, "pswpout": pswpout, "sampled_at": now}))
        os.replace(tmp, state_path)
    except OSError:
        pass  # a failure to persist the new sample must not crash admission — next call just fails closed again

    if previous is None:
        return True  # no prior sample to compare against — fail closed to active

    prev_pswpin = previous.get("pswpin")
    prev_pswpout = previous.get("pswpout")
    prev_sampled_at = previous.get("sampled_at")
    if (
        not isinstance(prev_pswpin, int) or isinstance(prev_pswpin, bool)
        or not isinstance(prev_pswpout, int) or isinstance(prev_pswpout, bool)
        or not isinstance(prev_sampled_at, (int, float)) or isinstance(prev_sampled_at, bool)
    ):
        return True  # corrupt/malformed durable state — fail closed

    age_s = now - prev_sampled_at
    if age_s > SWAP_SAMPLE_MAX_AGE_S or age_s < 0:
        return True  # prior sample too old (or clock skew) to trust as "no change since" — fail closed

    delta_in = pswpin - prev_pswpin
    delta_out = pswpout - prev_pswpout
    if delta_in < 0 or delta_out < 0:
        return True  # counters moved backwards (reboot/counter reset) — untrustworthy, fail closed

    total_delta_pages = delta_in + delta_out
    if total_delta_pages == 0:
        return False  # exact zero movement since a trusted recent sample — genuinely cold

    if age_s == 0:
        return True  # nonzero movement with no measurable elapsed time — cannot compute a rate, fail closed

    rate_pages_per_s = total_delta_pages / age_s
    return rate_pages_per_s >= SWAP_ACTIVITY_RATE_THRESHOLD_PAGES_PER_S


def _read_psi_memory_avg10() -> float:
    """Reads /proc/pressure/memory's "some" line avg10 value (percent of
    the last 10 seconds at least one task was stalled waiting on memory —
    a real, kernel-measured distress signal, not inferred from free/used
    numbers). Returns 100.0 (worst case) if unreadable — an older kernel,
    or a cgroup/container config without PSI exposed — so a host that
    cannot report this is never silently assumed healthy for the purposes
    of the cold-swap override that consumes this value."""
    try:
        with open("/proc/pressure/memory") as f:
            for line in f:
                if line.startswith("some "):
                    for field in line.split():
                        if field.startswith("avg10="):
                            return float(field.split("=", 1)[1])
    except (OSError, ValueError, IndexError):
        pass
    return 100.0


def _detect_recent_oom_kill(*, lookback_s: int = OOM_KILL_LOOKBACK_S) -> bool:
    """Bounded, best-effort check for a real OOM-kill event in the recent
    kernel log via `journalctl -k --since`. Fails closed to True (assume a
    recent kill) on ANY failure: journalctl missing, erroring, timing out,
    or unexpected output — never silently treated as "no kill happened"
    just because the check itself couldn't be completed."""
    try:
        result = subprocess.run(
            ["journalctl", "-k", "--no-pager", "--since", f"-{lookback_s}s"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return True
        text = result.stdout.lower()
        return ("killed process" in text) or ("out of memory" in text)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return True


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
        swap_active=detect_swap_activity(),
        psi_memory_avg10=_read_psi_memory_avg10(),
        oom_kill_recent=_detect_recent_oom_kill(),
    )


def _cold_swap_override_eligible(vector: ResourceVector) -> bool:
    """True only when ALL FOUR independent real signals look safe:
      1. swap is PROVABLY inactive (no pswpin/pswpout movement since the
         last durable, recent-enough sample — see detect_swap_activity());
      2. available memory is comfortably above
         MIN_HEALTHY_AVAILABLE_MB_FOR_COLD_SWAP_OVERRIDE;
      3. PSI memory "some avg10" is at or below MAX_PSI_MEMORY_AVG10_FOR_
         COLD_SWAP_PCT — a real, kernel-measured stalling signal, not
         inferred from free/used numbers alone (closes the exact gap
         Founder review identified: nominal "available RAM" existing does
         not by itself prove the host isn't genuinely under pressure);
      4. no OOM-kill event in the recent kernel log (see
         _detect_recent_oom_kill()) — a strong, unambiguous distress
         signal that overrides everything else if present.
    Any one of these being unfavorable — or unreadable, since all four
    fields default to their conservative/fail-closed value — disqualifies
    the override entirely; it never becomes "mostly safe.\""""
    return (
        not vector.swap_active
        and vector.mem_available_mb >= MIN_HEALTHY_AVAILABLE_MB_FOR_COLD_SWAP_OVERRIDE
        and vector.psi_memory_avg10 <= MAX_PSI_MEMORY_AVG10_FOR_COLD_SWAP_PCT
        and not vector.oom_kill_recent
    )


def admission_decision(
    vector: ResourceVector,
    *,
    required_mem_mb: float = DEFAULT_PER_WORKER_MEM_MB,
) -> tuple[bool, str]:
    """Pure boolean go/no-go for admitting ONE more job right now, given a
    resource snapshot. Fails closed: any condition below refuses admission
    with a specific, loggable reason rather than a bare False.

    COLD-SWAP REFINEMENT: swap at/above MAX_SWAP_USED_PCT no longer
    unconditionally refuses admission — if _cold_swap_override_eligible()
    confirms the swap pressure is stale (not actively thrashing) and
    available memory is genuinely healthy, ONE bounded admission is still
    allowed (dynamic_safe_concurrency() separately caps how many can run
    concurrently under this override — see COLD_SWAP_BOUNDED_CONCURRENCY_
    CAP). Actively thrashing swap, or cold swap without healthy available
    memory, still refuses exactly as before.

    DIRECT_PSI_OOM_GATE (2026-09-08): psi_memory_avg10/oom_kill_recent were
    collected on every ResourceVector but, before this fix, only ever
    consulted inside _cold_swap_override_eligible() — a branch this
    function only even reaches when vector.swap_total_mb > 0 AND swap is
    already over MAX_SWAP_USED_PCT. A swapless host (real example: the
    live node_capability_registry_state record for render-forge-01,
    swap_total_mb==0.0) can never enter that branch at all, so a real,
    kernel-measured distress signal — psi_memory_avg10==100.0 (maximally
    stalled) and oom_kill_recent==True (an actual OOM kill already
    happened) — was silently invisible to admission_decision(): mem_
    available_mb alone (20GB, nominally healthy) was enough to admit.
    "Available RAM" not proving genuine headroom is exactly the gap
    _cold_swap_override_eligible()'s own docstring already names for the
    swap-triggered path; this closes the identical gap for the swapless
    path, using the SAME two real signals, never a new inferred one."""
    if vector.oom_kill_recent:
        return False, "a real OOM-kill event occurred recently on this host — refusing admission regardless of reported available memory"
    if vector.psi_memory_avg10 > MAX_PSI_MEMORY_AVG10_FOR_COLD_SWAP_PCT:
        return False, (
            f"memory pressure (PSI some avg10): {vector.psi_memory_avg10:.1f}% "
            f"(limit {MAX_PSI_MEMORY_AVG10_FOR_COLD_SWAP_PCT:.1f}%) — genuine kernel-measured "
            "stalling, independent of nominally 'available' memory"
        )
    if vector.mem_available_mb - required_mem_mb < MIN_FREE_MEM_MB:
        return False, (
            f"insufficient free memory: {vector.mem_available_mb:.0f}MB available, "
            f"needs {required_mem_mb:.0f}MB plus a {MIN_FREE_MEM_MB}MB floor"
        )
    if vector.swap_total_mb > 0:
        swap_used_pct = vector.swap_used_mb / vector.swap_total_mb * 100.0
        if swap_used_pct >= MAX_SWAP_USED_PCT:
            if _cold_swap_override_eligible(vector):
                pass  # cold + healthy available memory — bounded admission proceeds
            else:
                reason = "actively thrashing" if vector.swap_active else "stale but available memory is not healthy enough"
                return False, f"swap pressure: {swap_used_pct:.0f}% used (limit {MAX_SWAP_USED_PCT:.0f}%), {reason}"
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
    ceiling is raised to match). COLD-SWAP REFINEMENT: swap at/above
    MAX_SWAP_USED_PCT still returns 0 unless _cold_swap_override_eligible()
    confirms the pressure is stale and available memory is healthy, in
    which case the result is capped at COLD_SWAP_BOUNDED_CONCURRENCY_CAP
    regardless of how favorable the memory/CPU-bound math looks — a
    deliberately small, cautious allowance, never full throttle."""
    if vector.swap_total_mb > 0 and (vector.swap_used_mb / vector.swap_total_mb * 100.0) >= MAX_SWAP_USED_PCT:
        if not _cold_swap_override_eligible(vector):
            return 0  # real/unconfirmed swap pressure: do not plan any new concurrent work
    if vector.disk_used_pct >= MAX_DISK_USED_PCT:
        return 0
    usable_mem_mb = max(0.0, vector.mem_available_mb - MIN_FREE_MEM_MB)
    mem_bound = int(usable_mem_mb // per_worker_mem_mb) if per_worker_mem_mb > 0 else MAX_ABSOLUTE_CONCURRENCY
    usable_cpus = max(0, vector.cpu_count - reserve_cpus)
    headroom_per_cpu = max(0.0, MAX_LOAD_PER_CPU - (vector.load_avg_1m / vector.cpu_count if vector.cpu_count else 0.0))
    cpu_bound = int(usable_cpus * (headroom_per_cpu / MAX_LOAD_PER_CPU)) if vector.cpu_count else 0
    result = max(0, min(mem_bound, cpu_bound, MAX_ABSOLUTE_CONCURRENCY))
    if vector.swap_total_mb > 0 and (vector.swap_used_mb / vector.swap_total_mb * 100.0) >= MAX_SWAP_USED_PCT:
        result = min(result, COLD_SWAP_BOUNDED_CONCURRENCY_CAP)  # cold-swap override: bounded, never full throttle
    return result


def concurrency_limit_reason(vector: ResourceVector, *, per_worker_mem_mb: float = DEFAULT_PER_WORKER_MEM_MB, reserve_cpus: int = RESERVE_CPUS) -> str:
    """Human-readable explanation of which resource is currently the
    binding constraint on dynamic_safe_concurrency()'s result — required by
    campaign section 7 (CONCURRENCY_LIMIT_REASON)."""
    if vector.swap_total_mb > 0 and (vector.swap_used_mb / vector.swap_total_mb * 100.0) >= MAX_SWAP_USED_PCT:
        if not _cold_swap_override_eligible(vector):
            return "swap_exhausted"
        return "swap_cold_bounded"  # cold-swap override active — see dynamic_safe_concurrency()'s cap
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
