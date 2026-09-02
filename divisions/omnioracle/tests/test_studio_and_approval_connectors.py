"""Tests for the two connectors wired live in RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION
(SIG_STUDIO_ACTIVITY, SIG_APPROVAL_PATTERNS) and their shared _connector_common helper.
Both read real, already-live mr_silent_spine/mrsilent_bridge state -- read-only, no
mocks needed for the happy path -- but every failure mode must still fail closed."""
import sys
from pathlib import Path

CONNECTOR_DIR = Path(__file__).resolve().parent.parent / "real_signal_connector"
sys.path.insert(0, str(CONNECTOR_DIR))

import _connector_common as cc
import live_studio_activity_connector_v1 as studio
import live_approval_patterns_connector_v1 as approval


# --- shared helper module -----------------------------------------------------

def test_payload_digest_deterministic():
    a = cc.payload_digest({"x": 1, "y": 2})
    b = cc.payload_digest({"y": 2, "x": 1})  # key order must not matter
    assert a == b


def test_is_fresh_and_stale():
    import datetime
    fresh = {"status": "OK", "timestamp": cc.now_iso()}
    assert cc.is_fresh(fresh) is True
    old = {"status": "OK", "timestamp": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()}
    assert cc.is_fresh(old) is False
    assert cc.is_fresh(None) is False
    assert cc.is_fresh({"status": "CONNECTOR_UNAVAILABLE", "timestamp": cc.now_iso()}) is False


def test_new_signal_id_unique_across_rapid_calls():
    ids = {cc.new_signal_id("X") for _ in range(50)}
    assert len(ids) == 50  # microsecond precision, no collisions


# --- SIG_STUDIO_ACTIVITY -------------------------------------------------------

def test_studio_activity_returns_live_provenance_and_ground_truth_confidence():
    signal = studio.collect()
    assert signal["provenance"] == "live"
    if signal["status"] == "OK":
        assert signal["confidence"] == 1.0
        for field in cc.REQUIRED_NORMALIZED_FIELDS:
            assert field in signal


def test_studio_activity_source_health_reports_real_metrics():
    signal = studio.collect()
    if signal["status"] == "OK":
        assert set(signal["source_health"].keys()) == {"inbox", "receipts"}
        assert signal["source_health"]["inbox"] == "ok"


def test_studio_activity_read_only_never_writes_to_mr_silent_spine(monkeypatch):
    # Confirm the connector's only Path.write_text touches divisions/omnioracle,
    # never mr_silent_spine, by watching every write during a real collect().
    import pathlib
    writes = []
    real_write_text = pathlib.Path.write_text

    def spy_write_text(self, *a, **k):
        writes.append(str(self))
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)
    studio.collect(force=True)
    assert all("mr_silent_spine" not in w for w in writes)
    assert any("divisions/omnioracle" in w for w in writes)


def test_studio_activity_degrades_on_single_metric_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, "CONNECTOR_DIR", tmp_path)

    def boom():
        raise OSError("simulated bus read failure")

    monkeypatch.setattr(studio, "_recent_receipts", boom)
    signal = studio.collect()
    assert signal["status"] == "OK"
    assert signal["source_health"]["receipts"].startswith("unavailable")


def test_studio_activity_fails_closed_when_every_metric_fails(monkeypatch, tmp_path):
    import pathlib
    monkeypatch.setattr(studio, "CONNECTOR_DIR", tmp_path)

    def boom(*a, **k):
        raise OSError("simulated failure")

    monkeypatch.setattr(studio, "_recent_receipts", boom)
    # BUS_INBOX.glob on a path that exists returns real results; force the
    # existence check itself to raise (class-level, auto-reverted) so both
    # metrics fail and the all-metrics-failed path is exercised.
    monkeypatch.setattr(pathlib.Path, "exists", boom)
    signal = studio.collect()
    assert signal["status"] == "CONNECTOR_UNAVAILABLE"
    assert signal["confidence"] is None


def test_studio_activity_duplicate_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "CONNECTOR_DIR", tmp_path)
    r1 = studio.collect()
    r2 = studio.collect()
    assert r1.get("duplicate_suppressed") is None
    assert r2.get("duplicate_suppressed") is True


# --- SIG_APPROVAL_PATTERNS ------------------------------------------------------

def test_approval_patterns_returns_live_provenance_and_ground_truth_confidence():
    signal = approval.collect()
    assert signal["provenance"] == "live"
    if signal["status"] == "OK":
        assert signal["confidence"] == 1.0
        for field in cc.REQUIRED_NORMALIZED_FIELDS:
            assert field in signal


def test_approval_patterns_reads_real_job_ledger_distribution():
    signal = approval.collect()
    if signal["status"] == "OK":
        payload = signal["raw_payload"]
        assert payload["total_job_records"] > 0
        assert isinstance(payload["approval_state_distribution"], dict)
        assert sum(payload["approval_state_distribution"].values()) == payload["total_job_records"]


def test_approval_patterns_read_only_never_writes_to_mrsilent_bridge(monkeypatch):
    import pathlib
    writes = []
    real_write_text = pathlib.Path.write_text

    def spy_write_text(self, *a, **k):
        writes.append(str(self))
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)
    approval.collect(force=True)
    assert all("mrsilent_bridge" not in w for w in writes)
    assert any("divisions/omnioracle" in w for w in writes)


def test_approval_patterns_fails_closed_when_ledger_read_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(approval, "CONNECTOR_DIR", tmp_path)

    def boom():
        raise RuntimeError("simulated job_ledger failure")

    monkeypatch.setattr(approval, "_job_ledger_records", boom)
    signal = approval.collect()
    assert signal["status"] == "CONNECTOR_UNAVAILABLE"
    assert signal["confidence"] is None


def test_approval_patterns_duplicate_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "CONNECTOR_DIR", tmp_path)
    r1 = approval.collect()
    r2 = approval.collect()
    assert r1.get("duplicate_suppressed") is None
    assert r2.get("duplicate_suppressed") is True
