"""
Failure Diagnosis + Bounded Repair — WALK-AWAY CONVERGENCE macro campaign,
Phase 6 (2026-09-05).

Closes the gap between "an item failed" (already durable via work_graph.
mark_repairable_failed()/retry_repairable_failed()'s bounded blind retry)
and "diagnose WHY, and if a real, nameable prerequisite is missing, create
BOUNDED REPAIR WORK for exactly that, rather than re-running the identical
failed attempt over and over hoping it works." A transient failure (a
known infra-shaped class) still uses the existing bounded retry/backoff
path unchanged — this module only adds the second branch: a failure
diagnosed as needing repair gets a real dependency-blocking repair
WorkItem instead of a blind retry.

Deliberately a coarse, closed taxonomy mirroring job_ledger.py's own
FAILURE_TAXONOMY-equivalent reasoning (classify_ledger_failure_
transience()) — never a new, incompatible vocabulary, and unrecognized
evidence always fails closed to UNKNOWN rather than being guessed as
either transient or repairable.
"""
from __future__ import annotations

from typing import Any

import work_graph as wg

DIAGNOSIS_TRANSIENT = "transient"          # infra-shaped, the existing bounded retry path applies unchanged
DIAGNOSIS_NEEDS_REPAIR = "needs_repair"    # a real, nameable prerequisite is missing/broken
DIAGNOSIS_UNKNOWN = "unknown"              # fails closed — never guessed

_TRANSIENT_ERROR_CLASSES = frozenset({
    "RuntimeError", "concurrent_advancer_blocked", "no_engine_available_this_attempt", "prior_job_still_active",
})

# REAL_WORK_CLOSURE (2026-09-06): missing_prerequisite is no longer only
# reachable via an explicit caller-supplied result["missing_prerequisite"]
# key (which nothing in this codebase ever set, making DIAGNOSIS_NEEDS_
# REPAIR permanently unreachable). Grounded in REAL evidence this exact
# campaign's own natural cycle logs produced: a genuine ModuleNotFoundError
# (Phase U's continuous_stewardship import, before the mission-layer files
# were deployed) — the textbook shape of "a specific required artifact is
# absent, and remediation (deploy the missing file) is non-protected and
# bounded," exactly the NEEDS_REPAIR contract this module's own docstring
# describes. Deliberately narrow and closed (mirrors _TRANSIENT_ERROR_
# CLASSES' own closed-taxonomy philosophy): only Python's own standard-
# library exception classes that UNAMBIGUOUSLY name an absent prerequisite
# qualify — never a broad/generic class, and this never guesses AT the
# remediation itself, only names what's missing.
_MISSING_PREREQUISITE_ERROR_CLASSES = frozenset({
    "ModuleNotFoundError", "FileNotFoundError", "ImportError",
})


def _detect_missing_prerequisite(result: dict[str, Any], error_class: str | None) -> str | None:
    """Returns a real, evidence-derived missing-prerequisite descriptor,
    or None if nothing in this evidence qualifies. An explicit, caller-
    supplied result["missing_prerequisite"] is always honored first
    (the original mechanism, unchanged); otherwise falls back to the
    mechanical detector above."""
    if result.get("missing_prerequisite"):
        return result["missing_prerequisite"]
    if error_class in _MISSING_PREREQUISITE_ERROR_CLASSES:
        detail = result.get("error") or result.get("blocked_reason") or error_class
        return f"{error_class}: {detail}"
    return None


def diagnose(work_id: str) -> dict[str, Any]:
    """Pure, read-only: inspects a REPAIRABLE_FAILED item's own result
    payload and returns a diagnosis dict. Never mutates anything — see
    create_repair_work() for the one state transition a NEEDS_REPAIR
    diagnosis feeds."""
    record = wg.load(work_id)
    if record is None or not record.result:
        return {"diagnosis": DIAGNOSIS_UNKNOWN, "reason": "no failure evidence available"}
    result = record.result
    error_class = result.get("error_class") or result.get("blocked_reason")
    if error_class in _TRANSIENT_ERROR_CLASSES:
        return {"diagnosis": DIAGNOSIS_TRANSIENT, "reason": f"known transient class: {error_class}"}
    missing = _detect_missing_prerequisite(result, error_class)
    if missing:
        return {"diagnosis": DIAGNOSIS_NEEDS_REPAIR, "reason": missing}
    return {"diagnosis": DIAGNOSIS_UNKNOWN, "reason": f"unrecognized failure evidence: {result}"}


def create_repair_work(work_id: str, diagnosis: dict[str, Any]) -> wg.WorkItem:
    """For a DIAGNOSIS_NEEDS_REPAIR outcome: creates (idempotently, via
    work_graph.create_child_for_dependency — no second duplicate-
    suppression mechanism) a bounded repair WorkItem wired as a REAL
    dependency of the failed item, and moves the failed item from
    REPAIRABLE_FAILED to BLOCKED_DEPENDENCY so the scheduler's own bounded
    retry_repairable_failed() backoff can never re-attempt the same doomed
    action before the repair lands. The failed item automatically becomes
    RUNNABLE again — genuinely "retryable" — the moment the repair
    genuinely completes, via work_graph.resume_eligible_parents() (already
    called inside mark_completed()); no separate 'retry now' mechanism is
    invented here."""
    if diagnosis["diagnosis"] != DIAGNOSIS_NEEDS_REPAIR:
        raise ValueError(f"create_repair_work() called for a non-repair diagnosis: {diagnosis}")
    repair = wg.create_child_for_dependency(
        work_id, kind="repair", description=f"repair for {work_id}: {diagnosis['reason']}",
        provenance={"diagnosis": diagnosis},
    )
    record = wg.load(work_id)
    record.state = wg.WorkState.BLOCKED_DEPENDENCY
    wg._save(record, note=f"diagnosed as needing repair — blocked on {repair.work_id}")
    return repair
