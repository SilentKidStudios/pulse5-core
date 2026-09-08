"""Failure/Retry/Anomaly Discovery — Studio-wide Walk-Away, discovery
source #3 (2026-09-08).

Reads the REAL canonical observation stream (evolution/observations/,
written by evolution/observe.py's signal_*() functions — the same
observe -> propose -> advance pipeline evolution/advance.py already
drives) and classifies each observation into an evidence-based
operational category. Creates no new signal-generation, proposal, or
governance system — this module only reads observe.py's OBSERVATIONS_DIR
path constant and proposal.py's/job_ledger.py's existing public records.

evolution/observe.py is explicitly WITHHELD from modification by every
campaign this Studio has run. This module never imports or calls anything
from it beyond the OBSERVATIONS_DIR path constant, and never edits it.

Root cause this module compensates for at the classification layer, found
2026-09-08 while reviewing a live escalation (proposal
2c6ea3db-a5a1-4178-b3c0-881904106dab): observe.py's
signal_repeated_retries() already has a _NON_PRODUCTION_REQUESTERS
hardcoded allowlist meant to exclude test traffic from being flagged as a
production retry storm ("test", "test_recovery", "test_operator", ...) —
but it lists specific names rather than a pattern, so a new test function
name (here, test_media_failure_taxonomy) slips through uncaught and gets
escalated as if it were a real production defect. _is_test_origin() below
uses a broader, pattern-based check specifically so this classification
layer doesn't repeat that same narrow-allowlist mistake — it does not,
and cannot, fix observe.py's own filter (WITHHELD file); that remains a
named, deferred gap.

Root-cause-lineage dedup already exists upstream: every Observation
carries a dedupe_key (falls back to [signal_type] if unset — see
Observation.fingerprint() in observe.py) that evolution/advance.py's
find_on_cooldown() already uses to stop the SAME underlying signal from
repeatedly generating new proposals. This module does not reimplement
that; DUPLICATE_RETRY below identifies repeat *observations* of an
already-seen fingerprint within one discovery pass, purely for reporting/
counting — it does not gate proposal creation (observe.py already does).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import job_ledger
import test_origin_classifier as toc
from evolution import observe
from evolution import proposal as proposal_mod

STALE_AFTER_DAYS = 30


class Category(str, Enum):
    NEW_FAILURE = "new_failure"
    RETRYING = "retrying"
    RECOVERED = "recovered"
    TERMINAL_FAILURE = "terminal_failure"
    DUPLICATE_RETRY = "duplicate_retry"
    STALE_FAILURE = "stale_failure"
    ALREADY_RESOLVED = "already_resolved"
    NEEDS_REPAIR = "needs_repair"
    NEEDS_FOUNDER_GATE = "needs_founder_gate"
    TEST_ONLY_EVENT = "test_only_event"


def _is_test_origin(rec: dict[str, Any]) -> bool:
    """Delegates to the canonical test_origin_classifier (2026-09-08
    self-stewardship consolidation — see that module's docstring). This
    was previously its own duplicated pattern list, narrower than the
    canonical union (missing the "fake" marker that caused a real live
    leak elsewhere in this campaign)."""
    evidence = rec.get("evidence", {}) or {}
    parts = [str(v) for v in evidence.values() if isinstance(v, (str, int, float))]
    parts.append(str(rec.get("description", "")))
    return toc.is_test_or_synthetic_origin(
        requested_by=evidence.get("requested_by"),
        text_blob=" ".join(parts),
    )


def _age_days(rec: dict[str, Any]) -> int | None:
    created = rec.get("created_at")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _fingerprint(rec: dict[str, Any]) -> str:
    key = rec.get("dedupe_key") or [rec.get("signal_type")]
    return "|".join(str(x) for x in key)


def _job_id_from_evidence(rec: dict[str, Any]) -> str | None:
    evidence = rec.get("evidence", {}) or {}
    for k in ("job_id", "adapter_job_id"):
        if evidence.get(k):
            return str(evidence[k])
    return None


@dataclass
class Classification:
    observation_id: str
    category: Category
    reason: str
    signal_type: str | None
    linked_proposal_id: str | None
    age_days: int | None
    fingerprint: str


def classify_observation(rec: dict[str, Any], *, seen_fingerprints: set[str]) -> Classification:
    """Deterministic, evidence-based classification of ONE observation.
    Never fabricates a category. Checked in order of strongest evidence
    first: test-origin is checked before anything else so a test fixture
    is never treated as a real production failure regardless of what a
    linked proposal or job says."""
    obs_id = rec.get("observation_id", "")
    signal_type = rec.get("signal_type")
    linked_proposal_id = rec.get("linked_proposal_id")
    age = _age_days(rec)
    fp = _fingerprint(rec)

    if _is_test_origin(rec):
        c = Category.TEST_ONLY_EVENT
        reason = "matches test-origin pattern in evidence/description"
        seen_fingerprints.add(fp)
        return Classification(obs_id, c, reason, signal_type, linked_proposal_id, age, fp)

    is_repeat_fingerprint = fp in seen_fingerprints
    seen_fingerprints.add(fp)

    if linked_proposal_id:
        try:
            prop = proposal_mod.load(linked_proposal_id)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            prop = None
        if prop is not None:
            if prop.status == proposal_mod.ProposalStatus.PROMOTED:
                return Classification(obs_id, Category.ALREADY_RESOLVED,
                                       f"linked proposal {linked_proposal_id} already promoted",
                                       signal_type, linked_proposal_id, age, fp)
            if prop.status in (proposal_mod.ProposalStatus.REJECTED, proposal_mod.ProposalStatus.ROLLED_BACK):
                return Classification(obs_id, Category.TERMINAL_FAILURE,
                                       f"linked proposal {linked_proposal_id} ended {prop.status} — not repaired",
                                       signal_type, linked_proposal_id, age, fp)
            if prop.status == proposal_mod.ProposalStatus.PROMOTION_CANDIDATE:
                return Classification(obs_id, Category.NEEDS_FOUNDER_GATE,
                                       f"linked proposal {linked_proposal_id} is a promotion_candidate awaiting "
                                       "Founder-directed promotion",
                                       signal_type, linked_proposal_id, age, fp)
            if prop.status == proposal_mod.ProposalStatus.DEFERRED:
                return Classification(obs_id, Category.NEEDS_REPAIR,
                                       f"linked proposal {linked_proposal_id} deferred, not abandoned",
                                       signal_type, linked_proposal_id, age, fp)
            # observed/proposed/implemented/tested/canary — still actively progressing
            return Classification(obs_id, Category.RETRYING,
                                   f"linked proposal {linked_proposal_id} in progress (status={prop.status})",
                                   signal_type, linked_proposal_id, age, fp)

    job_id = _job_id_from_evidence(rec)
    if job_id:
        ledger_record = job_ledger.load(job_id)
        if ledger_record is not None:
            if ledger_record.state == job_ledger.JobState.COMPLETED:
                return Classification(obs_id, Category.RECOVERED,
                                       f"job {job_id} completed — underlying issue resolved",
                                       signal_type, linked_proposal_id, age, fp)
            if ledger_record.state in job_ledger.TERMINAL_STATES:
                return Classification(obs_id, Category.TERMINAL_FAILURE,
                                       f"job {job_id} in terminal non-success state {ledger_record.state}",
                                       signal_type, linked_proposal_id, age, fp)
            return Classification(obs_id, Category.RETRYING,
                                   f"job {job_id} still active (state={ledger_record.state})",
                                   signal_type, linked_proposal_id, age, fp)

    if is_repeat_fingerprint:
        return Classification(obs_id, Category.DUPLICATE_RETRY,
                               f"same root-cause fingerprint ({fp}) already seen this discovery pass",
                               signal_type, linked_proposal_id, age, fp)

    if age is not None and age > STALE_AFTER_DAYS:
        return Classification(obs_id, Category.STALE_FAILURE,
                               f"no linked proposal/job and older than {STALE_AFTER_DAYS} days",
                               signal_type, linked_proposal_id, age, fp)

    return Classification(obs_id, Category.NEW_FAILURE,
                           "no linked proposal or job yet — unaddressed",
                           signal_type, linked_proposal_id, age, fp)


_FILENAME_TS_SAFETY_MARGIN_S = 60  # filename batch timestamp is always <= a record's own
# created_at, empirically within ~1s in every real sample checked (2026-09-08) — this margin is
# generous on top of that, so the pre-filter below can only ever skip reading a file too
# CONSERVATIVELY (process one it didn't strictly need to), never wrongly skip one it should have.

# Deliberately narrow: ONLY matches the real Observation-record naming convention
# ('YYYYMMDDTHHMMSSZ_obs-N-hash.json', from observe.py's own writer) — never a bare
# '..._report.json' snapshot file also found sharing this directory. Verified live (2026-09-08):
# those report files carry created_at=None, and classify_all()'s own `since` check only ever
# skips a record when created_dt IS determinable — a record with no created_at is ALWAYS included
# regardless of `since`, by original, unchanged design. A filename-only pre-filter cannot know a
# file's created_at without opening it, so it must never skip anything outside this exact pattern
# — confirmed by a real snapshot-identical A/B comparison against the pre-optimization algorithm
# that caught this exact gap before it shipped.
_OBS_FILENAME_TS_RE = re.compile(r"^(\d{8}T\d{6})Z_obs-")


def _filename_ts_before(p: Path, cutoff: datetime) -> bool | None:
    """True if the filename's own embedded batch timestamp is confidently
    before `cutoff` (safe to skip without opening the file); False if it's
    at or after; None if the filename doesn't match the real-observation
    naming pattern (falls back to opening the file, never guessed)."""
    m = _OBS_FILENAME_TS_RE.match(p.name)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return ts < (cutoff - timedelta(seconds=_FILENAME_TS_SAFETY_MARGIN_S))


def classify_all(*, since: datetime | None = None) -> list[Classification]:
    """Classifies observations, oldest first (so DUPLICATE_RETRY correctly
    marks the LATER occurrences of a repeated fingerprint, not the first).
    `since` bounds the scan to observations created at or after that time —
    use it for periodic incremental discovery instead of rescanning the
    full multi-hundred-thousand-file history every cycle; omit it only for
    a full historical audit.

    PRE-FILTER FIX (2026-09-08): `since` used to only filter the RESULT —
    every one of the (real, live) 145,715 observation files still got
    opened, parsed, and classified regardless, making a 'bounded' 1-day
    query cost ~50s, dominated entirely by file I/O/JSON-parsing on
    ~137,000 files that were always going to be discarded. Observation
    filenames embed their own write-batch timestamp
    ('YYYYMMDDTHHMMSSZ_obs-N-hash.json', confirmed via observe.py's own
    naming convention), which sorts identically to created_at and lets a
    too-old file be skipped via a filename-string comparison alone — no
    open, no parse. This changes WHICH FILES GET OPENED, never which
    records get CLASSIFIED: the original code already applied the exact
    same `created_dt < since` skip (after parsing) and already fed `seen`
    only from records that passed it — this pre-filter reproduces that
    same skip decision earlier and cheaper, with a 60s safety margin so it
    can only ever process a file it didn't strictly need to, never skip
    one it should have read (see _filename_ts_before())."""
    if not observe.OBSERVATIONS_DIR.exists():
        return []
    seen: set[str] = set()
    out: list[Classification] = []
    for p in sorted(observe.OBSERVATIONS_DIR.glob("*.json")):
        if since is not None:
            confidently_old = _filename_ts_before(p, since)
            if confidently_old:
                continue
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if since is not None:
            created = rec.get("created_at")
            try:
                created_dt = datetime.fromisoformat(created) if created else None
            except (ValueError, TypeError):
                created_dt = None
            if created_dt is not None:
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if created_dt < since:
                    continue
        out.append(classify_observation(rec, seen_fingerprints=seen))
    return out


@dataclass
class AnomalyReport:
    total: int
    test_only: int
    new_failure: int
    retrying: int
    recovered: int
    terminal_failure: int
    duplicate_retry: int
    stale_failure: int
    already_resolved: int
    needs_repair: int
    needs_founder_gate: int


def anomaly_report(classifications: list[Classification] | None = None, *, since: datetime | None = None) -> AnomalyReport:
    classifications = classify_all(since=since) if classifications is None else classifications
    counts = Counter(c.category for c in classifications)
    return AnomalyReport(
        total=len(classifications),
        test_only=counts[Category.TEST_ONLY_EVENT],
        new_failure=counts[Category.NEW_FAILURE],
        retrying=counts[Category.RETRYING],
        recovered=counts[Category.RECOVERED],
        terminal_failure=counts[Category.TERMINAL_FAILURE],
        duplicate_retry=counts[Category.DUPLICATE_RETRY],
        stale_failure=counts[Category.STALE_FAILURE],
        already_resolved=counts[Category.ALREADY_RESOLVED],
        needs_repair=counts[Category.NEEDS_REPAIR],
        needs_founder_gate=counts[Category.NEEDS_FOUNDER_GATE],
    )


def natural_language_summary(*, since: datetime | None = None) -> str:
    r = anomaly_report(since=since)
    parts = [
        f"Of {r.total} observed anomalies, {r.test_only} are test-only events, not real production issues."
    ]
    active = r.new_failure + r.retrying
    if active:
        parts.append(f"{active} are real and still active ({r.new_failure} newly seen, {r.retrying} already being worked).")
    if r.needs_founder_gate:
        parts.append(f"{r.needs_founder_gate} have a fix ready but need your promotion approval.")
    if r.terminal_failure:
        parts.append(f"{r.terminal_failure} failed permanently and were never repaired.")
    if r.recovered or r.already_resolved:
        parts.append(f"{r.recovered + r.already_resolved} were already resolved.")
    return " ".join(parts)
