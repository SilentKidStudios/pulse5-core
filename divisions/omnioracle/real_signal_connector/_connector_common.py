"""Shared helpers for real_signal_connector's live connectors -- factored out after
live_system_runtime_connector_v1.py established the pattern (per-metric source
health, time-windowed duplicate suppression, explicit freshness gate, real
provenance). live_system_runtime_connector_v1.py itself is left using its own
inline copy (already tested and live-wired) rather than risk regressing it by
refactoring; SIG_STUDIO_ACTIVITY and SIG_APPROVAL_PATTERNS use this shared version."""
from __future__ import annotations

import json
import hashlib
import uuid
from pathlib import Path
from datetime import datetime, timezone

STALE_AFTER_SECONDS = 900  # 15 min, matching omnioracle-continuous-daemon.timer's cadence
DUPLICATE_SUPPRESSION_WINDOW_SECONDS = 30

REQUIRED_NORMALIZED_FIELDS = ["signal_id", "source_type", "timestamp", "confidence", "importance", "risk_level", "raw_payload"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_metric(source_health: dict, key: str, fn):
    """Read one metric; record its own success/failure in source_health rather than
    letting one failing metric silently blank the whole signal."""
    try:
        value = fn()
        source_health[key] = "ok"
        return value
    except Exception as exc:
        source_health[key] = f"unavailable: {exc}"
        return None


def payload_digest(raw_payload: dict) -> str:
    return hashlib.sha256(json.dumps(raw_payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def is_fresh(signal: dict | None, max_age_seconds: int = STALE_AFTER_SECONDS) -> bool:
    if not signal or signal.get("status") != "OK":
        return False
    try:
        ts = datetime.fromisoformat(signal["timestamp"])
    except Exception:
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_seconds


def latest_signal(connector_dir: Path, signal_prefix: str, max_age_seconds: int = STALE_AFTER_SECONDS) -> dict:
    files = sorted(connector_dir.glob(f"{signal_prefix}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"status": "NO_SIGNAL_COLLECTED_YET", "provenance": "live"}

    latest = json.loads(files[0].read_text())
    try:
        ts = datetime.fromisoformat(latest["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        age = None

    if age is not None and age > max_age_seconds:
        latest = {**latest, "status": "STALE", "age_seconds": age}
    elif age is not None:
        latest = {**latest, "age_seconds": age}
    return latest


def new_signal_id(prefix: str) -> str:
    # Microsecond epoch alone is NOT collision-proof: two calls in the same tight
    # loop can land on the identical microsecond (observed in testing, ~10% of a
    # 50-call burst). A uuid4 suffix guarantees uniqueness regardless of clock
    # resolution or call rate; the timestamp prefix is kept for human-readable
    # ordering/debugging.
    ts = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


def write_signal(connector_dir: Path, signal: dict) -> None:
    connector_dir.mkdir(parents=True, exist_ok=True)
    (connector_dir / f"{signal['signal_id']}.json").write_text(json.dumps(signal, indent=2))


def check_duplicate_throttle(connector_dir: Path, signal_prefix: str, digest: str, force: bool):
    """Returns a duplicate-suppressed signal dict to return immediately, or None to
    proceed with writing a fresh one. Time-windowed (not exact-digest-match, mirroring
    live_system_runtime_connector_v1.py's own fix for this: underlying counts can
    legitimately be identical between reads a few seconds apart without that meaning
    'nothing changed since the last collection')."""
    if force:
        return None
    existing = latest_signal(connector_dir, signal_prefix, max_age_seconds=DUPLICATE_SUPPRESSION_WINDOW_SECONDS)
    if existing.get("status") == "OK":
        return {
            **existing,
            "duplicate_suppressed": True,
            "content_drifted_since_suppressed_reading": existing.get("payload_digest") != digest,
        }
    return None
