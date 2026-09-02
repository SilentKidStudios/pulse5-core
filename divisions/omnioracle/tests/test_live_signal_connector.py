"""Tests for the live_system_runtime_connector (Part C: real signal connector
closure). This is real local-machine telemetry -- no external credentials, no mocks
needed for the happy path, but failure must still fail closed."""
import sys
import json
from pathlib import Path

CONNECTOR_DIR = Path(__file__).resolve().parent.parent / "real_signal_connector"
sys.path.insert(0, str(CONNECTOR_DIR))

import live_system_runtime_connector_v1 as connector


def test_collect_returns_real_provenance_and_ground_truth_confidence():
    signal = connector.collect()
    assert signal["provenance"] == "live"
    if signal["status"] == "OK":
        assert signal["confidence"] == 1.0  # ground truth measurement, not a projection


def test_collect_writes_a_receipt_file():
    signal = connector.collect()
    matches = list(connector.CONNECTOR_DIR.glob(f"{signal['signal_id']}.json"))
    assert len(matches) == 1


def test_collect_fails_closed_on_internal_error(monkeypatch):
    import os as os_module

    def boom():
        raise OSError("simulated /proc read failure")

    monkeypatch.setattr(os_module, "getloadavg", boom)
    signal = connector.collect()
    assert signal["status"] == "CONNECTOR_UNAVAILABLE"
    assert signal["confidence"] is None  # never a fabricated confidence on failure


def test_latest_signal_reports_no_signal_when_none_collected(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    result = connector.latest_signal()
    assert result["status"] == "NO_SIGNAL_COLLECTED_YET"


def test_latest_signal_reports_stale_for_old_reading(tmp_path, monkeypatch):
    import datetime
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
    stale_file = tmp_path / "SIG_SYSTEM_RUNTIME_old.json"
    stale_file.write_text(json.dumps({
        "signal_id": "SIG_SYSTEM_RUNTIME_old", "source_type": "system_runtime",
        "timestamp": old_ts, "confidence": 1.0, "status": "OK", "provenance": "live",
        "raw_payload": {},
    }))
    result = connector.latest_signal(max_age_seconds=900)
    assert result["status"] == "STALE"


def test_request_oracle_surfaces_live_signals_field():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
    import request_oracle as ro
    result = ro.request_oracle("runtime_autonomy", mode="read")
    assert "live_signals" in result
    assert result["live_signals"].get("provenance") == "live"
