#!/usr/bin/env python3
"""Live studio activity connector -- SIG_STUDIO_ACTIVITY, wired live in the
RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION campaign (2026-09-02) after the
concurrent mr_silent_spine campaign's shared-scope collision was independently
re-verified as no longer current (see this campaign's final report).

Reads REAL, already-live studio infrastructure this project already runs --
mr_silent_spine/division_signal_bus/{inbox,receipts}/ -- the existing, already-live
division-signal-bus pipeline (consumed every ~30s by
mrsilent-division-bus-consumer.service; see services/th3_mcp/tools.py's own
council_post_result docstring for the same canonical source). This connector is
READ-ONLY against mr_silent_spine: it never writes there, only reads real file
counts/content and writes its own output under
divisions/omnioracle/real_signal_connector/live_ingestion/, exactly like
live_system_runtime_connector_v1.py.

queue_growth = inbox depth (real pending-signal count); division_health = how many
distinct source_division values show up in recent receipts (a division that stops
posting silently drops out of this count, which is itself a real, honest signal, not
inferred); worker_output = recent receipts count as a throughput proxy."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _connector_common as cc

ROOT = Path("/opt/pulse5-core")
BUS_INBOX = ROOT / "mr_silent_spine/division_signal_bus/inbox"
BUS_RECEIPTS = ROOT / "mr_silent_spine/division_signal_bus/receipts"

CONNECTOR_DIR = Path(__file__).resolve().parent / "live_ingestion"
SIGNAL_PREFIX = "SIG_STUDIO_ACTIVITY"

RECENT_RECEIPTS_SAMPLE = 200  # bounded, most-recent-N by mtime -- never scans unboundedly


def _recent_receipts():
    import json
    if not BUS_RECEIPTS.exists():
        return []
    files = sorted(BUS_RECEIPTS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT_RECEIPTS_SAMPLE]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def collect(force: bool = False) -> dict:
    """Read real division_signal_bus state. Fails closed to CONNECTOR_UNAVAILABLE
    only if every metric fails; per-metric failure is recorded in source_health."""
    source_health = {}
    try:
        inbox_count = cc.read_metric(source_health, "inbox", lambda: len(list(BUS_INBOX.glob("*.json"))) if BUS_INBOX.exists() else 0)
        receipts = cc.read_metric(source_health, "receipts", _recent_receipts)
        receipts = receipts or []

        divisions_seen = sorted({r.get("source_division") for r in receipts if r.get("source_division")})

        if not any(v == "ok" for v in source_health.values()):
            raise RuntimeError(f"all metrics failed: {source_health}")

        risk_level = "low"
        if inbox_count is not None and inbox_count > 200:
            risk_level = "high"
        elif inbox_count is not None and inbox_count > 50:
            risk_level = "medium"

        raw_payload = {
            "queue_growth_inbox_depth": inbox_count,
            "worker_output_recent_receipts_sampled": len(receipts),
            "division_health_distinct_divisions_posting": len(divisions_seen),
            "divisions_seen": divisions_seen,
        }
        digest = cc.payload_digest(raw_payload)

        suppressed = cc.check_duplicate_throttle(CONNECTOR_DIR, SIGNAL_PREFIX, digest, force)
        if suppressed is not None:
            return suppressed

        signal = {
            "signal_id": cc.new_signal_id(SIGNAL_PREFIX),
            "source_type": "studio_internal_activity",
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
            "source_type": "studio_internal_activity",
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
