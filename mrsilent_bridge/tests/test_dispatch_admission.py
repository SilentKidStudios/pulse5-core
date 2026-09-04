"""Tests for dispatch_admission.py — dynamic resource-aware admission
control built during the WALK-AWAY CONVERGENCE gap-closure pass
(2026-09-04). Decision functions are pure and tested with synthetic
ResourceVectors; only one smoke test touches the real host (read-only
kernel state, not the repo checkout — no worktree-isolation risk)."""
from __future__ import annotations

import dispatch_admission as da


def _vector(**overrides) -> da.ResourceVector:
    base = dict(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
    )
    base.update(overrides)
    return da.ResourceVector(**base)


def test_read_resource_vector_smoke_on_real_host():
    v = da.read_resource_vector()
    assert v.cpu_count >= 1
    assert v.mem_total_mb > 0
    assert 0.0 <= v.disk_used_pct <= 100.0


def test_admission_allowed_on_healthy_host():
    allowed, reason = da.admission_decision(_vector())
    assert allowed is True
    assert reason == "admitted"


def test_admission_refused_on_low_available_memory():
    allowed, reason = da.admission_decision(_vector(mem_available_mb=500))
    assert allowed is False
    assert "memory" in reason


def test_admission_refused_on_swap_exhaustion():
    """Directly models this project's own observed reality: swap fully
    used (2.0Gi/2.0Gi) — see the audit's HOST_RESOURCE_SNAPSHOT finding."""
    allowed, reason = da.admission_decision(_vector(swap_total_mb=2000, swap_used_mb=2000))
    assert allowed is False
    assert "swap" in reason


def test_admission_refused_on_disk_pressure():
    allowed, reason = da.admission_decision(_vector(disk_used_pct=95.0))
    assert allowed is False
    assert "disk" in reason


def test_admission_refused_on_load_pressure():
    allowed, reason = da.admission_decision(_vector(cpu_count=8, load_avg_1m=20.0))
    assert allowed is False
    assert "load" in reason


def test_dynamic_concurrency_is_zero_under_swap_exhaustion():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000)
    assert da.dynamic_safe_concurrency(v) == 0
    assert da.concurrency_limit_reason(v) == "swap_exhausted"


def test_dynamic_concurrency_is_zero_under_disk_exhaustion():
    v = _vector(disk_used_pct=95.0)
    assert da.dynamic_safe_concurrency(v) == 0
    assert da.concurrency_limit_reason(v) == "disk_exhausted"


def test_dynamic_concurrency_scales_up_with_more_available_memory():
    small = da.dynamic_safe_concurrency(_vector(mem_available_mb=2000), per_worker_mem_mb=512)
    large = da.dynamic_safe_concurrency(_vector(mem_available_mb=32000), per_worker_mem_mb=512)
    assert large > small


def test_dynamic_concurrency_scales_up_with_more_cpus_and_headroom():
    """Section 7's explicit acceptance requirement: stronger future
    hardware must raise safe concurrency automatically through the SAME
    function, not require a rewrite."""
    modest = da.dynamic_safe_concurrency(_vector(cpu_count=8, load_avg_1m=1.0, mem_available_mb=64000), per_worker_mem_mb=256)
    huge = da.dynamic_safe_concurrency(_vector(cpu_count=128, load_avg_1m=1.0, mem_available_mb=64000), per_worker_mem_mb=256)
    assert huge > modest


def test_dynamic_concurrency_never_negative():
    v = _vector(cpu_count=1, load_avg_1m=50.0, mem_available_mb=100)
    assert da.dynamic_safe_concurrency(v) == 0


def test_dynamic_concurrency_respects_absolute_ceiling(monkeypatch):
    monkeypatch.setattr(da, "MAX_ABSOLUTE_CONCURRENCY", 3)
    v = _vector(cpu_count=256, load_avg_1m=0.0, mem_available_mb=1_000_000)
    assert da.dynamic_safe_concurrency(v, per_worker_mem_mb=1) == 3


def test_concurrency_limit_reason_memory_bound():
    v = _vector(cpu_count=64, load_avg_1m=0.0, mem_available_mb=2000)
    assert da.concurrency_limit_reason(v, per_worker_mem_mb=512) == "memory_bound"


def test_backpressure_inactive_below_high_watermark():
    status = da.backpressure_status(queue_depth=3)
    assert status["BACKPRESSURE_ACTIVE"] is False
    assert status["QUEUE_DEPTH"] == 3
    assert status["RESOURCE_HIGH_WATER_MARK"] == da.QUEUE_HIGH_WATER
    assert status["RESOURCE_LOW_WATER_MARK"] == da.QUEUE_LOW_WATER


def test_backpressure_active_at_or_above_high_watermark():
    status = da.backpressure_status(queue_depth=da.QUEUE_HIGH_WATER)
    assert status["BACKPRESSURE_ACTIVE"] is True
