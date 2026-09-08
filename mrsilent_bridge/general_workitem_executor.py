"""General WorkItem Executor — Studio-wide Walk-Away, non-proposal
executor gap closure (2026-09-08).

Traced end-to-end before writing anything: scheduler.py itself is ALREADY
generic ("Execution is pluggable: callers pass an executor_fn(WorkItem) ->
dict" — its own docstring). scheduler_cycle_hook.run_scheduler_phase()
is likewise a thin, generic pass-through requiring only that some
executor_fn be supplied. NEITHER needs to change. The actual gap is one
level up: autonomous_cycle.py's live Phase T wires in exactly ONE executor
— proposal_work_bridge.make_advance_executor() — which raises
NotAProposalWorkItem for any WorkItem without a proposal_id in its
provenance. work_graph.WorkItem.kind already supports "objective" | "task"
| "dependency" | "repair", but only proposal-linked items of any kind can
currently execute; "repair" (created by failure_diagnosis.
create_repair_work()) has NO executor at all today — a real, already-
existing kind with zero dispatch path.

This module closes that gap the way scheduler.py's own design already
anticipates: a composite executor_fn that dispatches by real WorkItem
evidence (provenance.proposal_id present, or kind == "repair") to the
CORRECT existing/new handler, and fails closed — raises, never silently
drops or fabricates completion — for anything it doesn't recognize.
Creates no second scheduler, no second WorkGraph, no second governance
layer; delegates to proposal_work_bridge.make_advance_executor() for the
proposal case exactly as today, unchanged.

WIRING STATUS UPDATE (2026-09-08, Domain-B truthfulness audit): the
call-site change this section originally deferred (below, preserved for
its historical reasoning) has LANDED — autonomous_cycle.py's Phase T now
calls scheduler_cycle_hook.run_scheduler_phase(executor_fn=
general_workitem_executor.make_general_executor()) directly, confirmed
live and operating across many real, untriggered natural cycles this
session. The collision concern named below did not materialize.

Originally NOT WIRED into the live natural cycle by this module (2026-09-08,
historical): the one-line call-site change (swapping proposal_work_bridge.
make_advance_executor() for this module's make_general_executor() inside
autonomous_cycle.py's Phase T) required editing autonomous_cycle.py, which
was: (a) swapfix-owned — confirmed diverged from this live tree's copy
that session, and (b) scheduler_cycle_hook.py's own docstring additionally
documented a SEPARATE peer session ("pulse5-core-9e", tmux "mrsilent-
walkaway-final-gap") already working this exact integration point. Wiring
it directly then would have risked colliding with two independent owners
of the same call site, not one.

Repair-kind execution is deliberately conservative: failure_diagnosis's
own docstring confirms "repair" WorkItems are currently mechanically
inert under real evidence (nothing in this codebase yet sets a job
result's missing_prerequisite key, so DIAGNOSIS_NEEDS_REPAIR — the only
trigger for create_repair_work() — has never actually fired). No real
repair WorkItem exists to execute against today. _execute_repair_workitem
here re-diagnoses the ORIGINAL blocked item and only marks the repair
complete if that re-diagnosis evidence shows the missing prerequisite is
now genuinely satisfied — never a fabricated success — and is proven only
by focused tests, per this campaign's own instruction not to claim a real
unattended repair-to-success before a real event demonstrates one.
"""
from __future__ import annotations

from typing import Any, Callable

import failure_diagnosis
import proposal_work_bridge
import work_graph as wg


class UnknownWorkItemKind(ValueError):
    """Raised for a WorkItem this executor cannot safely identify a real
    handler for — fail closed, never a silent no-op, never fabricated
    completion. A raised exception here routes through work_graph's own
    existing mark_repairable_failed() path, same as any other executor
    error (see scheduler.py), so it is retried/escalated through the
    ALREADY-existing bounded-retry machinery, not a new one."""


def _execute_repair_workitem(item: wg.WorkItem) -> dict[str, Any]:
    """Real, bounded, idempotent handling for kind == "repair": re-checks
    whether the condition the original diagnosis identified is still
    present, using failure_diagnosis.diagnose() again — the SAME pure,
    read-only function that produced the original diagnosis, never a new
    verification mechanism. Only reports success if the parent item's own
    current evidence no longer reproduces DIAGNOSIS_NEEDS_REPAIR; never
    marks completion just because this function ran. work_graph.
    mark_completed() (called by the scheduler on a successful executor
    result) triggers resume_eligible_parents() itself — this function does
    not resume the parent directly, preserving the existing, proven
    dependency-satisfaction path rather than adding a second one."""
    parent_id = item.parent_id
    if not parent_id:
        raise UnknownWorkItemKind(f"repair work item {item.work_id!r} has no parent_id — cannot verify what it repairs")
    diagnosis = failure_diagnosis.diagnose(parent_id)
    if diagnosis.get("diagnosis") == failure_diagnosis.DIAGNOSIS_NEEDS_REPAIR:
        raise RuntimeError(
            f"repair for {parent_id} not yet resolved: {diagnosis.get('reason')}"
        )
    return {
        "repair_verified": True,
        "parent_id": parent_id,
        "reverified_diagnosis": diagnosis,
    }


def make_general_executor(advance_mod: Any = None) -> Callable[[wg.WorkItem], dict[str, Any]]:
    """Returns an executor_fn matching scheduler.run_scheduler_pass()'s
    exact contract (WorkItem -> dict). Dispatch order, strongest evidence
    first:
      1. provenance.proposal_id present -> the EXISTING, unchanged
         proposal_work_bridge.make_advance_executor() (kind is irrelevant
         here; a proposal-backed item is a proposal-backed item regardless
         of what kind string it carries).
      2. kind == "repair" -> _execute_repair_workitem() above.
      3. anything else -> UnknownWorkItemKind, fail closed.
    Never performs arbitrary shell execution and never fabricates a
    completion result — every branch either delegates to an existing,
    already-proven executor or raises."""
    proposal_executor = proposal_work_bridge.make_advance_executor(advance_mod)

    def _executor(item: wg.WorkItem) -> dict[str, Any]:
        if item.provenance.get("proposal_id"):
            return proposal_executor(item)
        if item.kind == "repair":
            return _execute_repair_workitem(item)
        raise UnknownWorkItemKind(
            f"work item {item.work_id!r} (kind={item.kind!r}) has no proposal_id and is not a "
            "recognized non-proposal kind — refusing to guess how to execute it"
        )
    return _executor
