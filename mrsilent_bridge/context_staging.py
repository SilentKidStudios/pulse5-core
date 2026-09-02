"""
Shared canonical-source context-staging filter -- GOVERNED CANONICAL SOURCE
STAGING REPAIR (Founder-authorized).

Extracted from omniengineer_harness.py (where this was first built, for the
decomposed Omni path) so that EVERY engine capable of receiving GOVERNED
source_paths -- omniengineer_harness.py's submit_job()/submit_job_decomposed()
AND bridge.py's submit_job() (Claude Code) -- reuses the exact same
exclusion list and logic, rather than each maintaining its own,
independently-driftable copy.

Doctrine, unchanged from its original location: authority_policy.py's
GATED_PATH_MARKERS already blocks a job from ever reaching credential/
protected paths at all (FOUNDER_GATED, a hard authority-level rejection of
the WHOLE job). This module is a SEPARATE, complementary hygiene filter --
even an authority-safe path can be noisy, historical, or irrelevant context
that should never be silently staged into a worker's sandbox just because a
caller's source_paths list happened to include it. Default-exclude; live
canonical source wins over historical copies.
"""
from __future__ import annotations

from pathlib import Path

import secret_path_policy

CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS = (
    "manual_candidates",
    "continuity_backup",
    "/backups/",
    "_backup/",
    "archived_jobs",
    "archive/",  # generic archive trees, distinct from archived_jobs above
    "founder_receipts",
    "content_packet",
    "/logs/",
    ".log",
    "/jobs/",  # another job's own historical workdir tree
    # Defense-in-depth secret/key exclusion. authority_policy.GATED_PATH_MARKERS
    # already REJECTS the whole job outright (a stronger guarantee) if a
    # source_path touches one of these -- this list ensures the STAGING
    # layer itself never copies such a path either, in case a future caller
    # reaches this filter through a path that does not also run classify()
    # first.
    "secrets",
    "credentials",
    ".env",
    "/.ssh",
    "/.aws",
    "/.config/gcloud",
    # Extended 2026-09-02 (SECRET / CREDENTIAL SOURCE-STAGING HARDENING
    # campaign) -- see secret_path_policy.py's docstring for the read-only
    # audit these came from. Shared with authority_policy.GATED_PATH_MARKERS
    # so the two layers never drift apart on what counts as secret-bearing.
) + secret_path_policy.NEW_CONFIRMED_SECRET_MARKERS


def stage_context_source_paths(source_paths: list[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    """Splits caller-provided source_paths into (allowed, excluded) per the
    default-exclusion markers above, PLUS an explicit ROOT-boundary check
    (2026-09-02 hardening) and traversal-safe resolution -- both checks run
    against secret_path_policy.resolve_path(p), not the raw caller string, so
    '..' segments or a symlink alias (e.g. .env.central -> /etc/pulse5.env)
    can't hide a marker or escape the repository. The RETURNED `allowed`
    entries are the caller's original (unresolved) Path objects, unchanged --
    only the exclusion DECISION uses the resolved form. Returns the excluded
    list with its matched marker/reason for durable, honest recording --
    callers never silently lose a path without a reason on record."""
    allowed: list[Path] = []
    excluded: list[dict[str, str]] = []
    for p in source_paths:
        p_str = str(p)
        resolved = secret_path_policy.resolve_path(p)

        if not secret_path_policy.is_within_root(resolved):
            excluded.append({"path": p_str, "excluded_marker": "outside_canonical_root"})
            continue

        marker = secret_path_policy.matches_secret_marker(resolved, CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS)
        if marker:
            excluded.append({"path": p_str, "excluded_marker": marker})
        else:
            allowed.append(p)
    return allowed, excluded
