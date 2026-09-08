"""Tests for node_capability_registry.py — WALK-AWAY CONVERGENCE gap-closure
slice 6/N (2026-09-04) and Founder-authorized read-only SSH health probe
(2026-09-07)."""
from __future__ import annotations

import subprocess
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
        psi_memory_avg10=0.0, oom_kill_recent=False,  # DIRECT_PSI_OOM_GATE (2026-09-08): healthy baseline
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


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_ssh_stdout(*, gpu_line: str = "NO_GPU") -> str:
    return "\n".join([
        "render-forge-01",   # hostname
        "8",                 # nproc
        "0.15 0.10 0.05 1/200 12345",  # /proc/loadavg
        "31000 19000",       # mem total/available MB
        "0 0",                # swap total/used MB
        "485 176",            # disk total/free GB
        gpu_line,
    ])


def test_probe_ssh_node_health_success_with_gpu(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, _fake_ssh_stdout(gpu_line="NVIDIA RTX 4000 Ada Generation, 20475, 274, 0")),
    )
    record = ncr.probe_ssh_node_health(
        "render-forge-01", ssh_target="root@100.105.150.104",
        ssh_key="/root/.ssh/pulse5_gpu_render", capabilities=["gpu", "render"],
    )
    # DIRECT_PSI_OOM_GATE (2026-09-08): _REMOTE_READ_ONLY_PROBE_SCRIPT's
    # authorized scope is exactly hostname/CPU/RAM/GPU/VRAM/disk — it does
    # not (and per that authorization's own scope, should not without
    # separate authorization) collect PSI/OOM over SSH. da.ResourceVector's
    # own fail-closed defaults (psi_memory_avg10=100.0, oom_kill_recent=
    # True) therefore correctly apply here, and admission_decision() now
    # correctly refuses admission for a vector it cannot confirm is
    # memory-healthy — DEGRADED, not HEALTHY, is the CORRECT, more
    # conservative outcome for a remote probe with no real memory-pressure
    # visibility, not a regression.
    assert record.health_status == ncr.DEGRADED
    assert record.resource_vector["cpu_count"] == 8
    assert record.extra["hostname"] == "render-forge-01"
    assert record.extra["gpu"]["present"] is True
    assert record.extra["gpu"]["vram_total_mb"] == 20475.0
    assert ncr.is_healthy(record) is False


def test_probe_ssh_node_health_success_without_gpu(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, _fake_ssh_stdout()))
    record = ncr.probe_ssh_node_health("some-node", ssh_target="root@1.2.3.4")
    assert record.extra["gpu"]["present"] is False


def test_probe_ssh_node_health_nonzero_exit_is_unreachable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(255, ""))
    record = ncr.probe_ssh_node_health("render-forge-01", ssh_target="root@1.2.3.4")
    assert record.health_status == ncr.UNREACHABLE
    assert record.resource_vector is None
    assert ncr.is_healthy(record) is False


def test_probe_ssh_node_health_timeout_is_unreachable_not_raised(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

    monkeypatch.setattr(subprocess, "run", _raise)
    record = ncr.probe_ssh_node_health("render-forge-01", ssh_target="root@1.2.3.4")
    assert record.health_status == ncr.UNREACHABLE


def test_probe_ssh_node_health_malformed_output_is_unreachable(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, "garbage\nnot enough lines"))
    record = ncr.probe_ssh_node_health("render-forge-01", ssh_target="root@1.2.3.4")
    assert record.health_status == ncr.UNREACHABLE


def test_probe_ssh_node_health_registers_node_first_if_absent(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, _fake_ssh_stdout()))
    assert ncr.load("brand-new-node") is None
    ncr.probe_ssh_node_health("brand-new-node", ssh_target="root@1.2.3.4", capabilities=["gpu"])
    record = ncr.load("brand-new-node")
    assert record is not None
    assert record.capabilities == ["gpu"]
    assert record.endpoint == "root@1.2.3.4"


def test_probe_ssh_node_health_never_puts_ssh_key_in_persisted_record(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, _fake_ssh_stdout()))
    record = ncr.probe_ssh_node_health(
        "render-forge-01", ssh_target="root@1.2.3.4", ssh_key="/root/.ssh/some_private_key",
    )
    serialized = str(asdict(record))
    assert "some_private_key" not in serialized
