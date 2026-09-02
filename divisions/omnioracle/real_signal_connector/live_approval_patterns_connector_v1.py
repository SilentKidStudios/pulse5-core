#!/usr/bin/env python3
"""Live approval patterns connector -- SIG_APPROVAL_PATTERNS, wired live in the
RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION campaign (2026-09-02) after the
concurrent mr_silent_spine campaign's shared-scope collision was independently
re-verified as no longer current (see this campaign's final report).

Reads REAL, already-live job outcome state -- mrsilent_bridge/job_ledger.py's
list_all(), the SAME "CANONICAL_EVIDENCE_SOURCE" services/th3_mcp/tools.py already
documents itself as using (see tools.py's own module docstring). This connector is
READ-ONLY: job_ledger.list_all() is a pure read function, nothing here writes to
mrsilent_bridge. approval_state distribution across real job records (not_required /
pending_approval / granted / rejected / etc.) is the real, live "approvals, denials,
priority_patterns" signal the SIG_APPROVAL_PATTERNS blueprint asked for -- an older,
mostly-dormant dataset also exists under mr_silent_spine/approval_learning_engine_v1
(5 training events from 2026-05-22), but job_ledger is the fresher, actively-updated,
already-canonical source (6400+ real records as of this writing) and is used here
instead."""

import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _connector_common as cc

ROOT = Path("/opt/pulse5-core")
CONNECTOR_DIR = Path(__file__).resolve().parent / "live_ingestion"
SIGNAL_PREFIX = "SIG_APPROVAL_PATTERNS"


def _job_ledger_records():
    sys.path.insert(0, str(ROOT / "mrsilent_bridge"))
    import job_ledger
    return job_ledger.list_all()


def collect(force: bool = False) -> dict:
    """Read real job_ledger approval_state distribution. Fails closed to
    CONNECTOR_UNAVAILABLE if the ledger itself can't be read; never fabricates a
    distribution."""
    source_health = {}
    try:
        records = cc.read_metric(source_health, "job_ledger", _job_ledger_records)

        if not any(v == "ok" for v in source_health.values()):
            raise RuntimeError(f"all metrics failed: {source_health}")

        records = records or []
        approval_counts = dict(Counter(str(r.approval_state) for r in records))
        risk_class_counts = dict(Counter(str(r.risk_class) for r in records))

        total = len(records)
        denied_or_rejected = sum(v for k, v in approval_counts.items() if k.lower() in ("rejected", "denied"))
        pending = approval_counts.get("pending_approval", 0)
        denial_rate = round(denied_or_rejected / total, 4) if total else None
        pending_fraction = round(pending / total, 4) if total else None

        risk_level = "low"
        if denial_rate is not None and denial_rate > 0.30:
            risk_level = "high"
        elif denial_rate is not None and denial_rate > 0.10:
            risk_level = "medium"

        raw_payload = {
            "total_job_records": total,
            "approval_state_distribution": approval_counts,
            "risk_class_distribution": risk_class_counts,
            "denial_rate": denial_rate,
            "pending_approval_fraction": pending_fraction,
        }
        digest = cc.payload_digest(raw_payload)

        suppressed = cc.check_duplicate_throttle(CONNECTOR_DIR, SIGNAL_PREFIX, digest, force)
        if suppressed is not None:
            return suppressed

        signal = {
            "signal_id": cc.new_signal_id(SIGNAL_PREFIX),
            "source_type": "founder_behavior",
            "timestamp": cc.now_iso(),
            "confidence": 1.0,
            "importance": "high" if risk_level != "low" else "moderate",
            "risk_level": risk_level,
            "provenance": "live",
            "source_health": source_health,
            "payload_digest": digest,
            "raw_payload": raw_payload,
            "status": "OK",
        }
    except Exception as exc:
        signal = {
            "signal_id": cc.new_signal_id(f"{SIGNAL_PREFIX}_ERROR"),
            "source_type": "founder_behavior",
            "timestamp": cc.now_iso(),
            "confidence": None,
            "importance": None,
            "risk_level": None,
            "provenance": "live",
            "source_health": source_health,
            "raw_payload": None,
            "status": "CONNECTOR_UNAVAILABLE",
            "error": str(exc),
        }

    cc.write_signal(CONNECTOR_DIR, signal)
    return signal


def is_fresh(signal: dict, max_age_seconds: int = cc.STALE_AFTER_SECONDS) -> bool:
    return cc.is_fresh(signal, max_age_seconds)


def latest_signal(max_age_seconds: int = cc.STALE_AFTER_SECONDS) -> dict:
    return cc.latest_signal(CONNECTOR_DIR, SIGNAL_PREFIX, max_age_seconds)


if __name__ == "__main__":
    import json
    print(json.dumps(collect(), indent=2))
