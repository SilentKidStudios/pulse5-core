"""Studio Status Aggregator — Studio-wide Walk-Away, whole-Studio census
convergence step (2026-09-08).

Combines the three real, tested discovery sources built this campaign
(omni_registry_stewardship.py, approval_backlog_hygiene.py,
failure_anomaly_discovery.py) plus the real Mission/WorkGraph state
(mission.py, work_graph.py) into ONE durable, evidence-based status
snapshot — the first piece of the "converge toward one Studio executive
intake model" goal. Creates no new registry or governance system; every
number here comes from calling each source's own already-tested public
functions, never re-deriving or guessing at their internals.

Deliberately NOT a new decision authority: this module never creates,
mutates, or retires anything. It answers "what does MR. SILENT currently
see" for natural-language Founder reporting and for a human auditing
whole-Studio visibility — read-only, like every module in this campaign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import approval_backlog_hygiene as abh
import failure_anomaly_discovery as fad
import job_ledger
import mission
import omni_registry_stewardship as ors
import omniregistry_manifest as orm
import validation_canary_selfheal_discovery as vcs
import work_graph


@dataclass
class StudioStatus:
    generated_at: str
    # source 1: OmniRegistry divisions (omni_registry_stewardship.py's
    # narrow, schema-fingerprinted, intake-eligible subset)
    registry_candidates_total: int
    registry_candidates_unclaimed: int
    # source 1b (2026-09-08): the SEPARATE, larger canonical division
    # manifest (omniregistry/registry.json) — visibility only, never
    # intake-eligible; see omniregistry_manifest.py's own docstring for why
    # these are two genuinely different registries, not a bug.
    omniregistry_manifest_available: bool
    omniregistry_manifest_total_entries: int
    omniregistry_divisions_active: list[str]
    omniregistry_projects_total: int
    omniregistry_systems_total: int
    omniregistry_future_entities_not_built_total: int
    # source 2: approval backlog
    approvals_pending_raw: int
    approvals_pending_real: int
    approvals_true_founder_gate: int
    approvals_needs_judgment: int
    # source 3: failure/retry/anomaly (bounded window — see anomaly_window_days)
    anomaly_window_days: int
    anomalies_total: int
    anomalies_test_only: int
    anomalies_new_failure: int
    anomalies_needs_founder_gate: int
    # Mission/WorkGraph
    missions_total: int
    workitems_total: int
    workitems_by_state: dict[str, int]
    # source 4: validation/canary/self-heal (unbounded — job_ledger + one
    # live snapshot file, both cheap enough to scan in full every call)
    validation_failures_real: int
    canary_failures_real: int
    independent_validation_disagreements_real: int
    maintenance_findings_active: int
    self_heal_findings_active: int
    # source 5: specialist/engine usage (2026-09-08) — real job_ledger.
    # selected_engine counts, bounded to the same anomaly_window_days
    # recency window as source 3, so "what specialist did you use" answers
    # recent activity rather than an all-time total that never changes
    # meaningfully. See node_capability_registry.py for node-level detail
    # this does not attempt to duplicate (single-node reality today).
    specialist_jobs_recent: int
    specialist_engine_counts_recent: dict[str, int]
    # source 6: self-stewardship (2026-09-08) — real Missions this
    # session's governed self-improvement registration created (origin ==
    # "self_stewardship_discovery"; see self_stewardship_findings work).
    self_stewardship_missions_total: int
    self_stewardship_missions_completed: int


def collect(*, anomaly_window_days: int = 1) -> StudioStatus:
    """Single, bounded, read-only pass over all three discovery sources
    plus real Mission/WorkGraph state. anomaly_window_days bounds the
    failure/retry scan (see failure_anomaly_discovery.classify_all's own
    `since` parameter) — the observation store is large enough that a full
    unbounded historical scan is impractical for a routine status check."""
    registry_candidates = ors.discover_candidate_entities()
    unclaimed = [c for c in registry_candidates if ors.already_has_any_campaign(c) is None]

    manifest = orm.census()

    approval_report = abh.backlog_report()

    since = datetime.now(timezone.utc) - timedelta(days=anomaly_window_days)
    anomaly_rep = fad.anomaly_report(since=since)

    missions = mission.list_all()
    items = work_graph.list_all()
    by_state: dict[str, int] = {}
    for it in items:
        state = getattr(it, "state", "unknown")
        by_state[state] = by_state.get(state, 0) + 1

    all_jobs = job_ledger.list_all()  # loaded once, shared with vcs.scan_all() below — see that
    # function's own 2026-09-08 docstring note on the redundant-scan cost this avoids
    vcs_findings = vcs.scan_all(all_jobs)

    window_start_iso = since.isoformat()
    recent_jobs = [j for j in all_jobs if (j.created_at or "") >= window_start_iso]
    engine_counts_recent: dict[str, int] = {}
    for j in recent_jobs:
        if j.selected_engine:
            engine_counts_recent[j.selected_engine] = engine_counts_recent.get(j.selected_engine, 0) + 1

    self_stewardship_missions = [m for m in missions if m.origin == "self_stewardship_discovery"]
    # Disposition (not category) decides "real and unresolved" — a
    # HISTORICAL_TERMINAL_FINDING (e.g. an old, closed internal-pipeline
    # validation run) must not count as an open production defect even
    # though its category is VALIDATION_FAILURE/etc. See
    # validation_canary_selfheal_discovery's 2026-09-08 classification fix.
    _real_unresolved = [f for f in vcs_findings if f.disposition == vcs.Disposition.UNRESOLVED_REAL_FINDING]
    validation_failures = sum(1 for f in _real_unresolved if f.category == vcs.Category.VALIDATION_FAILURE)
    canary_failures = sum(1 for f in _real_unresolved if f.category == vcs.Category.CANARY_FAILURE)
    independent_disagreements = sum(1 for f in _real_unresolved if f.category == vcs.Category.INDEPENDENT_VALIDATION_DISAGREEMENT)
    maintenance_active = sum(1 for f in vcs_findings if f.category == vcs.Category.MAINTENANCE_FINDING)
    self_heal_active = sum(1 for f in vcs_findings if f.category == vcs.Category.SELF_HEAL_FINDING)

    return StudioStatus(
        generated_at=datetime.now(timezone.utc).isoformat(),
        registry_candidates_total=len(registry_candidates),
        registry_candidates_unclaimed=len(unclaimed),
        omniregistry_manifest_available=manifest is not None,
        omniregistry_manifest_total_entries=manifest.total_entries if manifest else 0,
        omniregistry_divisions_active=manifest.divisions_active if manifest else [],
        omniregistry_projects_total=len(manifest.projects) if manifest else 0,
        omniregistry_systems_total=len(manifest.systems) if manifest else 0,
        omniregistry_future_entities_not_built_total=len(manifest.future_entities_not_built) if manifest else 0,
        approvals_pending_raw=approval_report.pending_total,
        approvals_pending_real=approval_report.pending_real,
        approvals_true_founder_gate=approval_report.true_founder_gate,
        approvals_needs_judgment=approval_report.needs_founder_or_operator_judgment,
        anomaly_window_days=anomaly_window_days,
        anomalies_total=anomaly_rep.total,
        anomalies_test_only=anomaly_rep.test_only,
        anomalies_new_failure=anomaly_rep.new_failure,
        anomalies_needs_founder_gate=anomaly_rep.needs_founder_gate,
        missions_total=len(missions),
        workitems_total=len(items),
        workitems_by_state=by_state,
        validation_failures_real=validation_failures,
        canary_failures_real=canary_failures,
        independent_validation_disagreements_real=independent_disagreements,
        maintenance_findings_active=maintenance_active,
        self_heal_findings_active=self_heal_active,
        specialist_jobs_recent=len(recent_jobs),
        specialist_engine_counts_recent=engine_counts_recent,
        self_stewardship_missions_total=len(self_stewardship_missions),
        self_stewardship_missions_completed=sum(
            1 for m in self_stewardship_missions if m.state == mission.MissionState.COMPLETED
        ),
    )


def natural_language_status(*, anomaly_window_days: int = 1) -> str:
    """Answers 'what is MR. SILENT currently working on / waiting on' from
    durable evidence only — every number traces to a real, tested
    discovery source's own report, never fabricated here."""
    s = collect(anomaly_window_days=anomaly_window_days)
    parts = [
        f"Right now: {s.missions_total} Missions tracking {s.workitems_total} WorkItems "
        f"({s.workitems_by_state.get('completed', 0)} completed, "
        f"{s.workitems_by_state.get('runnable', 0)} runnable, "
        f"{s.workitems_by_state.get('repairable_failed', 0)} needing repair)."
    ]
    if s.registry_candidates_unclaimed:
        parts.append(f"{s.registry_candidates_unclaimed} registered Studio divisions are unclaimed and eligible for stewardship.")
    else:
        parts.append("No unclaimed registered Studio divisions right now — the registry's real work has already been picked up.")
    if s.omniregistry_manifest_available:
        parts.append(
            f"Whole-Studio manifest: {len(s.omniregistry_divisions_active)} active divisions, "
            f"{s.omniregistry_projects_total} projects, {s.omniregistry_systems_total} systems "
            f"({s.omniregistry_future_entities_not_built_total} future/conceptual entities explicitly not built, "
            "not eligible for auto-start)."
        )
    parts.append(
        f"Approvals: {s.approvals_pending_real} real items need attention "
        f"({s.approvals_true_founder_gate} are genuine Founder gates), out of {s.approvals_pending_raw} raw pending "
        "(most of the difference is test/synthetic noise)."
    )
    if s.anomalies_new_failure:
        parts.append(f"{s.anomalies_new_failure} new real failures surfaced in the last {s.anomaly_window_days} day(s).")
    if s.validation_failures_real or s.canary_failures_real or s.independent_validation_disagreements_real:
        parts.append(
            f"Validation/canary: {s.validation_failures_real} real validation failures, "
            f"{s.canary_failures_real} real canary failures, and {s.independent_validation_disagreements_real} "
            "cases where an independent recheck disagreed with the primary validator."
        )
    if s.maintenance_findings_active or s.self_heal_findings_active:
        parts.append(
            f"Maintenance: {s.maintenance_findings_active} active maintenance finding(s) and "
            f"{s.self_heal_findings_active} active self-heal finding(s) right now."
        )
    else:
        parts.append("No active maintenance or self-heal findings right now.")
    if s.specialist_engine_counts_recent:
        engine_summary = ", ".join(f"{v} via {k}" for k, v in sorted(s.specialist_engine_counts_recent.items()))
        parts.append(f"In the last {s.anomaly_window_days} day(s), {s.specialist_jobs_recent} job(s) ran ({engine_summary}).")
    else:
        parts.append(f"No specialist-routed jobs ran in the last {s.anomaly_window_days} day(s).")
    if s.self_stewardship_missions_total:
        parts.append(
            f"Self-stewardship: {s.self_stewardship_missions_completed} of "
            f"{s.self_stewardship_missions_total} self-improvement Mission(s) completed."
        )
    return " ".join(parts)
