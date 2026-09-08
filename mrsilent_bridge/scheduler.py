"""
Scheduler — ties work_graph.py (durable dependency graph),
dispatch_admission.py (dynamic resource-aware concurrency), and
session_ownership.py (cross-session collision safety) into the bounded,
resource-aware PARALLEL dispatcher the WALK-AWAY CONVERGENCE audit found
missing: the live pipeline's evolution/advance.py::advance_eligible() is a
plain serial `for` loop under one global CYCLE_LOCK_KEY ("single global
lock — only one cycle system-wide, ever").

This module does not replace that lock or that loop — it is a new,
additive scheduling layer for the work_graph, exercised by run_scheduler_pass()
below. A future integration step could have autonomous_cycle.py's Phase S
(omni_registry_stewardship) or a dedicated new phase enqueue work-graph
items and call run_scheduler_pass() once per cycle; that wiring is
DEFERRED (see the campaign's own final receipt — this slice proves the
scheduler mechanism itself, not yet its live wiring into autonomous_cycle.py,
which is untracked-adjacent and needs its own reviewed integration pass).

Execution is pluggable: callers pass an `executor_fn(WorkItem) -> dict`
callable. The scale/load-test harness (see tests/test_scheduler_scale.py)
passes a trivial in-process function to prove the graph/scheduling
mechanics at 10..1000 items without spawning 1000 real OS processes
(campaign section 22 explicitly permits this for the high-count tiers).
Real bounded parallel execution (section 23) is proven separately with a
small number of genuinely independent, safe real callables.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import dispatch_admission as da
import work_graph as wg


@dataclass
class SchedulerPassResult:
    dispatched: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    repairable_failed: list[str] = field(default_factory=list)
    terminal_failed: list[str] = field(default_factory=list)
    capability_unplaceable: list[str] = field(default_factory=list)
    duplicate_claim_rejections: int = 0
    reclaimed_stale: list[str] = field(default_factory=list)
    repair_retried: list[str] = field(default_factory=list)
    peak_concurrent_observed: int = 0
    dynamic_safe_concurrency: int = 0
    concurrency_limit_reason: str = ""
    admission_refused: bool = False
    admission_reason: str = ""


class _ConcurrencyTracker:
    """Thread-safe peak-concurrency counter — real evidence of how many
    genuinely overlapping executions happened, not just how many were
    submitted (REAL_PARALLEL_JOBS_MAX_OBSERVED)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self._current += 1
            self.peak = max(self.peak, self._current)

    def exit(self) -> None:
        with self._lock:
            self._current -= 1


def _run_claimed_item(
    item: wg.WorkItem, executor_fn: Callable[[wg.WorkItem], dict], owner: str,
    tracker: _ConcurrencyTracker,
) -> tuple[str, bool, dict]:
    tracker.enter()
    try:
        result = executor_fn(item)
        return item.work_id, True, (result or {})
    except Exception as exc:  # noqa: BLE001 — a work item's own failure must never crash the scheduler
        return item.work_id, False, {"error": str(exc), "error_class": type(exc).__name__}
    finally:
        tracker.exit()


def _filter_by_capability_placement(items: list[wg.WorkItem]) -> tuple[list[wg.WorkItem], list[str]]:
    """MULTI_NODE_PLACEMENT gate: an item that declares resource_
    requirements["required_capabilities"] (e.g. ["gpu"]) is only placeable
    if node_capability_registry.placement_decision() finds a real,
    currently-healthy node for it — fail-closed exactly like that
    module's own contract (a never-checked/stale/unhealthy node is never
    guessed as fine). An item with no such declaration is unaffected
    (backward compatible with every existing local-only item). An
    unplaceable item is never dropped or marked failed — it simply stays
    RUNNABLE and is excluded from this pass's candidates, exactly like an
    item that loses the resource-admission budget; a later pass (once a
    capable node registers/becomes healthy) picks it up automatically."""
    try:
        import node_capability_registry as ncr
    except ImportError:
        return items, []  # soft dependency — degrade to "no capability gating" if absent

    placeable = []
    unplaceable = []
    for item in items:
        required = (item.resource_requirements or {}).get("required_capabilities")
        if not required:
            placeable.append(item)
            continue
        decision = ncr.placement_decision(required_capabilities=required)
        if decision.node_id is None:
            unplaceable.append(item.work_id)
        else:
            placeable.append(item)
    return placeable, unplaceable


def run_scheduler_pass(
    *,
    self_session_id: str,
    executor_fn: Callable[[wg.WorkItem], dict],
    resource_vector: da.ResourceVector | None = None,
    per_worker_mem_mb: float = da.DEFAULT_PER_WORKER_MEM_MB,
    max_items: int | None = None,
    owner_prefix: str = "scheduler",
) -> SchedulerPassResult:
    """One bounded scheduling pass:
      1. reclaim any stale RUNNING items (crash/restart recovery),
      2. compute the current runnable set (dependency- and
         session-ownership-aware, priority+aging ordered),
      3. compute DYNAMIC_MAX_SAFE_CONCURRENCY from a real (or injected,
         for tests) resource snapshot,
      4. claim and dispatch up to that many items CONCURRENTLY via a
         bounded thread pool, tracking real peak overlap,
      5. mark each result COMPLETED or REPAIRABLE_FAILED (never silently
         dropped — a raised exception in executor_fn becomes a
         repairable-failure record, not a crashed scheduler).
    Never dispatches more than dynamic_safe_concurrency() says is safe
    right now — QUEUED_WORK_CONTINUES_PROGRESS: anything left over simply
    stays RUNNABLE for the next pass, it is never lost or dropped.
    """
    result = SchedulerPassResult()
    result.reclaimed_stale = wg.reclaim_stale()
    result.repair_retried = wg.retry_repairable_failed()

    vector = resource_vector if resource_vector is not None else da.read_resource_vector()
    allowed, reason = da.admission_decision(vector, required_mem_mb=per_worker_mem_mb)
    result.dynamic_safe_concurrency = da.dynamic_safe_concurrency(vector, per_worker_mem_mb=per_worker_mem_mb)
    result.concurrency_limit_reason = da.concurrency_limit_reason(vector, per_worker_mem_mb=per_worker_mem_mb)
    if not allowed:
        result.admission_refused = True
        result.admission_reason = reason
        return result  # fail closed: host pressure means "run nothing new this pass"

    runnable = wg.runnable_items(self_session_id=self_session_id)
    placeable, result.capability_unplaceable = _filter_by_capability_placement(runnable)
    budget = result.dynamic_safe_concurrency
    if max_items is not None:
        budget = min(budget, max_items)

    # FRESH_WORK_LIVENESS (2026-09-06): reserve exactly ONE bounded
    # dispatch slot per pass for the freshest eligible item — see
    # work_graph.freshest_eligible()'s own docstring for the precise
    # starvation-direction gap this closes and why effective_priority()'s
    # existing aging bonus alone does not cover it. Only takes effect
    # when there is real budget headroom (budget >= 2, i.e. never at the
    # cold-swap-bounded cap of 1), a genuinely different freshest item
    # exists outside the top-priority selection already made, AND that
    # item is_meaningfully_fresher() than the marginal item it would
    # displace (work_graph.FRESH_WORK_MIN_AGE_GAP_MINUTES) — without this
    # last check, "pick whichever item is nominally most recent" fires on
    # pure creation-order noise between items created in the same instant
    # (a real regression this session's own test suite caught: a
    # synthetic 3-item batch created back-to-back displaced a
    # deliberately-different, unrelated test item). Old backlog's
    # existing FIFO-by-age fairness is otherwise completely unchanged
    # (still gets budget-1 of budget slots, same ordering as before);
    # this never adds capacity, never bypasses resource admission, and
    # never dispatches more than `budget` items total.
    if budget >= 2 and len(placeable) > budget:
        top = placeable[:budget - 1]
        freshest = wg.freshest_eligible(placeable)
        top_ids = {item.work_id for item in top}
        displaced = placeable[budget - 1]
        if (freshest is not None and freshest.work_id not in top_ids
                and wg.is_meaningfully_fresher(freshest, displaced)):
            candidates = top + [freshest]
        else:
            candidates = placeable[:budget]
    else:
        candidates = placeable[:budget] if budget > 0 else []

    claimed: list[wg.WorkItem] = []
    for item in candidates:
        owner = f"{owner_prefix}:{item.work_id}"
        if wg.claim(item.work_id, owner=owner):
            claimed.append(item)
        else:
            result.duplicate_claim_rejections += 1  # another worker already owns it — never double-dispatch

    if not claimed:
        return result

    tracker = _ConcurrencyTracker()
    with ThreadPoolExecutor(max_workers=max(1, len(claimed))) as pool:
        futures = {
            pool.submit(_run_claimed_item, item, executor_fn, f"{owner_prefix}:{item.work_id}", tracker): item
            for item in claimed
        }
        for future in as_completed(futures):
            item = futures[future]
            work_id, ok, payload = future.result()
            wg.release(work_id, owner=f"{owner_prefix}:{work_id}")
            if ok and payload.get("__terminal_failed__"):
                # a successful executor call that itself concluded the
                # underlying work is permanently, genuinely closed (e.g.
                # proposal_work_bridge's rejected-proposal case) — not a
                # scheduler/executor error, so it did not raise, but also
                # not retry-eligible; mark_repairable_failed()'s bounded
                # retry would be pointless (and, for a proposal, actively
                # wrong: the closed proposal store would refuse to
                # re-advance it anyway).
                wg.mark_terminal_failed(work_id, result=payload)
                result.terminal_failed.append(work_id)
            elif ok:
                wg.mark_completed(work_id, result=payload)
                result.completed.append(work_id)
            else:
                wg.mark_repairable_failed(work_id, result=payload)
                result.repairable_failed.append(work_id)
            result.dispatched.append(work_id)

    result.peak_concurrent_observed = tracker.peak
    return result


def run_until_drained(
    *,
    self_session_id: str,
    executor_fn: Callable[[wg.WorkItem], dict],
    resource_vector: da.ResourceVector | None = None,
    per_worker_mem_mb: float = da.DEFAULT_PER_WORKER_MEM_MB,
    max_passes: int = 200,
    owner_prefix: str = "scheduler",
) -> list[SchedulerPassResult]:
    """Repeats run_scheduler_pass() until nothing runnable remains or
    max_passes is hit (a bounded ceiling — this is a load-test/demo driver,
    never an unbounded loop). Used by the scale-harness tests to drive a
    whole dependency graph (with chains) to completion and prove
    PARENT_AUTOMATIC_RESUMPTION actually fires across passes, not just
    within one."""
    passes: list[SchedulerPassResult] = []
    for _ in range(max_passes):
        pass_result = run_scheduler_pass(
            self_session_id=self_session_id, executor_fn=executor_fn,
            resource_vector=resource_vector, per_worker_mem_mb=per_worker_mem_mb,
            owner_prefix=owner_prefix,
        )
        passes.append(pass_result)
        pending_repair = any(r.state == wg.WorkState.REPAIRABLE_FAILED for r in wg.list_all())
        if not wg.runnable_items(self_session_id=self_session_id) and not pass_result.dispatched and not pending_repair:
            break
    return passes
