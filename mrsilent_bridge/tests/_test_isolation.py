"""Shared test isolation harness (Founder-authorized 2026-08-18, Omni God
Mode Endgame Law #6 — TEST ISOLATION root-cause fix, not another manual
cleanup pass).

Root cause found via repeated live regression failures this session: every
tests/test_*.py file reimplemented its own check()/_cleanup() logic ad hoc,
so cleanup coverage was inconsistent — some test functions forgot a final
cleanup call, some intentionally left state behind as proof-of-behavior
with no later sweep, and an interrupted/crashed run had no guaranteed
teardown for anything that ran before the crash. The concrete failure this
caused: leaked ACTIVE/PAUSED campaigns and founder_request escalation
files silently changed the ambiguity outcome of an unrelated,
later-running test's substring-reference resolution — twice, in two
different rounds, "fixed" both times by a one-off manual cleanup rather
than a structural guarantee.

isolated_test_state() is a plain context manager (this project's tests are
standalone scripts, not pytest-collected — there is no fixture system to
hook into). Wrap the WHOLE sequence of test-function calls in a file's
`if __name__ == "__main__":` block in one `with isolated_test_state():`,
not each test function individually — that is enough to guarantee that
whatever any single test forgot, or whatever an exception left half-done,
gets torn down when the file finishes, without re-indenting every existing
test function body.

On exit (always — a real try/finally, so a genuine exception is cleaned up
too, not just the AssertionError-free check() failures this project
already tolerates):
  - abandons any campaign that is ACTIVE/PAUSED and did NOT exist before
    the block started (a campaign that already existed when the block
    began — e.g. leftover state from a still-broken earlier file in the
    same regression run — is left alone; this harness cleans up what ITS
    block created, not the whole system, so composing multiple test files
    in one regression sweep still works correctly),
  - deletes any founder_request escalation file that did NOT exist before
    the block started, UNLESS its real payload subject is one of the
    standing, durable, real requests this project must never delete
    (telegram_app_reconciliation) — preserved by reading its actual
    content, never guessed by filename or timing.
  - revokes any live_sensor_governance session that is still active and
    did NOT exist before the block started (Live Sensor Input Architecture
    + Governance milestone, 2026-08-18) — sessions already auto-expire on
    their own bound, but a crashed test mid-session should not leave an
    active governed sensor session sitting around until its timeout
    naturally elapses; this closes it immediately instead, same principle
    as the campaign/escalation guarantees above.
  - rejects any evolution.proposal.Proposal that did NOT exist before the
    block started and is still in a non-terminal status (Phase 13
    gap-closure round, 2026-08-19 — a REAL structural gap found via direct
    inspection: this harness's own coverage stopped one layer short.
    evolution/proposals/*.json is a real production ledger, driven by
    real observe_engine/campaign/mission code paths the same way
    campaigns and escalations are, but was never snapshotted/torn down
    here — found via 101 non-terminal proposal files accumulated across
    this session's test rounds, 78 of them literally self-labeled
    "synthetic test" in their own observed_weakness/finding text, none
    ever cleaned up by any per-file or suite-level mechanism. Proposals
    are NOT deleted (unlike escalation files) — they are REJECTED via the
    real evolution.proposal.advance() API, preserving the record as
    proposal.py's own lifecycle design intends (a proposal file is meant
    to be durable evolutionary history, not an ephemeral notification);
    this exactly matches the manual cleanup pattern already used
    repeatedly this session for dev-activity-echo proposals. A proposal
    already in a CLOSED_STATUSES terminal state (promoted/rolled_back/
    rejected) when the block ends is left untouched, and — like the
    campaign/escalation guarantees above — a proposal that already
    existed before the block started is never this block's to touch.
  - terminalizes any job_ledger record that did NOT exist before the
    block started and is still in a non-terminal state (2026-09-03 gap-
    closure round — the SAME structural gap the proposal fix above closed,
    one layer short again: job_ledger.py is a real, durable, production
    ledger the autonomous_cycle's own _startup_recovery() scans every
    cycle, but this harness never snapshotted/tore it down. Found via a
    live cycle observation that surfaced 19 leftover non-terminal fixture
    records from tests that call job_ledger.create()/checkpoint() directly
    without ever reaching a terminal state, each correctly but noisily
    escalated by the real recovery scanner as unrecoverable). Terminalized
    via job_ledger.checkpoint(..., JobState.FAILED, terminal_result=
    "test_teardown", error_class="test_fixture") — the same real, governed
    API every production caller uses, never raw ledger.json mutation. A
    job that already reached a terminal state on its own (e.g. this
    project's own _make_failed_job()-style fixtures) is left untouched,
    and — like every other guarantee above — a job that already existed
    before the block started is never this block's to touch.
    error_class="test_fixture" is intentionally NOT one of
    classify_ledger_failure_transience()'s recognized classes, so it
    always resolves to "AMBIGUOUS" (non-transient, fail-closed) — a
    teardown-terminalized fixture can never be mistaken for a genuine
    transient failure by the short-cadence continuation path.

This does not replace in-test assertions about state (e.g. "is still
PAUSED after a denial") — those still run and still matter. It only
guarantees final teardown regardless of how the test file exits.
"""
from __future__ import annotations

import contextlib
import json
from typing import Iterator

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# Real, standing, durable Founder requests that must never be deleted by
# this harness merely because they happen to postdate a test block's start
# (e.g. a test that itself legitimately creates/touches the real request).
# Exported (not underscore-prefixed) so other tests/*.py files that do
# their own escalations-directory cleanup (e.g. test_founder_conversation.
# py's _reset_pending_baseline(), found via this same audit to be
# unconditionally deleting EVERY escalation file, including this one, any
# time it happened to run while a real request was pending) can reuse the
# same single source of truth instead of re-deciding what's "safe to wipe".
PRESERVED_SUBJECTS = {"telegram_app_reconciliation"}
_PRESERVED_SUBJECTS = PRESERVED_SUBJECTS  # internal alias, kept for readability below


@contextlib.contextmanager
def isolated_test_state() -> Iterator[None]:
    import campaign as campaign_mod
    from evolution import founder_request
    import evolution.proposal as proposal_mod
    import live_sensor_governance as gov
    import job_ledger

    before_open_campaigns = {
        c.campaign_id for c in campaign_mod.list_all()
        if c.status in (campaign_mod.STATUS_ACTIVE, campaign_mod.STATUS_PAUSED)
    }
    before_escalations = {p.name for p in founder_request.ESCALATIONS_DIR.glob("*.json")}
    before_active_sessions = {s.session_id for s in gov.list_active_sessions()}
    before_proposal_ids = {p.stem for p in proposal_mod.PROPOSALS_DIR.glob("*.json")}
    before_job_ids = {r.job_id for r in job_ledger.list_all()}
    try:
        yield
    finally:
        for c in campaign_mod.list_all():
            if c.campaign_id in before_open_campaigns:
                continue  # pre-existing — not this block's to touch
            if c.status in (campaign_mod.STATUS_ACTIVE, campaign_mod.STATUS_PAUSED):
                campaign_mod.abandon(c.campaign_id, reason="isolated_test_state: automatic teardown")
        for p in sorted(founder_request.ESCALATIONS_DIR.glob("*.json")):
            if p.name in before_escalations:
                continue
            try:
                subject = json.loads(p.read_text()).get("payload", {}).get("subject", "")
            except (OSError, ValueError):
                subject = ""
            if subject in _PRESERVED_SUBJECTS:
                continue
            p.unlink(missing_ok=True)
        for s in gov.list_active_sessions():
            if s.session_id in before_active_sessions:
                continue  # pre-existing — not this block's to touch
            gov.revoke_session(s.session_id, requested_by="isolated_test_state", reason="automatic teardown")
        for pf in sorted(proposal_mod.PROPOSALS_DIR.glob("*.json")):
            if pf.stem in before_proposal_ids:
                continue  # pre-existing — not this block's to touch
            p = proposal_mod.load(pf.stem)
            if p is None or p.status in proposal_mod.CLOSED_STATUSES:
                continue  # already terminal (promoted/rolled_back/rejected) — nothing to tear down
            proposal_mod.advance(
                pf.stem, proposal_mod.ProposalStatus.REJECTED,
                note="isolated_test_state: automatic teardown — proposal created during a test run and left non-terminal",
            )
        terminal_values = {s.value for s in job_ledger.TERMINAL_STATES}
        for r in job_ledger.list_all():
            if r.job_id in before_job_ids:
                continue  # pre-existing — not this block's to touch
            if r.state in terminal_values:
                continue  # already terminal on its own — nothing to tear down
            job_ledger.checkpoint(
                r.job_id, job_ledger.JobState.FAILED,
                terminal_result="test_teardown", error_class="test_fixture",
                note="isolated_test_state: automatic teardown — job created during a test run and left non-terminal",
            )
