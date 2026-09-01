"""Self-evolution proposal lifecycle: OBSERVE -> PROPOSE -> (IMPLEMENT via bridge)
-> TEST -> CANARY/VALIDATE -> PROMOTE OR ROLLBACK -> RECORD LESSON.

Every step here is data: creating a Proposal writes a JSON file and nothing
else. There is no scheduler, cron job, or daemon in this project that reads
these files and acts on them automatically — a human (or a future, separately
authorized process) must run the CLI to advance a proposal's status. This is
the "proposal-first" requirement: the pipeline can represent the full
lifecycle, but nothing here is wired to execute it unattended, and
implementation is never applied directly to mrsilent_bridge's own code
(see authority_policy.classify's self-modification jail check).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROPOSALS_DIR = Path(__file__).resolve().parent / "proposals"
LESSONS_PATH = Path(__file__).resolve().parent / "lessons.jsonl"


class ProposalStatus:
    OBSERVED = "observed"
    PROPOSED = "proposed"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    CANARY = "canary"
    PROMOTION_CANDIDATE = "promotion_candidate"  # auto-advance's terminal state: ready, not yet promoted
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    DEFERRED = "deferred"  # known condition, not a bug — revisit after deferred_until


@dataclass
class Proposal:
    proposal_id: str
    created_at: str
    observed_weakness: str
    proposed_upgrade: str
    risk_score: str  # low/medium/founder_gated — mirrors authority_policy.RiskClass
    status: str = ProposalStatus.OBSERVED
    implementation_job_id: str | None = None  # most-recent job_id; kept for backward compat
    implementation_job_ids: list[str] = field(default_factory=list)  # full lineage, in order — see append_implementation_job()
    implementation_attempts: int = 0  # == len(implementation_job_ids); explicit for quick inspection/CLI display
    lesson: str | None = None
    history: list[dict] = field(default_factory=list)
    origin: str = "manual"  # "manual" (cli propose) or "observe_engine"
    fingerprint: str | None = None  # dedupe key for observe_engine-created proposals
    source_observation_ids: list[str] = field(default_factory=list)
    deferred_until: str | None = None  # ISO timestamp; set by defer()
    defer_reason: str | None = None
    founder_priority: int | None = None  # Phase Q: explicit Founder override, higher = more urgent; None = inferred priority applies
    paid_resources_allowed: bool = True  # HARD eligibility policy consumed by studio_router.rank(allow_paid=...)
    # via evolution/advance.py._implementation_router() — NOT a soft score.
    # Default True preserves existing behavior for the pre-existing proposal
    # store and the OBSERVE engine, neither of which ever declared this
    # constraint; the external-facing entry point (th3_mcp's submit_work)
    # defaults its OWN parameter to False instead, at that call site only.


# Event-driven pickup wake signal (mrsilent-autonomous-cycle.path watches
# ONLY this single file, via PathModified=). Deliberately a SIBLING of
# PROPOSALS_DIR, never inside it, and touched ONLY here in create() -- never
# in advance()/save(), which run repeatedly during the cycle's OWN
# processing of an already-existing proposal. That's what keeps this a
# clean "new work arrived" signal instead of a continuous self-retrigger:
# watching the whole proposals/ directory would fire on every stage
# transition the cycle itself makes while already running. Contains no task
# data/authority -- a bare timestamp only; the existing proposals/ store and
# job_ledger remain the sole source of truth for what work exists and who
# gets to decide about it. A failure to write it is caught and ignored: the
# durable proposal record (save(p), just above) has already succeeded by the
# time this runs, so a wake-hint failure can never lose or block real work --
# worst case, the existing 15-minute timer still picks it up.
NEW_PROPOSAL_SIGNAL_PATH = PROPOSALS_DIR.parent / "new_proposal_signal"


def _path(proposal_id: str) -> Path:
    return PROPOSALS_DIR / f"{proposal_id}.json"


def _touch_new_proposal_signal(at: str) -> None:
    try:
        NEW_PROPOSAL_SIGNAL_PATH.write_text(at)
    except OSError:
        pass


def create(
    observed_weakness: str,
    proposed_upgrade: str,
    risk_score: str,
    *,
    origin: str = "manual",
    fingerprint: str | None = None,
    source_observation_ids: list[str] | None = None,
    paid_resources_allowed: bool = True,
) -> Proposal:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    p = Proposal(
        proposal_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        observed_weakness=observed_weakness,
        proposed_upgrade=proposed_upgrade,
        risk_score=risk_score,
        origin=origin,
        fingerprint=fingerprint,
        source_observation_ids=source_observation_ids or [],
        paid_resources_allowed=paid_resources_allowed,
    )
    p.history.append({"at": p.created_at, "event": "created", "status": p.status})
    save(p)
    _touch_new_proposal_signal(p.created_at)  # durable write already succeeded above; this is a best-effort wake hint only
    return p


CLOSED_STATUSES = frozenset({ProposalStatus.PROMOTED, ProposalStatus.ROLLED_BACK, ProposalStatus.REJECTED})


def find_open_by_fingerprint(fingerprint: str) -> Proposal | None:
    """An 'open' proposal is one not yet resolved (promoted/rolled_back/rejected).
    DEFERRED counts as open (it's still the canonical record for the condition,
    just not currently actionable) — used by the OBSERVE engine to avoid
    creating duplicate proposals for a recurring signal it has already raised."""
    for p in list_all():
        if p.fingerprint == fingerprint and p.status not in CLOSED_STATUSES:
            return p
    return None


def defer(proposal_id: str, until: str, reason: str) -> Proposal:
    """Mark a proposal as a known, temporary, non-actionable condition rather
    than an open weakness. `until` is an ISO timestamp (best-effort if the
    source data was ambiguous — see caller). Distinct from REJECTED: this
    isn't wrong or unwanted, it's just not worth acting on yet."""
    p = load(proposal_id)
    p.status = ProposalStatus.DEFERRED
    p.deferred_until = until
    p.defer_reason = reason
    p.history.append({"at": datetime.now(timezone.utc).isoformat(), "event": "deferred",
                       "until": until, "reason": reason})
    save(p)
    return p


def is_deferred_and_current(p: Proposal) -> bool:
    """True if p is DEFERRED and its deferred_until window hasn't passed yet."""
    if p.status != ProposalStatus.DEFERRED or not p.deferred_until:
        return False
    try:
        until = datetime.fromisoformat(p.deferred_until)
    except ValueError:
        return True  # unparseable date: treat conservatively as still-deferred rather than resurfacing
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < until


def set_implementation_job(proposal_id: str, job_id: str) -> Proposal:
    """Kept for backward compatibility; delegates to append_implementation_job()."""
    return append_implementation_job(proposal_id, job_id)


def append_implementation_job(proposal_id: str, job_id: str, *, engine: str | None = None, note: str = "") -> Proposal:
    """STABLE IMPLEMENTATION IDENTITY (#1): records one more entry in this
    proposal's implementation lineage. Idempotent — appending the same
    job_id twice (e.g. once from an on_job_created callback the instant the
    job is created, and again defensively after the attempt returns) is a
    no-op the second time, never a duplicate lineage entry. This is the
    ONLY place implementation_job_ids is ever mutated, so a job_id is linked
    to its proposal the moment it exists — closing the "crash after routing,
    before the attempt returns" gap: on restart, the proposal already knows
    about the job_id even if the process died mid-attempt."""
    p = load(proposal_id)
    if job_id not in p.implementation_job_ids:
        p.implementation_job_ids.append(job_id)
        p.implementation_attempts = len(p.implementation_job_ids)
        p.history.append({
            "at": datetime.now(timezone.utc).isoformat(), "event": "implementation_job_appended",
            "job_id": job_id, "engine": engine, "attempt_number": p.implementation_attempts, "note": note,
        })
    p.implementation_job_id = job_id  # most-recent pointer always advances, even on a repeat call
    save(p)
    return p


def save(p: Proposal) -> None:
    _path(p.proposal_id).write_text(json.dumps(asdict(p), indent=2))


def load(proposal_id: str) -> Proposal:
    data = json.loads(_path(proposal_id).read_text())
    return Proposal(**data)


def list_all() -> list[Proposal]:
    if not PROPOSALS_DIR.exists():
        return []
    return [load(f.stem) for f in sorted(PROPOSALS_DIR.glob("*.json"))]


def advance(proposal_id: str, new_status: str, note: str = "") -> Proposal:
    """Manually move a proposal forward. Must be called explicitly — nothing
    calls this on its own. FOUNDER_GATED-risk proposals should not be advanced
    past PROPOSED without a human explicitly doing so outside this bridge."""
    p = load(proposal_id)
    p.status = new_status
    p.history.append({"at": datetime.now(timezone.utc).isoformat(), "event": "advanced", "status": new_status, "note": note})
    save(p)
    return p


def set_founder_priority(proposal_id: str, priority: int, *, note: str = "") -> Proposal:
    """Phase Q: an EXPLICIT Founder override, never inferred by any code
    path in this project — evolution/prioritization.py's priority_key()
    always ranks a founder_priority proposal ahead of any inferred score,
    regardless of recurrence/evidence, exactly as instructed ('Preserve
    explicit Founder priorities over inferred priorities'). Higher number =
    processed sooner."""
    p = load(proposal_id)
    p.founder_priority = priority
    p.history.append({"at": datetime.now(timezone.utc).isoformat(), "event": "founder_priority_set",
                       "priority": priority, "note": note})
    save(p)
    return p


def record_lesson(proposal_id: str, lesson: str) -> Proposal:
    p = load(proposal_id)
    p.lesson = lesson
    p.history.append({"at": datetime.now(timezone.utc).isoformat(), "event": "lesson_recorded"})
    save(p)
    with LESSONS_PATH.open("a") as f:
        f.write(json.dumps({"proposal_id": proposal_id, "lesson": lesson,
                             "at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return p


# --- PRE-24x7 Phase J: structured experience records (additive, same file) --
#
# record_lesson() above is unchanged and still works exactly as before — every
# existing caller keeps working. record_experience() is a strict superset:
# it writes to the SAME lessons.jsonl (not a second memory store), still sets
# Proposal.lesson to a readable one-line summary (so anything reading that
# plain field never breaks), and additionally embeds a structured
# `experience_record` alongside it. Old lines (no "experience_record" key)
# remain fully readable — find_known_remediation() below simply skips them.
#
# experience_record shape (all keys optional/best-effort; nothing here is
# schema-enforced beyond "it's a dict" — matches this project's existing
# preference for plain dicts over a second dataclass hierarchy, e.g.
# job_ledger.files_touched, CycleRecord.health_snapshot):
#   incident_fingerprint: {symptom, error_signature, affected_organ, environment_context}
#   root_cause:           {explanation, confidence, evidence}
#   remediation:          {procedure, authority_required, affected_files_or_services}
#   validation_contract:  [exact checks that prove the repair]
#   regression_test:      path/name of an automated test, if one exists
#   prevention_rule:      how to stop the condition from recurring
#   watchpoint:            what signal indicates recurrence
#   outcome:               {success, performance, side_effects}
#   negative_knowledge:    [failed approaches that should not be repeated blindly]
#   temporal_validity:     {assumptions, stale_check}

def record_experience(
    proposal_id: str,
    *,
    incident_fingerprint: dict,
    root_cause: dict,
    remediation: dict,
    validation_contract: list | None = None,
    regression_test: str | None = None,
    prevention_rule: str | None = None,
    watchpoint: str | None = None,
    outcome: dict | None = None,
    negative_knowledge: list | None = None,
    temporal_validity: dict | None = None,
) -> Proposal:
    experience_record = {
        "incident_fingerprint": incident_fingerprint,
        "root_cause": root_cause,
        "remediation": remediation,
        "validation_contract": validation_contract or [],
        "regression_test": regression_test,
        "prevention_rule": prevention_rule,
        "watchpoint": watchpoint,
        "outcome": outcome or {},
        "negative_knowledge": negative_knowledge or [],
        "temporal_validity": temporal_validity or {},
    }
    summary = (f"[{incident_fingerprint.get('affected_organ', '?')}] {incident_fingerprint.get('symptom', '?')} "
               f"-> root cause: {root_cause.get('explanation', '?')} -> fix: {remediation.get('procedure', '?')}")
    p = load(proposal_id)
    p.lesson = summary
    p.history.append({"at": datetime.now(timezone.utc).isoformat(), "event": "experience_recorded"})
    save(p)
    with LESSONS_PATH.open("a") as f:
        f.write(json.dumps({
            "schema_version": 2, "proposal_id": proposal_id, "lesson": summary,
            "experience_record": experience_record, "at": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
    return p


def find_known_remediation(*, affected_organ: str, error_signature: str) -> dict | None:
    """KNOWN FAILURE NON-RECURRENCE LAW: before treating a failure as novel,
    check whether a validated experience_record already exists for a
    materially equivalent incident (same affected_organ + error_signature).
    Returns the MOST RECENT matching experience_record (as a plain dict, plus
    proposal_id/at), or None if nothing matches — this never fabricates a
    match. A caller that gets a match back should prefer applying its
    remediation over rediscovering one from scratch. If that known
    remediation is attempted again and still fails, the caller must record a
    NEW experience (via record_experience(), with temporal_validity noting
    the prior assumption broke) rather than silently looping or silently
    keeping the old lesson as if it were still valid — see temporal_validity."""
    if not LESSONS_PATH.exists():
        return None
    best = None
    for line in LESSONS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec = entry.get("experience_record")
        if not rec:
            continue
        fp = rec.get("incident_fingerprint", {})
        if fp.get("affected_organ") == affected_organ and fp.get("error_signature") == error_signature:
            best = {**rec, "proposal_id": entry.get("proposal_id"), "at": entry.get("at")}
    return best


RECURRENCE_SAME_ROOT_CAUSE = "SAME_ROOT_CAUSE"
RECURRENCE_SIMILAR_SYMPTOM = "SIMILAR_SYMPTOM_DIFFERENT_ROOT_CAUSE"
RECURRENCE_UNKNOWN = "UNKNOWN"


def classify_recurrence(*, affected_organ: str, error_signature: str) -> tuple[str, dict | None]:
    """KNOWN FAILURE NON-RECURRENCE LAW, made explicit (Founder-authorized
    2026-08-18): find_known_remediation() above is a binary found/not-found
    exact match. This distinguishes THREE outcomes, exactly as required —
    "NEVER blindly replay an old fix against changed code":

      SAME_ROOT_CAUSE — an exact (affected_organ, error_signature) match
        exists. A caller MAY treat the matched remediation as a real,
        applicable hint (still subject to full validation — this function
        never bypasses that), returned as the second tuple element.

      SIMILAR_SYMPTOM_DIFFERENT_ROOT_CAUSE — the SAME organ has recorded
        experience, but for a DIFFERENT error_signature. This is real,
        useful context (this organ has known failure modes) but the
        specific fix is NOT known to apply here — a caller must never
        reuse the remediation text, only note the organ's history.

      UNKNOWN — nothing on record for this organ at all. Genuinely novel.

    The remediation dict is returned ONLY for SAME_ROOT_CAUSE; both other
    outcomes return (label, None) — there is nothing safe to hand back."""
    exact = find_known_remediation(affected_organ=affected_organ, error_signature=error_signature)
    if exact is not None:
        return RECURRENCE_SAME_ROOT_CAUSE, exact

    if LESSONS_PATH.exists():
        for line in LESSONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = entry.get("experience_record")
            if not rec:
                continue
            if rec.get("incident_fingerprint", {}).get("affected_organ") == affected_organ:
                return RECURRENCE_SIMILAR_SYMPTOM, None

    return RECURRENCE_UNKNOWN, None
