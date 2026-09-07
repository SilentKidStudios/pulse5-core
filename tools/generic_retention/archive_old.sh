#!/bin/bash
# Generic additive retention fix (Omni/God Mode GPU-elasticity storage
# remediation, 2026-08-21): several independent Studio subsystems write
# per-event result/receipt files with zero retention, growing root disk
# unbounded (same root cause independently confirmed in: syslog,
# studio-inventory diagnostics, omniforge/auto_logs, dependency_governance
# receipts, studios_autonomous_evolution/cycles). This moves (NEVER
# deletes) items older than the given age to the data volume archive,
# preserving full history while keeping root lean.
SRC="$1"
DEST="$2"
AGE="${3:-30 days ago}"
# /usr/bin/find on this host is bfs, whose -newermt requires an ISO-8601
# timestamp and rejects GNU find's relative date phrases (e.g. "14 days
# ago"), so AGE must be resolved to an absolute timestamp before use.
CUTOFF="$(date -d "$AGE" --iso-8601=seconds)" || exit 1
mkdir -p "$DEST"
find "$SRC" -maxdepth 1 -mindepth 1 ! -newermt "$CUTOFF" -exec mv -t "$DEST" {} + 2>/dev/null
