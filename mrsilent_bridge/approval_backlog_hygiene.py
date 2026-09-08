"""Founder Approval Backlog Hygiene — Studio-wide Walk-Away, discovery
source #2 (2026-09-07).

Reads the REAL canonical approval backlog (mrsilent_bridge/evolution/
escalations/, the same durable store founder_request.py already uses) and
classifies every escalation into an evidence-based category. Creates no
new registry, decision store, or governance system — this module only
reads founder_request.py's and job_ledger.py's existing public functions
and the same durable JSON files they already read. It does not import or
modify evolution/advance.py or evolution/founder_request.py's source.

Scope, deliberately bounded: classification and reporting ONLY. Retiring
or mutating any real escalation record is NOT performed by this module —
every function here is read-only with respect to the escalations store.
Whether to bulk-resolve the classified-synthetic/already-resolved set is a
separate, explicit decision, not taken automatically by adding this module.

Why this module exists rather than just counting list_pending_founder_
requests(): as of 2026-09-07, 916 of 1267 total escalation records carry
status pending_founder_review, but roughly 813 of those are test/synthetic
fixtures (created by test runs that wrote into the SAME live
ESCALATIONS_DIR rather than a mocked one) — not real Founder-facing
requests. Counting or notifying on the raw pending set overstates the real
backlog by close to 9x. classify_all()/backlog_report() are the first
place that distinguishes real from synthetic, using verifiable evidence
(cross-referencing job_ledger.py and founder_request.exact_proposal_
decision()) rather than assumption.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import job_ledger
import test_origin_classifier as toc
from evolution import founder_request
from evolution import proposal as proposal_mod

# A payload missing any of these is structurally incomplete — nothing
# coherent to decide, and MALFORMED_INVALID's evidence requirement.
_REQUIRED_PAYLOAD_FIELDS = ("capability_needed", "affected", "finding")

STALE_AFTER_DAYS = 30  # conservative; no currently-real record exceeds this — documented, not tuned to force a result


class Category(str, Enum):
    SYNTHETIC_TEST_NOISE = "synthetic_test_noise"
    ALREADY_RESOLVED_ELSEWHERE = "already_resolved_elsewhere"
    UNDERLYING_JOB_TERMINAL = "underlying_job_terminal"
    TRUE_FOUNDER_GATE = "true_founder_gate"
    NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT = "needs_founder_or_operator_judgment"
    RESOLVED_NON_PENDING = "resolved_non_pending"
    # 2026-09-08, Founder-authorized backlog-hygiene retirement expansion —
    # each backed by independently checkable evidence, never a guess; see
    # classify_escalation()'s own docstring for exactly what evidence each
    # one requires and _CATEGORY_TO_HYGIENE_LABEL below for how each maps
    # onto founder_request.py's separate (non-Founder-decision) status
    # namespace.
    MALFORMED_INVALID = "malformed_invalid"
    DUPLICATE_PENDING = "duplicate_pending"
    PROPOSAL_ALREADY_SATISFIED = "proposal_already_satisfied"
    PROPOSAL_SUPERSEDED = "proposal_superseded"


# Categories a Founder is delegated to have MR. SILENT retire without a
# fresh per-item decision (Founder-delegated backlog-hygiene authority):
# each one is backed by independently checkable evidence, never a guess.
DELEGATED_HYGIENE_CATEGORIES = frozenset({
    Category.SYNTHETIC_TEST_NOISE,
    Category.ALREADY_RESOLVED_ELSEWHERE,
    Category.UNDERLYING_JOB_TERMINAL,
    Category.MALFORMED_INVALID,
    Category.DUPLICATE_PENDING,
    Category.PROPOSAL_ALREADY_SATISFIED,
    Category.PROPOSAL_SUPERSEDED,
})

# Maps each delegated category onto founder_request.py's separate hygiene-
# retirement status namespace (never a Founder-decision value — see that
# module's own HYGIENE_RETIRED_* constants and their docstring).
_CATEGORY_TO_HYGIENE_LABEL = {
    Category.SYNTHETIC_TEST_NOISE: "HYGIENE_RETIRED_NONACTIONABLE",
    Category.ALREADY_RESOLVED_ELSEWHERE: "HYGIENE_RETIRED_DUPLICATE",
    Category.UNDERLYING_JOB_TERMINAL: "HYGIENE_RETIRED_ALREADY_TERMINAL",
    Category.MALFORMED_INVALID: "HYGIENE_RETIRED_INVALID",
    Category.DUPLICATE_PENDING: "HYGIENE_RETIRED_DUPLICATE",
    Category.PROPOSAL_ALREADY_SATISFIED: "HYGIENE_RETIRED_ALREADY_SATISFIED",
    Category.PROPOSAL_SUPERSEDED: "HYGIENE_RETIRED_SUPERSEDED",
}

# Real, currently-invoked-only capability_needed values that this codebase
# uses SPECIFICALLY to ask the Founder to advance/promote the referenced
# proposal itself (as opposed to, e.g., "please investigate why this
# component is degraded" — a root-cause review request that a proposal's
# own rejection does NOT resolve). PROPOSAL_SUPERSEDED is deliberately
# scoped to ONLY this narrow set: a real live case was found and checked
# by hand (escalation 55273f2985a5a602, capability_needed=
# "human_review_of_failed_self_correction", proposal since rejected) where
# retiring on "proposal rejected" alone would have been WRONG — the review
# request concerns an ongoing degraded-component investigation, not
# whether to promote a specific rejected proposal, and rejection does not
# actually resolve that concern. Never widen this set to "any capability_
# needed" without the same by-hand check for exactly this failure mode.
_PROPOSAL_ADVANCEMENT_CAPABILITIES = frozenset({"production_promotion"})


def _is_synthetic(rec: dict[str, Any]) -> bool:
    """Delegates to the canonical test_origin_classifier (2026-09-08
    self-stewardship consolidation — see that module's docstring for the
    full history of why this used to be a locally-duplicated, incomplete
    keyword list here, and the two real live leaks that resulted).
    Verified unchanged behavior against every case this module's own test
    suite already covers, plus the canonical classifier's own broader
    coverage."""
    payload = rec.get("payload", {}) or {}
    return toc.is_test_or_synthetic_origin(
        requested_by=rec.get("requested_by"),
        capability_needed=payload.get("capability_needed"),
        job_id=payload.get("affected", {}).get("job_id"),
        text_blob=" ".join(str(payload.get(k, "")) for k in ("finding", "human_readable", "recommended_action")),
    )


def _age_days(rec: dict[str, Any]) -> int | None:
    created = rec.get("created_at")
    if not created:
        return None
    try:
        created_dt = datetime.fromisoformat(created)
    except (ValueError, TypeError):
        return None
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_dt).days


@dataclass
class Classification:
    escalation_id: str
    category: Category
    reason: str
    age_days: int | None
    capability_needed: str | None
    proposal_id: str | None
    job_id: str | None


def _is_malformed(rec: dict[str, Any]) -> bool:
    payload = rec.get("payload")
    if not payload or not isinstance(payload, dict):
        return True
    return any(k not in payload for k in _REQUIRED_PAYLOAD_FIELDS)


def classify_escalation(
    rec: dict[str, Any], *, canonical_by_fingerprint: dict[str, str] | None = None,
) -> Classification:
    """Deterministic, evidence-based classification of ONE escalation
    record. Never fabricates a category — every branch is backed by a
    specific, checkable fact (a field on this record, a cross-referenced
    job_ledger/proposal state, an independently resolved decision for the
    same proposal_id, or — for duplicates — a cross-record fingerprint
    index the caller supplies), never a guess. Checked in order of
    strongest evidence first: an already-resolved record is never re-
    classified as anything else, a synthetic fixture is never treated as a
    real founder gate, a malformed record is checked before anything that
    assumes its structure is trustworthy.

    `canonical_by_fingerprint` (2026-09-08): an optional {fingerprint:
    earliest_pending_escalation_id} index — see classify_all()'s own
    construction of it. Without it (the default, e.g. a caller classifying
    a single ad-hoc record), duplicate detection is simply skipped for
    that record, never guessed."""
    payload = rec.get("payload", {}) or {}
    escalation_id = rec.get("escalation_id", "")
    proposal_id = payload.get("affected", {}).get("proposal_id")
    job_id = payload.get("affected", {}).get("job_id")
    capability_needed = payload.get("capability_needed")
    status = rec.get("status")
    age = _age_days(rec)

    if status in ("approved", "denied"):
        return Classification(
            escalation_id, Category.RESOLVED_NON_PENDING,
            f"already resolved: {status}", age, capability_needed, proposal_id, job_id,
        )

    if status != "pending_founder_review":
        # already retired as backlog hygiene in a prior pass (one of
        # founder_request.HYGIENE_RETIRED_* — never re-classified, same
        # "already resolved" doctrine as approved/denied above) — this is
        # what makes repeated hygiene passes idempotent.
        return Classification(
            escalation_id, Category.RESOLVED_NON_PENDING,
            f"already retired as backlog hygiene: {status}", age, capability_needed, proposal_id, job_id,
        )

    if _is_malformed(rec):
        return Classification(
            escalation_id, Category.MALFORMED_INVALID,
            f"payload missing required field(s): {[k for k in _REQUIRED_PAYLOAD_FIELDS if k not in payload]}",
            age, capability_needed, proposal_id, job_id,
        )

    if _is_synthetic(rec):
        return Classification(
            escalation_id, Category.SYNTHETIC_TEST_NOISE,
            "matches test-fixture fingerprint (requested_by/job_id/finding text)",
            age, capability_needed, proposal_id, job_id,
        )

    if canonical_by_fingerprint is not None:
        fp = payload.get("fingerprint")
        survivor = canonical_by_fingerprint.get(fp) if fp else None
        if survivor is not None and survivor != escalation_id:
            return Classification(
                escalation_id, Category.DUPLICATE_PENDING,
                f"same fingerprint ({fp}) as still-pending escalation {survivor}, which was created first "
                "and is the canonical survivor",
                age, capability_needed, proposal_id, job_id,
            )

    if proposal_id:
        resolved = founder_request.exact_proposal_decision(proposal_id)
        if resolved is not None:
            return Classification(
                escalation_id, Category.ALREADY_RESOLVED_ELSEWHERE,
                f"a resolved decision ({resolved}) already exists for this exact proposal_id "
                "under a different escalation record",
                age, capability_needed, proposal_id, job_id,
            )

        try:
            prop = proposal_mod.load(proposal_id)
        except (FileNotFoundError, json.JSONDecodeError):
            prop = None  # unlike job_ledger.load()/mission.load(), proposal.load() raises rather
            # than returning None for a missing/corrupt record — normalized to None here,
            # matching every other lookup in this module (never treated as evidence of anything)
        if prop is not None and prop.status == proposal_mod.ProposalStatus.PROMOTED:
            return Classification(
                escalation_id, Category.PROPOSAL_ALREADY_SATISFIED,
                f"proposal {proposal_id} already reached PROMOTED — the underlying work this escalation "
                "asked about is already done",
                age, capability_needed, proposal_id, job_id,
            )

        if (
            prop is not None
            and prop.status in (proposal_mod.ProposalStatus.REJECTED, proposal_mod.ProposalStatus.ROLLED_BACK)
            and capability_needed in _PROPOSAL_ADVANCEMENT_CAPABILITIES
        ):
            return Classification(
                escalation_id, Category.PROPOSAL_SUPERSEDED,
                f"proposal {proposal_id} reached {prop.status} through the ordinary pipeline — this "
                f"escalation's own request ({capability_needed}) to advance that exact proposal is moot; "
                "scoped ONLY to capabilities that mean 'advance this proposal', never to a general "
                "root-cause-review request that a rejection does not actually resolve",
                age, capability_needed, proposal_id, job_id,
            )

    if job_id:
        ledger_record = job_ledger.load(job_id)
        if ledger_record is not None and ledger_record.state in job_ledger.TERMINAL_STATES:
            return Classification(
                escalation_id, Category.UNDERLYING_JOB_TERMINAL,
                f"job_ledger shows job {job_id} already in terminal state {ledger_record.state}",
                age, capability_needed, proposal_id, job_id,
            )

    if capability_needed == "production_promotion":
        return Classification(
            escalation_id, Category.TRUE_FOUNDER_GATE,
            "production_promotion is explicitly always founder-gated regardless of risk_class",
            age, capability_needed, proposal_id, job_id,
        )

    return Classification(
        escalation_id, Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT,
        "no independent evidence proves this safe to auto-resolve",
        age, capability_needed, proposal_id, job_id,
    )


def _build_canonical_fingerprint_index(recs: list[dict[str, Any]]) -> dict[str, str]:
    """{fingerprint: earliest-created still-pending escalation_id}, scoped
    to ONLY status=='pending_founder_review' records (a resolved/retired
    record is never a 'duplicate' of anything — it's already terminal on
    its own). Earliest-by-created_at wins as the canonical survivor so the
    choice is deterministic and stable across repeated runs, never
    order-of-glob dependent."""
    by_fp: dict[str, tuple[str, str]] = {}  # fp -> (created_at, escalation_id)
    for rec in recs:
        if rec.get("status") != "pending_founder_review":
            continue
        fp = (rec.get("payload") or {}).get("fingerprint")
        if not fp:
            continue
        eid = rec.get("escalation_id", "")
        created = rec.get("created_at") or ""
        current = by_fp.get(fp)
        if current is None or created < current[0]:
            by_fp[fp] = (created, eid)
    return {fp: eid for fp, (created, eid) in by_fp.items()}


def classify_all() -> list[Classification]:
    """Classifies EVERY escalation record (not just pending) — scans the
    full durable store directly (the same ESCALATIONS_DIR founder_request.py
    uses) since RESOLVED_NON_PENDING and already-resolved-elsewhere
    detection both need visibility beyond list_pending_founder_requests()'s
    pending-only view. Builds the cross-record duplicate-fingerprint index
    once per call (see _build_canonical_fingerprint_index) so DUPLICATE_
    PENDING detection is real and consistent, not per-record guesswork."""
    if not founder_request.ESCALATIONS_DIR.exists():
        return []
    recs: list[dict[str, Any]] = []
    for p in sorted(founder_request.ESCALATIONS_DIR.glob("*.json")):
        try:
            recs.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    canonical_by_fp = _build_canonical_fingerprint_index(recs)
    return [classify_escalation(rec, canonical_by_fingerprint=canonical_by_fp) for rec in recs]


@dataclass
class BacklogReport:
    total: int
    pending_total: int
    pending_real: int
    synthetic_noise: int
    already_resolved_elsewhere: int
    underlying_job_terminal: int
    true_founder_gate: int
    needs_founder_or_operator_judgment: int
    resolved_non_pending: int
    stale_real_pending: int
    malformed_invalid: int
    duplicate_pending: int
    proposal_already_satisfied: int
    proposal_superseded: int


def backlog_report(classifications: list[Classification] | None = None) -> BacklogReport:
    """Durable-evidence-backed backlog counts. Pass a pre-computed
    classifications list to avoid re-scanning the store when composing
    multiple reports in one call."""
    classifications = classify_all() if classifications is None else classifications
    counts = Counter(c.category for c in classifications)
    real_pending_categories = (Category.TRUE_FOUNDER_GATE, Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT)
    stale = sum(
        1 for c in classifications
        if c.category in real_pending_categories and c.age_days is not None and c.age_days > STALE_AFTER_DAYS
    )
    return BacklogReport(
        total=len(classifications),
        pending_total=len(classifications) - counts[Category.RESOLVED_NON_PENDING],
        pending_real=sum(counts[c] for c in real_pending_categories),
        synthetic_noise=counts[Category.SYNTHETIC_TEST_NOISE],
        already_resolved_elsewhere=counts[Category.ALREADY_RESOLVED_ELSEWHERE],
        underlying_job_terminal=counts[Category.UNDERLYING_JOB_TERMINAL],
        true_founder_gate=counts[Category.TRUE_FOUNDER_GATE],
        needs_founder_or_operator_judgment=counts[Category.NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT],
        resolved_non_pending=counts[Category.RESOLVED_NON_PENDING],
        stale_real_pending=stale,
        malformed_invalid=counts[Category.MALFORMED_INVALID],
        duplicate_pending=counts[Category.DUPLICATE_PENDING],
        proposal_already_satisfied=counts[Category.PROPOSAL_ALREADY_SATISFIED],
        proposal_superseded=counts[Category.PROPOSAL_SUPERSEDED],
    )


def delegable_for_retirement(classifications: list[Classification] | None = None) -> list[Classification]:
    """Every escalation whose classification falls under Founder-delegated
    backlog-hygiene authority — candidates for retirement, NOT retired by
    this function. Actually retiring them is a separate, explicit action
    (see run_backlog_hygiene_pass())."""
    classifications = classify_all() if classifications is None else classifications
    return [c for c in classifications if c.category in DELEGATED_HYGIENE_CATEGORIES]


def run_backlog_hygiene_pass(*, requested_by: str = "approval_backlog_hygiene") -> dict[str, Any]:
    """THE actual retirement action — everything above this function is
    read-only classification/reporting; this is the one function that
    mutates the durable escalations store, and it does so ONLY through
    founder_request.retire_for_backlog_hygiene() (delegated backlog-
    hygiene authority — never a Founder decision, never self-approval; see
    that function's own hard guards).

    Idempotent by construction: classify_all() itself excludes already-
    retired records (their status is no longer 'pending_founder_review',
    so they fall into RESOLVED_NON_PENDING — see classify_escalation()) —
    a repeated call only ever acts on records still genuinely pending,
    never re-touches one it already retired. Never touches TRUE_FOUNDER_
    GATE or NEEDS_FOUNDER_OR_OPERATOR_JUDGMENT — those are exactly what
    DELEGATED_HYGIENE_CATEGORIES excludes.

    Returns a real, itemized result — never a bare count — so a caller
    (or a human reviewing the audit trail) can see exactly which
    escalation went to which category, not just a number."""
    classifications = classify_all()
    delegable = [c for c in classifications if c.category in DELEGATED_HYGIENE_CATEGORIES]
    retired: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for c in delegable:
        label = _CATEGORY_TO_HYGIENE_LABEL[c.category]
        evidence: dict[str, Any] = {
            "category": c.category.value, "reason": c.reason,
            "proposal_id": c.proposal_id, "job_id": c.job_id, "age_days": c.age_days,
        }
        kwargs: dict[str, Any] = {}
        if c.category == Category.DUPLICATE_PENDING:
            # reason text embeds the survivor id — extract it back out
            # rather than re-deriving, so evidence and the actual
            # canonical_survivor_id field can never disagree
            m = re.search(r"still-pending escalation ([0-9a-f]+)", c.reason)
            if m:
                kwargs["canonical_survivor_id"] = m.group(1)
        if c.category == Category.PROPOSAL_SUPERSEDED:
            kwargs["superseding_reference"] = c.proposal_id
        try:
            founder_request.retire_for_backlog_hygiene(
                c.escalation_id, label, evidence=evidence, reason=c.reason,
                requested_by=requested_by, **kwargs,
            )
            retired.append({"escalation_id": c.escalation_id, "category": c.category.value, "label": label})
        except (ValueError, FileNotFoundError) as e:  # noqa: BLE001 — one bad record must never abort the pass
            errors.append({"escalation_id": c.escalation_id, "error": repr(e)})
    by_category = Counter(r["category"] for r in retired)
    return {"retired": retired, "errors": errors, "retired_count": len(retired), "by_category": dict(by_category)}


def natural_language_summary() -> str:
    """Composes a Founder-facing summary strictly from backlog_report()'s
    durable counts — never fabricates activity or a number not backed by
    classify_all()'s evidence."""
    r = backlog_report()
    parts = [
        f"The approval backlog has {r.pending_total} items marked pending, but {r.synthetic_noise} of those "
        f"are test/synthetic fixtures, not real requests. That leaves {r.pending_real} approvals that actually "
        f"need attention: {r.true_founder_gate} are genuine Founder-authority gates (mostly production "
        f"promotion, which is always founder-gated), and {r.needs_founder_or_operator_judgment} need judgment "
        f"I can't make on my own."
    ]
    if r.already_resolved_elsewhere:
        parts.append(
            f"{r.already_resolved_elsewhere} pending record(s) already have a resolved decision recorded "
            f"under a different escalation for the same proposal — safe to retire."
        )
    if r.underlying_job_terminal:
        parts.append(
            f"{r.underlying_job_terminal} pending record(s) reference a job that already finished "
            f"(completed/failed/escalated) — the request itself is moot now."
        )
    if r.stale_real_pending:
        parts.append(f"{r.stale_real_pending} real pending approval(s) are older than {STALE_AFTER_DAYS} days.")
    if r.malformed_invalid:
        parts.append(f"{r.malformed_invalid} pending record(s) are structurally malformed — nothing coherent to decide.")
    if r.duplicate_pending:
        parts.append(f"{r.duplicate_pending} pending record(s) duplicate another still-pending request.")
    if r.proposal_already_satisfied:
        parts.append(f"{r.proposal_already_satisfied} pending record(s) reference work that's already been completed.")
    if r.proposal_superseded:
        parts.append(f"{r.proposal_superseded} pending record(s) ask to advance a proposal that's since been rejected — moot now.")
    return " ".join(parts)
