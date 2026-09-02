#!/usr/bin/env python3
"""Live system runtime connector -- the ONE real_signal_connector connector actually
flipped to live_collection_enabled=True in the OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_
LIVE-SIGNAL_CLOSURE campaign (2026-09-02).

Of the 5 blueprint connectors in omnioracle_real_signal_connector_v1.py
(SIG_SOCIAL_TRENDS, SIG_SYSTEM_RUNTIME, SIG_APPROVAL_PATTERNS, SIG_MARKET_INTEL,
SIG_STUDIO_ACTIVITY), only SIG_SYSTEM_RUNTIME can be made genuinely live in this
session without new credentials, Founder secrets, paid activation, or touching any
file owned by the concurrently active Work 10.7 session: local machine telemetry
(load average, memory, disk, and the health of this division's own systemd units)
requires no external account and no protected state.

The other 4 remain blueprint_ready/live_collection_enabled=False -- SIG_SOCIAL_TRENDS
and SIG_MARKET_INTEL need real external API credentials (paid_resources_allowed=false
blocks this outright without Founder authorization); SIG_APPROVAL_PATTERNS would need
reading mr_silent_spine internal approval-queue state, which this campaign's
concurrency boundary keeps out of scope; SIG_STUDIO_ACTIVITY would need reading
mr_silent_spine/division_signal_bus, same boundary. See
divisions/omnioracle/api/proposed_registry_entries_v1.json / this campaign's final
report for the exact external dependency each one is blocked on.

Signal values here are REAL measurements, not evidence-derived heuristics -- so
confidence=1.0 (it is ground truth about this machine, not a projection) and
provenance="live", the only place in this division that field is ever anything but
"synthetic". Freshness is explicit: a reading older than STALE_AFTER_SECONDS is
reported status="STALE" rather than silently reused as if fresh."""

import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/opt/pulse5-core")
ORACLE = ROOT / "divisions/omnioracle"
CONNECTOR_DIR = ORACLE / "real_signal_connector/live_ingestion"
CONNECTOR_DIR.mkdir(parents=True, exist_ok=True)

STALE_AFTER_SECONDS = 900  # 15 min -- matches omnioracle-continuous-daemon.timer's own cadence

WATCHED_UNITS = ["omnioracle-continuous-daemon.timer", "omnisim-loop.timer"]


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


def collect() -> dict:
    """Read REAL local system telemetry. Fails closed to status=CONNECTOR_UNAVAILABLE
    (not a fabricated reading) if the underlying reads themselves fail."""
    try:
        import os, shutil
        load1, load5, load15 = os.getloadavg()
        disk = shutil.disk_usage("/")
        mem_total = mem_available = None
        try:
            with open("/proc/meminfo") as f:
                fields = {}
                for line in f:
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
                mem_total = int(fields["MemTotal"].split()[0])
                mem_available = int(fields["MemAvailable"].split()[0])
        except Exception:
            pass

        disk_used_fraction = round(disk.used / disk.total, 4) if disk.total else None
        mem_used_fraction = round(1 - (mem_available / mem_total), 4) if mem_total and mem_available is not None else None

        risk_level = "low"
        if (disk_used_fraction and disk_used_fraction > 0.90) or (mem_used_fraction and mem_used_fraction > 0.90):
            risk_level = "high"
        elif (disk_used_fraction and disk_used_fraction > 0.75) or (mem_used_fraction and mem_used_fraction > 0.75):
            risk_level = "medium"

        signal = {
            "signal_id": f"SIG_SYSTEM_RUNTIME_{int(datetime.now(timezone.utc).timestamp())}",
            "source_type": "system_runtime",
            "timestamp": _now(),
            "confidence": 1.0,
            "importance": "high" if risk_level != "low" else "moderate",
            "risk_level": risk_level,
            "provenance": "live",
            "raw_payload": {
                "load_average_1m": load1,
                "load_average_5m": load5,
                "load_average_15m": load15,
                "disk_used_fraction": disk_used_fraction,
                "memory_used_fraction": mem_used_fraction,
                "watched_service_health": _service_health(),
            },
            "status": "OK",
        }
    except Exception as exc:
        signal = {
            "signal_id": f"SIG_SYSTEM_RUNTIME_ERROR_{int(datetime.now(timezone.utc).timestamp())}",
            "source_type": "system_runtime",
            "timestamp": _now(),
            "confidence": None,
            "importance": None,
            "risk_level": None,
            "provenance": "live",
            "raw_payload": None,
            "status": "CONNECTOR_UNAVAILABLE",
            "error": str(exc),
        }

    out = CONNECTOR_DIR / f"{signal['signal_id']}.json"
    out.write_text(json.dumps(signal, indent=2))
    return signal


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
