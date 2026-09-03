"""
OBSERVE — the first stage of the self-evolution lifecycle:

    OBSERVE -> IDENTIFY WEAKNESS -> PROPOSE UPGRADE -> RISK SCORE ->
    ISOLATED IMPLEMENTATION -> TEST -> CANARY/VALIDATE -> PROMOTE OR ROLLBACK
    -> RECORD LESSON

This module only does the first two arrows. It reads existing local records
(audit_log/audit.jsonl, jobs/*/result.json, evolution/proposals/*.json,
registry_data/capabilities.json) — nothing here calls out to any external
service, runs a job, or edits anything outside evolution/observations/ and
evolution/proposals/. It never touches mrsilent_bridge's own code, and it
never advances an existing proposal past PROPOSED on its own — see
proposal.advance()'s docstring, which this module does not call.

Every Observation that looks like a real weakness gets one linked Proposal
(deduped by fingerprint against still-open proposals, so re-running this
doesn't spam duplicates for a recurring issue). That's it: "create structured
observations/proposals, not autonomously rewrite protected core."
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import audit
import capability_registry
from evolution import advance as advance_mod  # reuse the SAME eligibility semantics
# advance_eligible() already uses (advance_mod._eligible()) -- never a second
# risk/eligibility policy, see classify_governing_priority_proposals() below.
from evolution import proposal as proposal_mod

BRIDGE_ROOT = Path(__file__).resolve().parent.parent
JOBS_ROOT = BRIDGE_ROOT / "jobs"
OBSERVATIONS_DIR = Path(__file__).resolve().parent / "observations"

ERROR_LOG_PATTERNS = (
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"authentication (failed|error)", re.IGNORECASE),
)


@dataclass
class Observation:
    observation_id: str
    created_at: str
    signal_type: str
    description: str
    evidence: dict[str, Any]
    severity: str  # low | medium | high
    suggested_risk_score: str  # low | medium | founder_gated
    linked_proposal_id: str | None = None
    dedupe_key: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Stable identity for 'is this the same underlying condition as last
        run', deliberately NOT built from `description` — description embeds
        volatile counts ("...over 3 job(s)" vs "...over 4 job(s)"), which would
        change the fingerprint every run and defeat dedup entirely. dedupe_key
        must contain only stable identity fields (adapter name, check name,
        pattern, capability_id, proposal_id — never a count)."""
        key = self.dedupe_key or [self.signal_type]
        raw = ":".join([self.signal_type, *key])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ObserveReport:
    generated_at: str
    observations: list[dict[str, Any]] = field(default_factory=list)
    proposals_created: list[str] = field(default_factory=list)
    proposals_reused: list[str] = field(default_factory=list)
    proposals_known_deferred: list[str] = field(default_factory=list)
    proposals_on_cooldown: list[str] = field(default_factory=list)
    root_causes_correlated: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# How long after a proposal is closed (rejected/promoted/rolled_back) to wait
# before letting the same fingerprint create a fresh proposal — avoids
# immediate re-litigation right after a human just made a call on it.
COOLDOWN_HOURS = 24


def _last_event_at(p: "proposal_mod.Proposal") -> datetime:
    if p.history:
        try:
            return datetime.fromisoformat(p.history[-1]["at"])
        except (KeyError, ValueError):
            pass
    return datetime.fromisoformat(p.created_at)


def find_on_cooldown(fingerprint: str) -> "proposal_mod.Proposal | None":
    now = datetime.now(timezone.utc)
    for p in proposal_mod.list_all():
        if p.fingerprint != fingerprint or p.status not in proposal_mod.CLOSED_STATUSES:
            continue
        last = _last_event_at(p)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < COOLDOWN_HOURS * 3600:
            return p
    return None


def _job_results() -> list[dict[str, Any]]:
    results = []
    if not JOBS_ROOT.exists():
        return results
    for d in sorted(JOBS_ROOT.iterdir()):
        rp = d / "result.json"
        if rp.exists():
            try:
                results.append(json.loads(rp.read_text()))
            except json.JSONDecodeError:
                continue
    return results


# ---- individual signal detectors -------------------------------------------

def signal_failed_jobs(entries: list[dict], _jobs: list[dict]) -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    failed = [e for e in entries if e["final_disposition"] in ("failed", "timeout", "error")]
    by_adapter: dict[str, list[dict]] = defaultdict(list)
    for e in failed:
        by_adapter[e["tool_agent_selected"]].append(e)
    for adapter_name, fails in by_adapter.items():
        obs.append(Observation(
            observation_id=f"obs-{len(obs)}", created_at=now, signal_type="failed_job",
            description=f"{len(fails)} job(s) failed/timed-out/errored on adapter '{adapter_name}'",
            evidence={"adapter": adapter_name, "job_ids": [f["job_id"] for f in fails][:10],
                      "dispositions": dict(Counter(f["final_disposition"] for f in fails))},
            severity="medium" if len(fails) >= 2 else "low",
            suggested_risk_score="low",
            dedupe_key=[adapter_name],
        ))
    return obs


def signal_validator_failures(entries: list[dict], _jobs: list[dict]) -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    check_failures: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        tr = e.get("test_results")
        if not tr:
            continue
        checks = tr.get("checks", [])
        if not isinstance(checks, list):
            continue  # defensive: a malformed/legacy audit entry (e.g. a plain int count instead of
                       # a check-dict list) must never crash OBSERVE — same "skip, don't crash" discipline
                       # job_ledger/proposal/campaign list_all() already use for a bad on-disk record
        for c in checks:
            if not isinstance(c, dict):
                continue
            if not c.get("passed", True):
                check_failures[c["name"]] += 1
                if len(examples[c["name"]]) < 3:
                    examples[c["name"]].append(e["job_id"])
    for check_name, count in check_failures.items():
        obs.append(Observation(
            observation_id=f"obs-{len(obs)}", created_at=now, signal_type="validator_failure",
            description=f"validator check '{check_name}' has failed {count} time(s) across jobs",
            evidence={"check_name": check_name, "count": count, "example_job_ids": examples[check_name]},
            severity="medium" if count >= 2 else "low",
            suggested_risk_score="low",
            dedupe_key=[check_name],
        ))
    return obs


# Requester identities known to be internal pipeline components or this
# project's own test/dev fixtures, never a human or external system stuck
# resubmitting a genuinely failing task. Centralized here (rather than
# scattered one-off checks added each time a new identity trips this signal)
# specifically because this project's test suites legitimately re-run the
# SAME fixed task text under the SAME requested_by identity many times
# across a long development session — that is reproducibility, not a retry
# loop, but it is indistinguishable from one using audit-log text alone.
# Proposal-level dedup (fingerprint-based, evolution/proposal.py) and job-
# level idempotency (task_fingerprint, job_ledger.py) are what actually
# prevent duplicate *work* — this heuristic is a coarse, human-facing
# "something looks stuck" signal, and internal/test identities are exactly
# where it is structurally unreliable. Real, external, human-driven retry
# loops still surface normally under any OTHER requested_by (e.g. "cli_user").
_NON_PRODUCTION_REQUESTERS = frozenset({
    "studio_router", "autonomous_pipeline", "autonomous_cycle", "recovery",
    "test", "test_recovery", "test_omniengineer_integration", "test_operator",
    "worker_a", "worker_b", "dead_worker", "new_worker",
    "live_e2e_test", "live_e2e_test_2",
})


def signal_repeated_retries(entries: list[dict]) -> list[Observation]:
    """Same requester submitting near-identical task text repeatedly is a proxy
    for 'this kept failing and someone kept resubmitting it'."""
    now = datetime.now(timezone.utc).isoformat()
    groups: Counter[tuple[str, str]] = Counter()
    for e in entries:
        if e["tool_agent_selected"] == "studio_router":
            # routing lookups aren't "a task being resubmitted after failing"
            # — routine, repeatable read-only selections, regardless of which
            # requested_by a particular caller passed through to select().
            continue
        if e["requested_by"] in _NON_PRODUCTION_REQUESTERS:
            continue
        key = (e["requested_by"], e["task_summary"][:60])
        groups[key] += 1
    obs = []
    for (requester, summary), count in groups.items():
        if count >= 3:
            obs.append(Observation(
                observation_id=f"obs-{len(obs)}", created_at=now, signal_type="repeated_retry",
                description=f"task starting '{summary}...' was submitted {count} times by '{requester}' — possible repeated retry of a failing task",
                evidence={"requested_by": requester, "task_prefix": summary, "count": count},
                severity="medium",
                suggested_risk_score="low",
                dedupe_key=[requester, summary],
            ))
    return obs


ENGINEERING_AGENT_ADAPTERS = frozenset({"claude_code", "codex", "local_model", "omni_engineer"})
# An agent execution "succeeded" for health purposes if the agent itself ran
# without error — a validation failure is a content problem (already captured
# by signal_validator_failures), not a sign the provider/adapter is unhealthy.
# succeeded_canary_failed is omni_engineer-specific (its stricter, canary-gated
# promotion bar — see omniengineer_harness.py) and is the same kind of content/
# determinism issue, not an infra/provider-health one.
AGENT_SUCCESS_DISPOSITIONS = frozenset({"succeeded", "succeeded_validation_failed", "succeeded_canary_failed"})


def signal_service_health(entries: list[dict]) -> list[Observation]:
    """Only meaningful for engineering-agent adapters (claude_code/codex/
    local_model), which share the succeeded/failed/timeout/error vocabulary.
    promotion/promotion_rollback/human_escalation use a different vocabulary
    (granted/blocked/dry_run, rolled_back, pending_founder_review) where
    e.g. 'granted' and 'rolled_back' ARE the success outcomes — scoring them
    against "succeeded" would produce a false 0%-health reading."""
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    by_adapter: dict[str, list[tuple[str, str]]] = defaultdict(list)  # adapter -> [(disposition, job_id)]
    for e in entries:
        if e["tool_agent_selected"] not in ENGINEERING_AGENT_ADAPTERS:
            continue
        by_adapter[e["tool_agent_selected"]].append((e["final_disposition"], e["job_id"]))
    for adapter_name, pairs in by_adapter.items():
        attempted = [(d, jid) for d, jid in pairs if d != "rejected_policy"]
        if len(attempted) < 1:
            continue
        succeeded = sum(1 for d, _ in attempted if d in AGENT_SUCCESS_DISPOSITIONS)
        rate = succeeded / len(attempted)
        if len(attempted) >= 1 and rate < 0.5:
            failing_job_ids = [jid for d, jid in attempted if d not in AGENT_SUCCESS_DISPOSITIONS][:10]
            obs.append(Observation(
                observation_id=f"obs-{len(obs)}", created_at=now, signal_type="service_health",
                description=f"adapter '{adapter_name}' success rate is {rate:.0%} over {len(attempted)} executed job(s)",
                evidence={"adapter": adapter_name, "executed": len(attempted), "succeeded": succeeded,
                          "dispositions": dict(Counter(d for d, _ in attempted)),
                          "job_ids": failing_job_ids},
                severity="high" if rate == 0 else "medium",
                suggested_risk_score="low",
                dedupe_key=[adapter_name],
            ))
    return obs


def signal_error_logs(jobs: list[dict]) -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    pattern_hits: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for j in jobs:
        for path_key in ("stdout_path", "stderr_path"):
            p = j.get(path_key)
            if not p or not Path(p).exists():
                continue
            text = Path(p).read_text(errors="replace")
            for pattern in ERROR_LOG_PATTERNS:
                if pattern.search(text):
                    pattern_hits[pattern.pattern] += 1
                    if len(examples[pattern.pattern]) < 3:
                        examples[pattern.pattern].append(j["job_id"])
    for pattern_str, count in pattern_hits.items():
        obs.append(Observation(
            observation_id=f"obs-{len(obs)}", created_at=now, signal_type="error_log",
            description=f"log pattern '{pattern_str}' observed {count} time(s) in job output",
            evidence={"pattern": pattern_str, "count": count, "example_job_ids": examples[pattern_str]},
            severity="medium",
            suggested_risk_score="low",
            dedupe_key=[pattern_str],
        ))
    return obs


def signal_performance_regression(jobs: list[dict]) -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    by_adapter: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for j in sorted(jobs, key=lambda x: x.get("started_at", "")):
        dur = j.get("duration_s")
        adapter_name = j.get("adapter") or "claude_code"
        if isinstance(dur, (int, float)) and j.get("status") == "succeeded":
            by_adapter[adapter_name].append((dur, j.get("job_id")))
    for adapter_name, pairs in by_adapter.items():
        if len(pairs) < 4:
            continue  # not enough history to call anything a regression
        *history, (latest, latest_job_id) = pairs
        history_durations = [d for d, _ in history]
        median = statistics.median(history_durations)
        if median > 0 and latest > median * 2:
            obs.append(Observation(
                observation_id=f"obs-{len(obs)}", created_at=now, signal_type="performance_regression",
                description=f"adapter '{adapter_name}' latest job took {latest:.1f}s, over 2x the historical median ({median:.1f}s)",
                evidence={"adapter": adapter_name, "latest_s": latest, "median_s": median,
                          "n_history": len(history_durations), "job_ids": [latest_job_id] if latest_job_id else []},
                severity="low",
                suggested_risk_score="low",
                dedupe_key=[adapter_name],
            ))
    return obs


def signal_unresolved_proposals() -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    open_states = {proposal_mod.ProposalStatus.OBSERVED, proposal_mod.ProposalStatus.PROPOSED}
    obs = []
    for p in proposal_mod.list_all():
        if p.status in open_states:
            obs.append(Observation(
                observation_id=f"obs-{len(obs)}", created_at=now, signal_type="unresolved_proposal",
                description=f"proposal {p.proposal_id[:8]} ('{p.observed_weakness[:60]}') has sat at status '{p.status}' with no advancement",
                evidence={"proposal_id": p.proposal_id, "status": p.status, "created_at": p.created_at},
                severity="low",
                suggested_risk_score="low",
                dedupe_key=[p.proposal_id],
            ))
    return obs


def signal_capability_gaps() -> list[Observation]:
    now = datetime.now(timezone.utc).isoformat()
    obs = []
    for entry in capability_registry.load():
        avail = entry.get("availability", "")
        # "external_not_integrated" covers legitimate discovery-only Studio
        # organs (ComfyUI, Pulse Core, OmniVisual, OmniSonus, ...) that this
        # project deliberately never intended to build adapters for — those
        # aren't gaps, they're accurate discovery notes. Only a real
        # "not_implemented" (something this project is expected to build)
        # is a genuine capability gap worth a proposal.
        if avail == "not_implemented":
            obs.append(Observation(
                observation_id=f"obs-{len(obs)}", created_at=now, signal_type="capability_gap",
                description=f"capability '{entry['capability_id']}' is {avail}",
                evidence={"capability_id": entry["capability_id"], "availability": avail,
                          "note": entry.get("note", "")},
                severity="low",
                suggested_risk_score="low",
                dedupe_key=[entry["capability_id"]],
            ))
    return obs


@dataclass
class GoverningPriorityProposalState:
    """Truthful classification of every canonical proposal (open OR closed)
    that already references a Founder Top-10 governing rank -- built from the
    SAME eligibility semantics evolution/advance.py::_eligible() already uses
    for advance_eligible(), never a second risk/eligibility policy. Replaces
    a prior coarse "any proposal exists (any status)" check that let a single
    rejected/terminal proposal silently suppress the bridge signal forever,
    even when nothing was actually actionable or pending Founder review --
    a real deadlock (mechanically proven 2026-09-03, commit e240d80 gap)."""
    matches: list[Any]
    actionable: list[Any]          # advance_mod._eligible() is True for these -- the existing timer already picks them up unattended, no gate involved
    founder_gated_open: list[Any]  # status not in CLOSED_STATUSES, not actionable -- a real, unresolved Founder gate
    terminal_only: bool            # matches exist but EVERY one is CLOSED_STATUSES (rejected/promoted/rolled_back) -- nothing left to wait on


def classify_governing_priority_proposals(governing_priority_id: str | None) -> GoverningPriorityProposalState:
    """Reusable by both the OBSERVE bridge signal below and
    autonomous_cycle.py's founder_top10 truthful-state fields -- single
    source of truth, computed once per cycle, not re-derived twice."""
    if not governing_priority_id:
        return GoverningPriorityProposalState([], [], [], False)
    matches = [
        p for p in proposal_mod.list_all()
        if governing_priority_id in (p.observed_weakness or "") or governing_priority_id in (p.proposed_upgrade or "")
    ]
    actionable = [p for p in matches if advance_mod._eligible(p)[0]]
    actionable_ids = {p.proposal_id for p in actionable}
    open_matches = [p for p in matches if p.status not in proposal_mod.CLOSED_STATUSES]
    founder_gated_open = [p for p in open_matches if p.proposal_id not in actionable_ids]
    terminal_only = bool(matches) and not open_matches
    return GoverningPriorityProposalState(matches, actionable, founder_gated_open, terminal_only)


def signal_governing_priority_needs_proposal(governing_priority_id: str | None) -> list[Observation]:
    """Bridges the Founder Top-10 governing rank (already read every cycle by
    autonomous_cycle._consume_founder_top10(), via the existing read-only
    founder_priority_governor module — this function never re-invokes that
    governor, it only receives the id it already computed) to the canonical
    proposal pipeline.

    Fires ONLY when the governing rank has GENUINELY NOTHING to wait on: no
    matching proposal at all, or every matching proposal that ever existed is
    now terminal (rejected/promoted/rolled_back) with none open. An
    actionable low-risk proposal, or an open founder_gated one still awaiting
    Founder review, both correctly suppress this (see
    classify_governing_priority_proposals()) -- a rejected/terminal proposal
    alone never does, so this can re-fire and reconcile after a rejection
    instead of deadlocking silently forever.

    Deliberately NEVER suggests risk_score="low" itself: an un-scoped
    strategic Founder priority is not yet an ordinary authorized engineering
    task, so the proposal this creates always stays founder_gated (surfaced
    via pending_founder_gated_jobs for explicit Founder review/scoping) —
    only a properly-scoped delta someone actually reviews and creates as
    "low" is eligible to auto-advance without repeated Founder involvement,
    exactly like every other low-risk proposal already does."""
    if not governing_priority_id:
        return []
    state = classify_governing_priority_proposals(governing_priority_id)
    if state.matches and not state.terminal_only:
        return []  # actionable and/or an open founder_gated proposal already exists -- do not duplicate
    now = datetime.now(timezone.utc).isoformat()
    return [Observation(
        observation_id="obs-0", created_at=now, signal_type="founder_priority_unproposed",
        description=f"Founder Top-10 governing priority '{governing_priority_id}' has no actionable or pending proposal"
                     + (f" (its only {len(state.matches)} prior proposal(s) are all terminal/closed)" if state.matches else ""),
        evidence={"governing_priority_id": governing_priority_id,
                  "terminal_proposal_ids": [p.proposal_id for p in state.matches]},
        severity="medium",
        suggested_risk_score="founder_gated",
        dedupe_key=[f"founder_priority_unproposed:{governing_priority_id}"],
    )]


def signal_operational_friction(entries: list[dict]) -> list[Observation]:
    """High ratio of rejected_policy outcomes suggests either genuinely risky
    requests keep coming in (policy working as intended) or the policy is
    creating friction for routine work — this only surfaces the ratio, it
    does not decide which."""
    now = datetime.now(timezone.utc).isoformat()
    if not entries:
        return []
    rejected = sum(1 for e in entries if e["final_disposition"] == "rejected_policy")
    total = len(entries)
    rate = rejected / total
    if total >= 5 and rate >= 0.3:
        return [Observation(
            observation_id="obs-0", created_at=now, signal_type="operational_friction",
            description=f"{rejected}/{total} ({rate:.0%}) of all recorded operations were policy-rejected",
            evidence={"rejected": rejected, "total": total, "rate": rate},
            severity="medium",
            suggested_risk_score="low",
            dedupe_key=["global"],
        )]
    return []


# ---- root-cause correlation --------------------------------------------------

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
_RISK_ORDER = {"low": 0, "medium": 1, "founder_gated": 2}


def _job_ids_of(evidence: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("job_ids", "example_job_ids"):
        v = evidence.get(key)
        if isinstance(v, list):
            ids.update(x for x in v if x)
    return ids


@dataclass
class RootCauseCluster:
    """One or more Observations that are, in this run, evidence of the SAME
    underlying incident — identified by sharing at least one job_id (a
    precise, high-confidence signal: they both fired off the exact same job
    execution). A cluster of size 1 behaves identically to a lone Observation
    — nothing changes for signals that don't correlate with anything."""
    observations: list[Observation]

    @property
    def signal_types(self) -> list[str]:
        return sorted({o.signal_type for o in self.observations})

    @property
    def job_ids(self) -> list[str]:
        ids: set[str] = set()
        for o in self.observations:
            ids |= _job_ids_of(o.evidence)
        return sorted(ids)

    @property
    def severity(self) -> str:
        return max(self.observations, key=lambda o: _SEVERITY_ORDER.get(o.severity, 0)).severity

    @property
    def suggested_risk_score(self) -> str:
        return max(self.observations, key=lambda o: _RISK_ORDER.get(o.suggested_risk_score, 0)).suggested_risk_score

    def fingerprint(self) -> str:
        """Built from each member's stable dedupe_key, NOT from job_ids —
        job_ids are unique per incident and would defeat cross-run dedup for
        a recurring condition the same way volatile counts did before. Two
        clusters with the same set of (signal_type, dedupe_key) pairs are the
        same recurring condition even if the specific job that triggered them
        this time is new."""
        parts = sorted(f"{o.signal_type}:{':'.join(o.dedupe_key or [o.signal_type])}" for o in self.observations)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def description(self) -> str:
        if len(self.observations) == 1:
            return self.observations[0].description
        primary = max(self.observations, key=lambda o: _SEVERITY_ORDER.get(o.severity, 0))
        others = [o.description for o in self.observations if o is not primary]
        job_note = f" (correlated via job_id(s): {', '.join(self.job_ids)})" if self.job_ids else ""
        return (f"Correlated root cause across {len(self.observations)} signals "
                f"[{', '.join(self.signal_types)}]{job_note}: {primary.description}"
                + (f". Also observed: {'; '.join(others)}" if others else ""))


def correlate(observations: list[Observation]) -> list[RootCauseCluster]:
    """Union-find over shared job_ids. Deliberately does NOT correlate on
    weaker signals like 'same adapter name' — that would risk merging two
    genuinely unrelated incidents that happen to involve the same adapter at
    different times. job_id overlap means they came from the literal same
    execution, which is as precise as correlation gets without guessing."""
    n = len(observations)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    job_id_sets = [_job_ids_of(o.evidence) for o in observations]
    for i in range(n):
        if not job_id_sets[i]:
            continue
        for j in range(i + 1, n):
            if job_id_sets[j] and job_id_sets[i] & job_id_sets[j]:
                union(i, j)

    groups: dict[int, list[Observation]] = defaultdict(list)
    for idx, o in enumerate(observations):
        groups[find(idx)].append(o)
    return [RootCauseCluster(obs) for obs in groups.values()]


# ---- orchestration ----------------------------------------------------------

_UPGRADE_SUGGESTIONS = {
    "failed_job": "review why this adapter's jobs are failing; consider adding a pre-flight check or clearer task-authoring guidance",
    "validator_failure": "either the validator is too strict for how it's being used, or the agent needs clearer task instructions to satisfy it — investigate before loosening the check",
    "repeated_retry": "investigate why this task needed repeated submission; likely an unclear task description or an unhandled failure mode",
    "service_health": "investigate adapter/provider health before routing more work to it; may need a circuit breaker in task_router",
    "error_log": "add a specific, named check for this error pattern so it surfaces as a structured signal instead of raw log text",
    "performance_regression": "profile the slow job; check for sandbox size growth or an external dependency slowdown",
    "unresolved_proposal": "review this proposal and explicitly advance, reject, or archive it",
    "capability_gap": "decide whether to build this adapter/integration or formally deprioritize it",
    "operational_friction": "review whether authority_policy is over-blocking legitimate low-risk work, or whether risky requests are genuinely common",
}


def run(auto_propose: bool = True, governing_priority_id: str | None = None) -> ObserveReport:
    entries = audit.read_all()
    jobs = _job_results()

    observations: list[Observation] = []
    observations += signal_failed_jobs(entries, jobs)
    observations += signal_validator_failures(entries, jobs)
    observations += signal_repeated_retries(entries)
    observations += signal_service_health(entries)
    observations += signal_error_logs(jobs)
    observations += signal_performance_regression(jobs)
    observations += signal_unresolved_proposals()
    observations += signal_capability_gaps()
    observations += signal_operational_friction(entries)
    observations += signal_governing_priority_needs_proposal(governing_priority_id)

    for i, o in enumerate(observations):
        o.observation_id = f"obs-{i}-{o.fingerprint()[:8]}"

    clusters = correlate(observations)
    root_causes_correlated = [
        {"signal_types": c.signal_types, "job_ids": c.job_ids, "observation_ids": [o.observation_id for o in c.observations]}
        for c in clusters if len(c.observations) > 1
    ]

    proposals_created: list[str] = []
    proposals_reused: list[str] = []
    proposals_known_deferred: list[str] = []
    proposals_on_cooldown: list[str] = []

    if auto_propose:
        for cluster in clusters:
            if cluster.severity == "low":
                continue  # avoid proposal spam for minor/single-occurrence signals
            fp = cluster.fingerprint()

            existing = proposal_mod.find_open_by_fingerprint(fp)
            if existing:
                for o in cluster.observations:
                    o.linked_proposal_id = existing.proposal_id
                if proposal_mod.is_deferred_and_current(existing):
                    # Known, temporary, already-recorded condition — link for
                    # traceability but do NOT treat it as fresh attention-needed.
                    proposals_known_deferred.append(existing.proposal_id)
                else:
                    proposals_reused.append(existing.proposal_id)
                continue

            on_cooldown = find_on_cooldown(fp)
            if on_cooldown:
                for o in cluster.observations:
                    o.linked_proposal_id = on_cooldown.proposal_id
                proposals_on_cooldown.append(on_cooldown.proposal_id)
                continue

            suggestions = list(dict.fromkeys(
                _UPGRADE_SUGGESTIONS.get(st, "review and decide next step") for st in cluster.signal_types
            ))
            p = proposal_mod.create(
                observed_weakness=cluster.description(),
                proposed_upgrade="; ".join(suggestions),
                risk_score=cluster.suggested_risk_score,
                origin="observe_engine",
                fingerprint=fp,
                source_observation_ids=[o.observation_id for o in cluster.observations],
            )
            for o in cluster.observations:
                o.linked_proposal_id = p.proposal_id
            proposals_created.append(p.proposal_id)

    OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for o in observations:
        (OBSERVATIONS_DIR / f"{ts}_{o.observation_id}.json").write_text(json.dumps(asdict(o), indent=2))

    report = ObserveReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        observations=[asdict(o) for o in observations],
        proposals_created=proposals_created,
        proposals_reused=proposals_reused,
        proposals_known_deferred=proposals_known_deferred,
        proposals_on_cooldown=proposals_on_cooldown,
        root_causes_correlated=root_causes_correlated,
        summary={
            "total_audit_entries": len(entries),
            "total_jobs": len(jobs),
            "observations_by_signal": dict(Counter(o.signal_type for o in observations)),
            "observations_by_severity": dict(Counter(o.severity for o in observations)),
            "raw_observation_count": len(observations),
            "correlated_cluster_count": len(clusters),
            "multi_signal_clusters": len(root_causes_correlated),
        },
    )
    (OBSERVATIONS_DIR / f"{ts}_report.json").write_text(json.dumps(asdict(report), indent=2))
    return report


if __name__ == "__main__":
    r = run()
    print(json.dumps(asdict(r), indent=2))
