"""
Scheduler Cycle Hook — the precise, tested integration point for wiring
scheduler.py into the real mrsilent-autonomous-cycle.timer -> run_cycle()
path, and the exact specification for the one remaining edit this slice
deliberately does NOT make.

WHY THE WIRING EDIT ITSELF IS NOT IN THIS COMMIT (2026-09-04):

autonomous_cycle.py's own module-level imports (campaign, capability_
registry, local_model_health, omni_registry_stewardship, organ_discovery)
are ALL untracked in git — confirmed via `git ls-files`. That means two
independent, each-sufficient reasons to stop exactly here rather than edit
that file in this pass:

  1. This isolated worktree cannot import autonomous_cycle.py at all (its
     own import chain fails immediately on `import campaign`), so any edit
     to it literally cannot be tested end-to-end from here — only guessed.
  2. A peer session (pulse5-core-9e, tmux "mrsilent-walkaway-final-gap")
     was observed active this same session, working what looks like this
     exact campaign, with files in this same area already modified-but-
     uncommitted in the live checkout. Copying those five untracked
     modules into this worktree to make testing possible would mean
     reading content of uncertain, possibly-concurrently-edited state —
     exactly the interference this campaign's own rules prohibit.

Everything in THIS module needs neither of those five untracked files. It
is real, tested code, ready to be called from inside run_cycle() the
moment reconciliation with the peer session (or those files becoming
tracked) resolves the blocker above.

EXACT INTEGRATION SPECIFICATION (for whoever performs that edit later):

  File:     mrsilent_bridge/autonomous_cycle.py
  Location: immediately after the existing Phase S call, i.e. right after
            `_maybe_pursue_omni_registry_stewardship(record)` (observed at
            line 947-949 during the read-only gap audit, 2026-09-04) —
            the same place every other bounded, best-effort, non-blocking
            per-cycle phase already lives, so this becomes one more phase
            in the existing sequence, not a second scheduler/daemon.
  Call:

      from scheduler_cycle_hook import run_scheduler_phase
      record.scheduler_phase = run_scheduler_phase(executor_fn=<real dispatcher>)

  The <real dispatcher> callable is the one piece this slice deliberately
  does NOT author: a function `(work_graph.WorkItem) -> dict` that
  actually performs the item's real work (most plausibly by calling
  evolution.advance.advance_one() for a work item whose provenance
  references a proposal_id, or omniengineer_harness directly for one that
  references a job_id). Authoring that blind, without being able to run it
  against the real advance.py/omniengineer_harness behavior from this
  worktree, would be guesswork on the single most consequential function
  in the whole integration — it is correctly left for a session that can
  safely test against the real modules (either once this branch merges
  into the live checkout, or once the peer session's status is resolved).
"""
from __future__ import annotations

from typing import Any, Callable

import dispatch_admission as da
import scheduler as sched
import work_graph as wg

# A single stable identity for the canonical timer-driven process itself,
# distinct from any ad-hoc human/Claude session's own session_ownership
# lease — work this hook dispatches is owned by "the pipeline", not by
# whichever interactive session happens to be running at the time.
CANONICAL_CYCLE_SESSION_ID = "mrsilent-autonomous-cycle"


class NoExecutorConfigured(NotImplementedError):
    """Raised by run_scheduler_phase() when no executor_fn is supplied and
    no default has been wired in. Deliberately fails loudly rather than
    defaulting to a no-op that would silently mark real objectives
    COMPLETED without doing anything — see module docstring."""


def run_scheduler_phase(
    *,
    executor_fn: Callable[[wg.WorkItem], dict] | None = None,
    self_session_id: str = CANONICAL_CYCLE_SESSION_ID,
    resource_vector: da.ResourceVector | None = None,
    per_worker_mem_mb: float = da.DEFAULT_PER_WORKER_MEM_MB,
) -> dict[str, Any]:
    """The one call a future autonomous_cycle.py phase would make. Thin by
    design: all real logic already lives in scheduler.run_scheduler_pass();
    this just supplies the canonical identity/defaults a live cycle should
    use, and returns a plain JSON-serializable dict (matching the shape
    every other autonomous_cycle.py phase result already takes) rather
    than the dataclass scheduler.py itself uses internally.

    Raises NoExecutorConfigured if executor_fn is omitted -- there is no
    safe default real dispatcher yet (see module docstring)."""
    if executor_fn is None:
        raise NoExecutorConfigured(
            "run_scheduler_phase() requires a real executor_fn — no default "
            "dispatcher exists yet (see this module's docstring for why)."
        )
    result = sched.run_scheduler_pass(
        self_session_id=self_session_id, executor_fn=executor_fn,
        resource_vector=resource_vector, per_worker_mem_mb=per_worker_mem_mb,
        owner_prefix=self_session_id,
    )
    return {
        "dispatched": result.dispatched,
        "completed": result.completed,
        "repairable_failed": result.repairable_failed,
        "reclaimed_stale": result.reclaimed_stale,
        "repair_retried": result.repair_retried,
        "duplicate_claim_rejections": result.duplicate_claim_rejections,
        "peak_concurrent_observed": result.peak_concurrent_observed,
        "dynamic_safe_concurrency": result.dynamic_safe_concurrency,
        "concurrency_limit_reason": result.concurrency_limit_reason,
        "admission_refused": result.admission_refused,
        "admission_reason": result.admission_reason,
        "work_graph_status": wg.status_counts(),
    }
