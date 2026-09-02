"""Omni Oracle observability -- current health, last cycle, and validation state.

Read-only aggregator over real on-disk state. Does not trigger any engine.
Answers: is the live pipeline running, when did it last run, did it succeed,
what engines exist and are they registered, what's the current evidence
volume, and what's the last request handled through request_oracle.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

ORACLE = Path(__file__).resolve().parent.parent
DAEMON_STATE = ORACLE / "continuous_oracle_daemon/state/continuous_oracle_daemon_v1_state.json"
RECEIPTS = Path(__file__).resolve().parent / "receipts"
FORECAST_DIR = ORACLE / "probabilistic_forecast_engine/forecasts"
LEDGER_DIR = ORACLE / "forecast_ledger/entries"
VERIFICATIONS_DIR = ORACLE / "forecast_outcome_verifier/verifications"


def _now():
    return datetime.now(timezone.utc).isoformat()


def health() -> dict:
    daemon_state = None
    daemon_age_seconds = None
    if DAEMON_STATE.exists():
        try:
            daemon_state = json.loads(DAEMON_STATE.read_text())
            updated_at = datetime.fromisoformat(daemon_state["updated_at"])
            daemon_age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        except Exception:
            daemon_state = {"error": "state file present but unreadable"}

    last_requests = []
    if RECEIPTS.exists():
        receipt_files = sorted(RECEIPTS.glob("*_receipt.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in receipt_files[:5]:
            try:
                data = json.loads(f.read_text())
                last_requests.append({
                    "request_id": data.get("request_id"),
                    "timestamp": data.get("timestamp"),
                    "domain": data.get("request", {}).get("domain"),
                    "mode": data.get("request", {}).get("mode"),
                    "status": data.get("status"),
                    "reproducible": data.get("reproducible"),
                })
            except Exception:
                continue

    return {
        "checked_at": _now(),
        "component": "omnioracle",
        "daemon_timer": "omnioracle-continuous-daemon.timer (systemd)",
        "daemon_last_cycle": daemon_state,
        "daemon_last_cycle_age_seconds": daemon_age_seconds,
        "daemon_considered_healthy": bool(daemon_state and daemon_state.get("all_ok") and
                                           daemon_age_seconds is not None and daemon_age_seconds < 3600),
        "evidence_volume": {
            "forecast_artifacts": len(list(FORECAST_DIR.glob("*.json"))) if FORECAST_DIR.exists() else 0,
            "ledger_entries": len(list(LEDGER_DIR.glob("*.json"))) if LEDGER_DIR.exists() else 0,
            "verifications": len(list(VERIFICATIONS_DIR.glob("*.json"))) if VERIFICATIONS_DIR.exists() else 0,
        },
        "request_api_receipts_total": len(list(RECEIPTS.glob("*_receipt.json"))) if RECEIPTS.exists() else 0,
        "last_5_requests_via_request_oracle": last_requests,
        "known_limitations": [
            "confidence/probability fields are synthetic placeholders, not calibrated forecasts "
            "(see divisions/omnioracle/api/request_oracle.py module docstring)",
            "not registered in mrsilent_bridge/registry_data/capabilities.json as of this writing "
            "(pending human review; see divisions/omnioracle/api/proposed_registry_entries_v1.json)",
            "not wired into services/th3_mcp/tools.py (owned by a separate active session as of this writing)",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(health(), indent=2, default=str))
