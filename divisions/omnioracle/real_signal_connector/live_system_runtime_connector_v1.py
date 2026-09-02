#!/usr/bin/env python3
"""Live system runtime connector -- the ONE real_signal_connector connector actually
flipped to live_collection_enabled=True in the OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_
LIVE-SIGNAL_CLOSURE campaign (2026-09-02), hardened in the subsequent
INDEPENDENT_GAP_CLOSURE pass (same day) with per-metric source health, duplicate-
event suppression, and an explicit reusable freshness gate.

Of the 5 blueprint connectors in omnioracle_real_signal_connector_v1.py
(SIG_SOCIAL_TRENDS, SIG_SYSTEM_RUNTIME, SIG_APPROVAL_PATTERNS, SIG_MARKET_INTEL,
SIG_STUDIO_ACTIVITY), only SIG_SYSTEM_RUNTIME can be made genuinely live in this
session without new credentials, Founder secrets, paid activation, or touching any
file owned by the concurrently active Work 10.7 session: local machine telemetry
(load average, memory, disk, and the health of this division's own systemd units)
requires no external account and no protected state.

The other 4 remain blueprint_ready/live_collection_enabled=False -- SIG_SOCIAL_TRENDS
and SIG_MARKET_INTEL need real external API credentials (paid_resources_allowed=false
blocks this outright without Founder authorization); SIG_APPROVAL_PATTERNS and
SIG_STUDIO_ACTIVITY would need reading mr_silent_spine internal state -- explicitly
deferred while Work 10.7 is active there (SIG_STUDIO_ACTIVITY=
DEFERRED_ACTIVE_SESSION_COLLISION, SIG_APPROVAL_PATTERNS=
DEFERRED_ACTIVE_SESSION_COLLISION), not merely "out of scope".

Signal values here are REAL measurements, not evidence-derived heuristics -- so
confidence=1.0 (it is ground truth about this machine, not a projection) and
provenance="live", the only place in this division that field is ever anything but
"synthetic". Freshness is explicit: is_fresh()/latest_signal() report a reading older
than STALE_AFTER_SECONDS as status="STALE" rather than silently reusing it as current.

Normalization: every OK/STALE reading conforms exactly to the parent
omnioracle_real_signal_connector_v1.py normalization_schema's required_fields
(signal_id, source_type, timestamp, confidence, importance, risk_level, raw_payload)
-- see test_live_signal_connector.py:test_conforms_to_normalization_schema."""

import json
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/opt/pulse5-core")
ORACLE = ROOT / "divisions/omnioracle"
CONNECTOR_DIR = ORACLE / "real_signal_connector/live_ingestion"
CONNECTOR_DIR.mkdir(parents=True, exist_ok=True)

STALE_AFTER_SECONDS = 900  # 15 min -- matches omnioracle-continuous-daemon.timer's own cadence
DUPLICATE_SUPPRESSION_WINDOW_SECONDS = 30  # collect() called faster than this with an
                                            # identical reading writes no new file

WATCHED_UNITS = ["omnioracle-continuous-daemon.timer", "omnisim-loop.timer"]

REQUIRED_NORMALIZED_FIELDS = ["signal_id", "source_type", "timestamp", "confidence", "importance", "risk_level", "raw_payload"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _service_health():
    health = {}
    for unit in WATCHED_UNITS:
        try:
            proc = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
            health[unit] = proc.stdout.strip() or proc.stderr.strip()
        except Exception as exc:
            health[unit] = f"UNKNOWN: {exc}"
    return health


def _payload_digest(raw_payload: dict) -> str:
    return hashlib.sha256(json.dumps(raw_payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _read_metric(source_health: dict, key: str, fn):
    """Read one metric; record its own success/failure in source_health rather than
    letting one failing metric silently blank the whole signal."""
    try:
        value = fn()
        source_health[key] = "ok"
        return value
    except Exception as exc:
        source_health[key] = f"unavailable: {exc}"
        return None


def collect(force: bool = False) -> dict:
    """Read REAL local system telemetry. Fails closed to status=CONNECTOR_UNAVAILABLE
    (not a fabricated reading) if the underlying reads themselves fail. Per-metric
    failures are recorded in source_health without failing the whole signal. Skips
    writing a new file (duplicate-event protection) if an OK reading already exists
    within DUPLICATE_SUPPRESSION_WINDOW_SECONDS -- time-windowed, not exact-content
    match, since load average fluctuates continuously and would almost never hash
    identically between two close-together reads -- unless force=True."""
    import os, shutil

    source_health = {}
    try:
        load_avg = _read_metric(source_health, "load_average", os.getloadavg)
        load1, load5, load15 = load_avg if load_avg else (None, None, None)

        disk = _read_metric(source_health, "disk", lambda: shutil.disk_usage("/"))
        disk_used_fraction = round(disk.used / disk.total, 4) if disk and disk.total else None

        def _read_mem():
            with open("/proc/meminfo") as f:
                fields = {}
                for line in f:
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
                return int(fields["MemTotal"].split()[0]), int(fields["MemAvailable"].split()[0])

        mem = _read_metric(source_health, "memory", _read_mem)
        mem_total, mem_available = mem if mem else (None, None)
        mem_used_fraction = round(1 - (mem_available / mem_total), 4) if mem_total and mem_available is not None else None

        service_health = _read_metric(source_health, "service_health", _service_health)

        if not any(v == "ok" for v in source_health.values()):
            # every single metric failed -- this is a connector-level failure, not a
            # partial reading.
            raise RuntimeError(f"all metrics failed: {source_health}")

        risk_level = "low"
        if (disk_used_fraction and disk_used_fraction > 0.90) or (mem_used_fraction and mem_used_fraction > 0.90):
            risk_level = "high"
        elif (disk_used_fraction and disk_used_fraction > 0.75) or (mem_used_fraction and mem_used_fraction > 0.75):
            risk_level = "medium"

        raw_payload = {
            "load_average_1m": load1,
            "load_average_5m": load5,
            "load_average_15m": load15,
            "disk_used_fraction": disk_used_fraction,
            "memory_used_fraction": mem_used_fraction,
            "watched_service_health": service_health,
        }
        digest = _payload_digest(raw_payload)

        if not force:
            # Time-window throttle, not exact-content dedup: load average is a
            # continuously fluctuating real measurement, so two readings a few
            # seconds apart will almost never hash identically even though neither
            # is meaningfully "new" information -- suppress writing a fresh file
            # (and inflating evidence counts on rapid repeated calls) whenever a
            # recent-enough OK reading already exists, and note whether the content
            # actually drifted via payload_digest for observability.
            existing = latest_signal(max_age_seconds=DUPLICATE_SUPPRESSION_WINDOW_SECONDS)
            if existing.get("status") == "OK":
                return {
                    **existing,
                    "duplicate_suppressed": True,
                    "content_drifted_since_suppressed_reading": existing.get("payload_digest") != digest,
                }

        signal = {
            "signal_id": f"SIG_SYSTEM_RUNTIME_{int(datetime.now(timezone.utc).timestamp()*1_000_000)}",
            "source_type": "system_runtime",
            "timestamp": _now(),
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
            "signal_id": f"SIG_SYSTEM_RUNTIME_ERROR_{int(datetime.now(timezone.utc).timestamp()*1_000_000)}",
            "source_type": "system_runtime",
            "timestamp": _now(),
            "confidence": None,
            "importance": None,
            "risk_level": None,
            "provenance": "live",
            "source_health": source_health,
            "raw_payload": None,
            "status": "CONNECTOR_UNAVAILABLE",
            "error": str(exc),
        }

    out = CONNECTOR_DIR / f"{signal['signal_id']}.json"
    out.write_text(json.dumps(signal, indent=2))
    return signal


def is_fresh(signal: dict, max_age_seconds: int = STALE_AFTER_SECONDS) -> bool:
    """Reusable freshness gate: True only for an OK signal with a parseable
    timestamp younger than max_age_seconds. Never true for a missing/errored/
    unparseable signal."""
    if not signal or signal.get("status") != "OK":
        return False
    try:
        ts = datetime.fromisoformat(signal["timestamp"])
    except Exception:
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_seconds


def latest_signal(max_age_seconds: int = STALE_AFTER_SECONDS) -> dict:
    """Read the most recent already-collected signal from disk without collecting a
    new one. Distinguishes live/fresh from stale from absent -- never silently reuses
    old data as if it were current."""
    files = sorted(CONNECTOR_DIR.glob("SIG_SYSTEM_RUNTIME_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
