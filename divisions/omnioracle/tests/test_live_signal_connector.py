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


def test_collect_degrades_gracefully_on_single_metric_failure(monkeypatch, tmp_path):
    # Priority 3 hardening: one metric failing must not fail the whole reading --
    # it's recorded in source_health and the other real metrics still count.
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    import os as os_module

    def boom():
        raise OSError("simulated /proc read failure")

    monkeypatch.setattr(os_module, "getloadavg", boom)
    signal = connector.collect()
    assert signal["status"] == "OK"
    assert signal["source_health"]["load_average"].startswith("unavailable")
    assert signal["source_health"]["disk"] == "ok"


def test_collect_fails_closed_when_every_metric_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    import os as os_module, shutil as shutil_module, builtins

    def boom(*a, **k):
        raise OSError("simulated total failure")

    real_open = builtins.open

    def selective_open_boom(file, *a, **k):
        if str(file) == "/proc/meminfo":
            raise OSError("simulated failure")
        return real_open(file, *a, **k)

    monkeypatch.setattr(os_module, "getloadavg", boom)
    monkeypatch.setattr(shutil_module, "disk_usage", boom)
    monkeypatch.setattr(connector, "_service_health", boom)
    monkeypatch.setattr(builtins, "open", selective_open_boom)
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


def test_conforms_to_normalization_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    signal = connector.collect()
    for field in connector.REQUIRED_NORMALIZED_FIELDS:
        assert field in signal


def test_duplicate_event_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    r1 = connector.collect()
    r2 = connector.collect()  # called immediately after -- well within the throttle window
    assert r1.get("duplicate_suppressed") is None
    assert r2.get("duplicate_suppressed") is True
    assert r1["signal_id"] == r2["signal_id"]
    assert len(list(tmp_path.glob("SIG_SYSTEM_RUNTIME_*.json"))) == 1


def test_force_bypasses_duplicate_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_DIR", tmp_path)
    r1 = connector.collect()
    r2 = connector.collect(force=True)
    assert r1["signal_id"] != r2["signal_id"]


def test_is_fresh_true_for_recent_ok_signal():
    signal = {"status": "OK", "timestamp": connector._now()}
    assert connector.is_fresh(signal) is True


def test_is_fresh_false_for_missing_error_or_old_signal():
    import datetime
    assert connector.is_fresh(None) is False
    assert connector.is_fresh({"status": "CONNECTOR_UNAVAILABLE", "timestamp": connector._now()}) is False
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)).isoformat()
    assert connector.is_fresh({"status": "OK", "timestamp": old_ts}) is False


def test_live_signal_fuses_into_runtime_autonomy_forecast(tmp_path, monkeypatch):
    import importlib.util
    pfe_path = Path(__file__).resolve().parent.parent / "probabilistic_forecast_engine" / "omnioracle_probabilistic_forecast_engine_v1.py"
    spec = importlib.util.spec_from_file_location("pfe_livefusion_test", pfe_path)
    pfe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pfe)

    isolated_ledger = tmp_path / "entries"
    isolated_ledger.mkdir()
    monkeypatch.setattr(pfe, "LEDGER", isolated_ledger)

    result = pfe.forecast_domain("runtime_autonomy", seed_override=1, min_evidence=0)
    assert result["evidence_inputs"]["live_signal"]["applicable"] is True
    # source_health/status must be reported either way (fresh OK, or honestly excluded)
    assert "status" in result["evidence_inputs"]["live_signal"] or "excluded_reason" in result["evidence_inputs"]["live_signal"]


def test_live_signal_not_applicable_for_unrelated_domain(tmp_path, monkeypatch):
    import importlib.util
    pfe_path = Path(__file__).resolve().parent.parent / "probabilistic_forecast_engine" / "omnioracle_probabilistic_forecast_engine_v1.py"
    spec = importlib.util.spec_from_file_location("pfe_livefusion_test2", pfe_path)
    pfe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pfe)

    isolated_ledger = tmp_path / "entries"
    isolated_ledger.mkdir()
    monkeypatch.setattr(pfe, "LEDGER", isolated_ledger)

    result = pfe.forecast_domain("swarm_coordination", seed_override=1, min_evidence=0)
    assert result["evidence_inputs"]["live_signal"]["applicable"] is False
