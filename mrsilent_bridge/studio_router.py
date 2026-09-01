"""
Capability-Aware Studio Router.

MR. SILENT should choose the best available Studio organ/agent for a task
based on more than task_type — this module scores every registry entry that
claims to support the requested task_type across: current availability,
health/status, cost class, quota/usage limitations, locality (local Studio
infra preferred over external paid APIs when it can safely do the job), risk
class, authority requirements, and whether a validation method is even
defined for it. It is deliberately narrow about what it will ever select:
of the 18 registry entries discovered/built on 2026-08-16, only five
(_CAPABILITY_TO_ADAPTER) are actually invocable by this project. Everything
else — ComfyUI, Pulse Core, OmniVisual, OmniSonus, the planned-but-never-built
OmniForge/BlackVault/OmniGuard/AED/OmniCompetitio roles, and OmniScraper
(zero references found anywhere) — is real, discovered information the
router can EXPLAIN with (e.g. "OmniForge is planned_not_implemented, not
available"), never a capability it will silently invent and route to.

Doctrine: UNLIMITED OUTSIDE CAPABILITY, EXTREMELY LIMITED OUTSIDE AUTHORITY.
Concretely: prefer free/local/low-risk capability that can safely do the job;
escalate to a stronger, more expensive, or higher-authority capability only
when nothing weaker qualifies; never let cost or convenience skip an
authority gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import audit
import capability_registry
from evolution import proposal as proposal_mod

# Lower is better.
_COST_RANK = {"free": 0, "gpu_compute": 1, "metered_api": 2}
_RISK_RANK = {
    "low": 0, "low_by_default_escalatable": 1, "medium": 2,
    "always_founder_gated": 3, "founder_gated": 3, "isolated_do_not_invoke": 99,
}
_ACTIVE_STATUS_PREFIXES = ("active",)  # "active", "active (self-reported)" both count

# The only registry entries this project can actually invoke. Everything else
# in the registry is discovery, not a routing target — see module docstring.
_CAPABILITY_TO_ADAPTER = {
    "claude_code_engineer_v1": "claude_code",
    "codex_engineer_v1": "codex",
    "local_model_engineer_v1": "local_model",
    "omni_engineer_v1": "omni_engineer",
    "human_escalation_v1": "human_escalation",
    # Phase S (2026-08-17): the pre-existing, already-universally-used
    # validation.py gate, now independently routable for a standalone
    # 'static_validation' objective rather than only reachable via Claude.
    "local_static_validation_v1": "local_static_validation",
}


@dataclass
class CandidateEvaluation:
    capability_id: str
    accepted: bool
    reason: str
    locality: str | None = None
    cost_class: str | None = None
    risk_class: str | None = None
    score: tuple[int, ...] | None = None


@dataclass
class SelectionResult:
    task_type: str
    selected_capability_id: str | None
    selected_adapter: str | None
    reasoning: str
    candidates_considered: list[dict[str, Any]] = field(default_factory=list)
    capability_gap: bool = False  # TRUE gap: zero registry entries even claim this task_type
    authority_gate_blocked: bool = False  # a real capability exists but none passed authority/quota checks


def _quota_blocked(entry: dict[str, Any]) -> str | None:
    q = entry.get("quota_status") or {}
    if not q.get("limited"):
        return None
    until = q.get("until")
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= until_dt:
                return None  # quota window has passed
        except ValueError:
            pass
    return q.get("reason") or f"quota-limited until {until}"


def _score(entry: dict[str, Any]) -> tuple[int, int, int]:
    locality_rank = 0 if entry.get("locality") == "local" else 1
    cost_rank = _COST_RANK.get(str(entry.get("cost_class", "")).split(" ")[0], 9)
    risk_rank = _RISK_RANK.get(entry.get("risk_class", ""), 9)
    return (locality_rank, cost_rank, risk_rank)


def _evaluate(entry: dict[str, Any], *, allow_founder_gated: bool, allow_paid: bool = True) -> CandidateEvaluation:
    cap_id = entry["capability_id"]

    if cap_id not in _CAPABILITY_TO_ADAPTER:
        return CandidateEvaluation(cap_id, False, "not an invocable capability (discovery-only entry)")

    status = entry.get("status", "")
    if not status.startswith(_ACTIVE_STATUS_PREFIXES):
        return CandidateEvaluation(cap_id, False, f"status={status!r} is not active")

    if entry.get("risk_class") == "isolated_do_not_invoke":
        return CandidateEvaluation(cap_id, False, "isolated_do_not_invoke — hard-excluded regardless of task")

    # HARD eligibility filter, evaluated before scoring — not a soft
    # preference. Reuses the exact same cost_class-first-word parsing
    # _score() already uses for _COST_RANK, so this can never disagree with
    # the existing cost metadata convention. "metered_api" is the only value
    # this project's real invocable capabilities ever use for a genuinely
    # billed/paid resource (see registry_data/capabilities.json) — gpu_compute
    # is local/unmetered contention, not a billed resource, so it is not
    # excluded by this flag.
    if not allow_paid and str(entry.get("cost_class", "")).split(" ")[0] == "metered_api":
        return CandidateEvaluation(cap_id, False,
                                    "paid resource excluded by policy (paid_resources_allowed=false)")

    avail = entry.get("availability", "")
    if avail == "available":
        pass
    elif avail == "available_founder_gated":
        if not allow_founder_gated:
            return CandidateEvaluation(cap_id, False,
                                        "requires founder approval (allow_founder_gated=False) — authority gate not crossed")
    else:
        return CandidateEvaluation(cap_id, False, f"availability={avail!r}, not invocable")

    quota_reason = _quota_blocked(entry)
    if quota_reason:
        return CandidateEvaluation(cap_id, False, f"quota-limited: {quota_reason}")

    if entry.get("validation_method", "n/a") in ("n/a", "unknown", ""):
        # Not a hard exclusion — a capability without a defined validation
        # method is still usable, just noted so the reasoning is honest
        # about a weaker guarantee. It shows up in the score tie-break only.
        pass

    return CandidateEvaluation(
        cap_id, True, "eligible",
        locality=entry.get("locality"), cost_class=entry.get("cost_class"),
        risk_class=entry.get("risk_class"), score=_score(entry),
    )


def rank(task_type: str, *, allow_founder_gated: bool = False, allow_paid: bool = True) -> list[CandidateEvaluation]:
    """Every registry entry claiming task_type, each evaluated by the exact
    same _evaluate() select() uses — but returning the FULL list (accepted
    candidates sorted best-first, then rejected candidates) instead of just
    the single winner. select() is implemented in terms of this. Callers
    that need a fallback SEQUENCE, not just one pick (e.g. evolution/advance.py's
    implementation router: try the best engine, fall back to the next
    accepted one on an infra-looking failure, never on a validation failure
    or a rejected_policy authority decision) walk the accepted prefix of this
    list. This function creates no proposals and has no side effects — pure
    query, safe to call as often as needed without dedup/cooldown concerns.

    allow_paid=False is a HARD eligibility filter (evaluated in _evaluate(),
    same tier as allow_founder_gated) — a metered/paid candidate is never
    "accepted" at all when False, so no downstream caller (e.g. advance.py's
    engineering-engine reordering, which only ever reorders the ALREADY-
    accepted list) can silently select one."""
    all_entries = capability_registry.load()
    matching = [e for e in all_entries if task_type in e.get("task_types", [])]
    evaluations = [_evaluate(e, allow_founder_gated=allow_founder_gated, allow_paid=allow_paid) for e in matching]
    accepted = sorted((ev for ev in evaluations if ev.accepted), key=lambda ev: ev.score)
    rejected = [ev for ev in evaluations if not ev.accepted]
    return accepted + rejected


def select(task_type: str, *, allow_founder_gated: bool = False, allow_paid: bool = True, requested_by: str = "studio_router") -> SelectionResult:
    evaluations = rank(task_type, allow_founder_gated=allow_founder_gated, allow_paid=allow_paid)
    accepted = [ev for ev in evaluations if ev.accepted]

    candidates_considered = [
        {"capability_id": ev.capability_id, "accepted": ev.accepted, "reason": ev.reason,
         "locality": ev.locality, "cost_class": ev.cost_class, "risk_class": ev.risk_class}
        for ev in evaluations
    ]

    if not accepted:
        true_gap = len(evaluations) == 0  # zero registry entries even claim this task_type
        if true_gap:
            reasoning = (f"no capability in the registry claims task_type={task_type!r} at all — "
                         f"escalating to a human and recording a capability-gap proposal")
        else:
            reasoning = (f"{len(evaluations)} candidate(s) claim task_type={task_type!r} but none passed "
                         f"authority/availability/quota checks — escalating to a human for an explicit "
                         f"decision (this is a real, existing capability that's currently gated, not a "
                         f"missing one, so no capability-gap proposal is recorded)")
        result = SelectionResult(
            task_type=task_type, selected_capability_id=None, selected_adapter="human_escalation",
            reasoning=reasoning, candidates_considered=candidates_considered,
            capability_gap=true_gap, authority_gate_blocked=not true_gap,
        )
        if true_gap:
            _record_capability_gap(task_type, candidates_considered)
        _audit(result, requested_by)
        return result

    best = accepted[0]  # already sorted best-first by rank()

    result = SelectionResult(
        task_type=task_type,
        selected_capability_id=best.capability_id,
        selected_adapter=_CAPABILITY_TO_ADAPTER[best.capability_id],
        reasoning=(f"selected {best.capability_id!r} (locality={best.locality}, cost={best.cost_class}, "
                   f"risk={best.risk_class}): best-ranked of {len(accepted)} eligible / "
                   f"{len(evaluations)} considered candidate(s) for task_type={task_type!r} "
                   f"— local-and-free preferred, escalating to paid/external only if nothing local qualifies"),
        candidates_considered=candidates_considered,
    )
    _audit(result, requested_by)
    return result


def _record_capability_gap(task_type: str, candidates_considered: list[dict[str, Any]]) -> None:
    """Every unroutable task_type becomes a deduped capability-gap proposal —
    repeated identical gaps link to the same open proposal instead of spamming."""
    fingerprint = f"studio_router_capability_gap:{task_type}"
    existing = proposal_mod.find_open_by_fingerprint(fingerprint)
    if existing:
        return
    rejected_summary = "; ".join(f"{c['capability_id']}: {c['reason']}" for c in candidates_considered) or "no registry entries claim this task_type at all"
    proposal_mod.create(
        observed_weakness=f"studio_router found no eligible capability for task_type={task_type!r}. {rejected_summary}",
        proposed_upgrade=f"either build/wire a real adapter that supports task_type={task_type!r}, "
                          f"or confirm no such capability should exist in this Studio",
        risk_score="low",
        origin="studio_router",
        fingerprint=fingerprint,
        source_observation_ids=[],
    )


def _audit(result: SelectionResult, requested_by: str) -> None:
    audit.record(
        job_id=f"routing-{result.task_type}",
        requested_by=requested_by,
        task_summary=f"studio_router.select(task_type={result.task_type!r})",
        tool_agent_selected="studio_router",
        permissions_granted=[result.selected_capability_id] if result.selected_capability_id else [],
        files_touched={},
        commands_executed=[],
        test_results={"candidates_considered": result.candidates_considered},
        risk_class="n/a",
        approval_state="not_required",
        final_disposition=result.selected_capability_id or ("capability_gap" if result.capability_gap else "escalated"),
        lesson=result.reasoning,
    )
