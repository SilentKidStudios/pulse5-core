"""Tests for node_capability_registry.py — WALK-AWAY CONVERGENCE gap-closure
slice 6/N (2026-09-04)."""
from __future__ import annotations

from dataclasses import asdict

import dispatch_admission as da
import node_capability_registry as ncr


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(ncr, "NODES_DIR", tmp_path / "nodes")


def _healthy_vector(**overrides) -> da.ResourceVector:
    base = dict(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
    )
    base.update(overrides)
    return da.ResourceVector(**base)


def test_register_and_load_node(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("render-forge-01", capabilities=["gpu", "render"])
    record = ncr.load("render-forge-01")
    assert record is not None
    assert record.capabilities == ["gpu", "render"]
    assert record.health_status == ncr.UNKNOWN


def test_never_health_checked_node_is_not_healthy(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("render-forge-01", capabilities=["gpu"])
    record = ncr.load("render-forge-01")
    assert ncr.is_healthy(record) is False


def test_recent_healthy_check_is_healthy(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("render-forge-01", capabilities=["gpu"])
    ncr.record_health("render-forge-01", ncr.HEALTHY, resource_vector=asdict(_healthy_vector()))
    record = ncr.load("render-forge-01")
    assert ncr.is_healthy(record) is True


def test_stale_health_check_is_not_healthy_fail_closed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("render-forge-01", capabilities=["gpu"])
    ncr.record_health("render-forge-01", ncr.HEALTHY, resource_vector=asdict(_healthy_vector()))
    record = ncr.load("render-forge-01")
    from datetime import datetime, timedelta, timezone
    record.last_health_check_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert ncr.is_healthy(record) is False


def test_degraded_status_is_not_healthy(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("render-forge-01", capabilities=["gpu"])
    ncr.record_health("render-forge-01", ncr.DEGRADED, resource_vector=asdict(_healthy_vector()))
    assert ncr.is_healthy(ncr.load("render-forge-01")) is False


def test_placement_fails_closed_when_no_node_has_required_capability(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(da, "read_resource_vector", lambda: _healthy_vector())
    decision = ncr.placement_decision(required_capabilities=["gpu"])
    assert decision.node_id is None
    assert "no healthy node" in decision.reason


def test_placement_falls_back_to_local_when_capability_matches(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(da, "read_resource_vector", lambda: _healthy_vector())
    decision = ncr.placement_decision(required_capabilities=["cpu"])
    assert decision.node_id == ncr.LOCAL_NODE_ID


def test_placement_prefers_remote_node_with_more_headroom(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(da, "read_resource_vector", lambda: _healthy_vector(mem_available_mb=1500))
    ncr.register_node("big-node", capabilities=["cpu", "gpu"])
    ncr.record_health("big-node", ncr.HEALTHY, resource_vector=asdict(_healthy_vector(mem_available_mb=64000, cpu_count=64)))

    decision = ncr.placement_decision(required_capabilities=["cpu"])
    assert decision.node_id == "big-node"


def test_placement_ignores_unhealthy_remote_node_even_if_registered(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(da, "read_resource_vector", lambda: _healthy_vector())
    ncr.register_node("flaky-node", capabilities=["cpu"])
    ncr.record_health("flaky-node", ncr.UNREACHABLE, resource_vector=None)

    decision = ncr.placement_decision(required_capabilities=["cpu"])
    assert decision.node_id == ncr.LOCAL_NODE_ID  # never falls through to the unreachable one


def test_placement_fails_closed_when_local_is_also_resource_pressured(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    pressured = _healthy_vector(swap_total_mb=2000, swap_used_mb=2000)
    monkeypatch.setattr(da, "read_resource_vector", lambda: pressured)
    decision = ncr.placement_decision(required_capabilities=["cpu"])
    assert decision.node_id is None


def test_registering_twice_is_idempotent_and_preserves_health_history(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    ncr.register_node("node-a", capabilities=["cpu"])
    ncr.record_health("node-a", ncr.HEALTHY, resource_vector=asdict(_healthy_vector()))
    ncr.register_node("node-a", capabilities=["cpu", "gpu"])  # re-register with more capabilities
    record = ncr.load("node-a")
    assert record.capabilities == ["cpu", "gpu"]
    assert record.health_status == ncr.HEALTHY  # not reset by re-registration
