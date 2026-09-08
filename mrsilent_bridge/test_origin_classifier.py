"""Canonical Test/Synthetic-Origin Classifier — Studio-wide Walk-Away,
self-stewardship consolidation (2026-09-08).

Real self-improvement finding, not fabricated: this campaign independently
built test/synthetic-origin detection THREE times (founder_view_filter.py,
2026-08-20, pre-existing; approval_backlog_hygiene.py, this campaign;
failure_anomaly_discovery.py and validation_canary_selfheal_discovery.py,
this campaign), each maintaining its own separate, incomplete keyword
list. This caused two REAL, live leaks that had to be independently
discovered and fixed:
  1. escalation 8d7079a8d795871b/7900750e05499885 (campaign 34c0fbe0,
     "synthetic end-to-end test") reaching the real Telegram channel —
     founder_view_filter.py's own list caught it; the Telegram sender
     simply never consulted that filter (fixed separately).
  2. escalation c634ab2d33d97a70 (job_id="fake-canary-job") slipped past
     approval_backlog_hygiene.py's ORIGINAL job_id.startswith("test-")
     check because this codebase's OWN naming convention also uses a
     "fake-" marker (confirmed by the pre-existing "test-fake-omni_
     engineer-ok-..." sample) — a fix applied to ONE of three duplicated
     lists, leaving the other two (failure_anomaly_discovery.py,
     validation_canary_selfheal_discovery.py) still narrower.

This module is the single, canonical, most-complete union of every marker
verified real across this campaign's own investigation — consolidating,
not replacing, each consuming module's specific control flow. Future
markers need adding in exactly ONE place.
"""
from __future__ import annotations

import re

# Bounded-token match: "test"/"tests" as a delimited token (test_x, x_test,
# x:test, exactly "test") so it never false-positives on "latest"/
# "contest"/"fastest". Verified against real production data this
# campaign (device_client, mrsilent_app, founder, live_sensor:device_client
# never match; test_media_failure_taxonomy, smoke_test, acceptance_test,
# live_sensor:test, test-fake-omni_engineer-ok-... all match).
_TEST_TOKEN_RE = re.compile(r"(?:^|[_\-:.\s])tests?(?:$|[_\-:.\s])", re.IGNORECASE)

# Substring markers — each individually verified against a REAL leaked
# record this campaign found, not speculative:
#   "synthetic"  -> escalation payload finding text, campaign 34c0fbe0
#   "fixture"    -> defensive, same family as "synthetic"
#   "fake"       -> escalation c634ab2d33d97a70's job_id "fake-canary-job"
_SUBSTRING_MARKERS = ("synthetic", "fixture", "fake")

_EXACT_REQUESTED_BY_MARKERS = frozenset({"test", "acceptance_test"})
_EXACT_CAPABILITY_MARKERS = frozenset({"test"})


def is_test_or_synthetic_origin(
    *,
    requested_by: str | None = None,
    capability_needed: str | None = None,
    job_id: str | None = None,
    text_blob: str | None = None,
) -> bool:
    """True if ANY field carries a verified test/synthetic marker. Every
    argument is optional so a caller only passes the fields its own record
    shape actually has (escalations vs. observations vs. job_ledger
    records differ) — never forces a schema on a caller."""
    requested_by_s = str(requested_by or "")
    if requested_by_s in _EXACT_REQUESTED_BY_MARKERS:
        return True
    if str(capability_needed or "") in _EXACT_CAPABILITY_MARKERS:
        return True

    blob = " ".join(str(x) for x in (requested_by, job_id, text_blob) if x).lower()
    if _TEST_TOKEN_RE.search(blob):
        return True
    return any(marker in blob for marker in _SUBSTRING_MARKERS)
