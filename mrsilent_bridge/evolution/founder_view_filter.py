"""Normal-vs-History/Test classification for the Founder-facing Approvals
and Projects views (App/Command Orchestration milestone, Phase 7 real-
device UX repair, Founder-authorized 2026-08-20).

Real S25 Ultra device finding: the federated Approvals view surfaced 52
pending items and the Projects view surfaced many "synthetic ... test"
campaigns -- almost entirely accumulated regression/acceptance-test
artifacts from this project's own testing, not genuine Founder work. This
module NEVER deletes, denies, or mutates anything -- it is a pure,
read-time classifier over the SAME real state every other view already
reads, reused (not duplicated) by both the Approvals and Projects
endpoints. Classification reuses the EXACT evidence-based logic already
proven correct in this milestone's own live escalation cleanup (checking
the real underlying proposal/campaign text and requested_by, never a
guess) -- never invented fresh here.

Pulse items are deliberately NEVER classified as test/synthetic by this
module: this codebase has zero visibility into whether a given Pulse
FOUNDER_SIGNAL/gap/rg reference is a test artifact without fetching real
detail per item (expensive, and unverified), and the Founder explicitly
required "do not guess" / "do not mutate unknown real Founder decisions".
Every real Pulse item is always REAL_ACTIONABLE_FOUNDER_DECISION here.
"""
from __future__ import annotations

from typing import Any

CATEGORY_REAL_ACTIONABLE = "REAL_ACTIONABLE_FOUNDER_DECISION"
CATEGORY_TEST_ARTIFACT = "TEST_ARTIFACT"
CATEGORY_SYNTHETIC_ACCEPTANCE = "SYNTHETIC_ACCEPTANCE_ARTIFACT"
CATEGORY_INTERNAL_DIAGNOSTIC = "INTERNAL_DIAGNOSTIC"
CATEGORY_OTHER = "OTHER"

_TEST_MARKERS = ("synthetic", "test-", "test_", "-test", "regression-test", "acceptance-test", "acceptance test")


def classify_pending_item(item: dict[str, Any]) -> str:
    """Real-evidence classification for one combined_pending() item.
    Render items are checked against their own real underlying proposal
    text (same logic proven correct resolving 31 real test artifacts
    earlier this milestone); Pulse items are never guessed at."""
    if item.get("source_authority") == "pulse":
        return CATEGORY_REAL_ACTIONABLE

    payload = item.get("payload") or {}
    subject = (payload.get("subject") or "")
    finding = (payload.get("finding") or "")
    if subject.startswith("stuck job_ledger record"):
        return CATEGORY_INTERNAL_DIAGNOSTIC

    blob = subject.lower() + " " + finding.lower()
    if any(m in blob for m in _TEST_MARKERS) or item.get("requested_by") == "test":
        return CATEGORY_TEST_ARTIFACT

    # pulse_escalation items (Founder Notification Delivery Certification,
    # 2026-09-03) carry a proposal_id from PULSE's own evolution/proposals/
    # store, not Render's local one -- proposal_mod.load() here is always
    # this HOST's local store, so it can never resolve a Pulse-owned id
    # (confirmed live: FileNotFoundError crashed this whole endpoint for
    # every caller, not just pulse_escalation items, the first time this
    # ran against the real merged list). Skipped entirely for this source
    # rather than papered over with a broad except, since it is a
    # structural cross-host mismatch, not an occasional missing file --
    # same conservative "never guess" treatment classify_pending_item()
    # already gives plain "pulse" items above.
    proposal_id = (payload.get("affected") or {}).get("proposal_id")
    if proposal_id and item.get("source_authority") != "pulse_escalation":
        from evolution import proposal as proposal_mod
        try:
            p = proposal_mod.load(proposal_id)
        except FileNotFoundError:
            p = None
        if p is not None:
            pblob = (p.observed_weakness or "").lower() + " " + (p.proposed_upgrade or "").lower() + " " + (p.fingerprint or "").lower()
            if any(m in pblob for m in _TEST_MARKERS):
                return CATEGORY_SYNTHETIC_ACCEPTANCE

    return CATEGORY_REAL_ACTIONABLE


def split_normal_and_history(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Splits a combined_pending()-shaped list into (normal, history) --
    never drops anything; every item appears in exactly one bucket, with
    its real classification attached so a caller/UI can still show the
    full picture on request."""
    normal, history = [], []
    for item in items:
        category = classify_pending_item(item)
        tagged = dict(item)
        tagged["_view_category"] = category
        if category == CATEGORY_REAL_ACTIONABLE:
            normal.append(tagged)
        else:
            history.append(tagged)
    return normal, history


# Real evidence found classifying 1066 real accumulated campaigns
# (2026-08-20): keyword matching alone under-catches for campaigns --
# engineering objectives are free-form task descriptions where "test" is
# ordinary vocabulary ("...and a matching unit test"), unlike an
# escalation's more formulaic subject/finding text. The strongest real,
# non-guessed signal: a genuine one-off Founder request is never
# submitted dozens of times verbatim. Objectives repeated more than
# _DUPLICATE_THRESHOLD times across ALL campaigns are near-certainly a
# programmatic test-suite fixture (confirmed live: "Write a small Python
# function that computes a value and a matching unit test in a sandbox."
# appeared 26 times, none containing any keyword marker).
_DUPLICATE_THRESHOLD = 3


def classify_campaign(objective: str, original_founder_request: str | None, *, objective_frequency: int = 1) -> str:
    """Real-evidence approach for Projects/campaigns: keyword markers in
    the real objective/original_founder_request text, PLUS real duplicate-
    frequency evidence across all campaigns -- never a guess."""
    blob = (objective or "").lower() + " " + (original_founder_request or "").lower()
    if any(m in blob for m in _TEST_MARKERS):
        return CATEGORY_TEST_ARTIFACT
    if objective_frequency > _DUPLICATE_THRESHOLD:
        return CATEGORY_SYNTHETIC_ACCEPTANCE
    return CATEGORY_REAL_ACTIONABLE


def split_campaigns_normal_and_history(campaigns: list[Any]) -> tuple[list[Any], list[Any]]:
    """Splits a list of real campaign.Campaign objects into (normal,
    history) using real duplicate-frequency evidence computed across the
    WHOLE given list -- never deletes or mutates anything."""
    from collections import Counter
    freq = Counter(c.objective for c in campaigns)
    normal, history = [], []
    for c in campaigns:
        category = classify_campaign(c.objective, getattr(c, "original_founder_request", None), objective_frequency=freq[c.objective])
        if category == CATEGORY_REAL_ACTIONABLE:
            normal.append(c)
        else:
            history.append(c)
    return normal, history
