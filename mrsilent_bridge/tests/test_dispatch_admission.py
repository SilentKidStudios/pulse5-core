"""Tests for dispatch_admission.py — dynamic resource-aware admission
control built during the WALK-AWAY CONVERGENCE gap-closure pass
(2026-09-04). Decision functions are pure and tested with synthetic
ResourceVectors; only one smoke test touches the real host (read-only
kernel state, not the repo checkout — no worktree-isolation risk)."""
from __future__ import annotations

import json
import time

import dispatch_admission as da


def _vector(**overrides) -> da.ResourceVector:
    base = dict(
        cpu_count=8, load_avg_1m=1.0,
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=0,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
        psi_memory_avg10=0.0, oom_kill_recent=False,  # healthy baseline for the new signals
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


# ---- COLD-SWAP REFINEMENT (2026-09-05) -------------------------------------
# swap_active defaults to True (fail-closed) on ResourceVector, so every test
# above that never mentions it is an implicit "swap presumed active" case —
# confirming the refinement changes nothing for existing callers unless they
# explicitly supply a cold, confirmed-inactive vector.

def test_existing_swap_exhaustion_tests_are_unaffected_default_is_fail_closed():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000)
    assert v.swap_active is True  # dataclass default, not set by _vector()
    assert da.admission_decision(v)[0] is False
    assert da.dynamic_safe_concurrency(v) == 0
    assert da.concurrency_limit_reason(v) == "swap_exhausted"


def test_admission_refused_when_swap_full_and_actively_thrashing():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=True, mem_available_mb=16000)
    allowed, reason = da.admission_decision(v)
    assert allowed is False
    assert "thrashing" in reason


def test_admission_refused_when_swap_full_cold_but_memory_unhealthy():
    """Cold swap alone is not sufficient — available memory must also be
    genuinely healthy."""
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=da.MIN_HEALTHY_AVAILABLE_MB_FOR_COLD_SWAP_OVERRIDE - 1)
    allowed, reason = da.admission_decision(v)
    assert allowed is False
    assert "not healthy enough" in reason


def test_admission_allowed_when_swap_full_cold_and_memory_healthy():
    """The actual real-host scenario this campaign found: swap 100% used,
    zero vmstat si/so movement, 8+GB available."""
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=8700)
    allowed, reason = da.admission_decision(v)
    assert allowed is True


def test_dynamic_concurrency_bounded_not_full_throttle_under_cold_swap():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=64000, cpu_count=64, load_avg_1m=0.0)
    concurrency = da.dynamic_safe_concurrency(v, per_worker_mem_mb=64)
    assert concurrency > 0
    assert concurrency <= da.COLD_SWAP_BOUNDED_CONCURRENCY_CAP  # capped, never full-throttle even with huge headroom


def test_dynamic_concurrency_zero_under_cold_swap_with_unhealthy_memory():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False, mem_available_mb=500)
    assert da.dynamic_safe_concurrency(v) == 0


def test_dynamic_concurrency_zero_under_thrashing_swap_even_with_huge_memory():
    """False-admit guard: a huge amount of available memory must never
    override genuinely active swap thrashing."""
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=True, mem_available_mb=64000)
    assert da.dynamic_safe_concurrency(v) == 0


def test_concurrency_limit_reason_reports_cold_bounded_distinctly():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False, mem_available_mb=8700)
    assert da.concurrency_limit_reason(v) == "swap_cold_bounded"


def test_concurrency_limit_reason_reports_swap_exhausted_when_thrashing():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=True, mem_available_mb=8700)
    assert da.concurrency_limit_reason(v) == "swap_exhausted"


def test_detect_swap_activity_fails_closed_with_no_prior_sample(tmp_path):
    state_path = tmp_path / "swap_activity.json"
    assert da.detect_swap_activity(state_path=state_path) is True  # first-ever call: no prior sample to compare


def test_detect_swap_activity_detects_stable_counters_as_cold(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    counters = [(1000, 2000), (1000, 2000)]  # identical on both reads
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: counters.pop(0))
    first = da.detect_swap_activity(state_path=state_path)   # no prior sample -> fail closed True
    second = da.detect_swap_activity(state_path=state_path)  # same counters as the now-durable first sample -> cold
    assert first is True
    assert second is False


def test_detect_swap_activity_detects_real_movement_as_active(monkeypatch, tmp_path):
    """Sustained movement: 500 pages of combined pswpin delta over the ~0s
    that elapses between two back-to-back calls in a test is an effectively
    instantaneous burst — a rate far above SWAP_ACTIVITY_RATE_THRESHOLD_
    PAGES_PER_S once elapsed time is (however briefly) nonzero, so this
    pins elapsed time explicitly rather than relying on real wall-clock gaps
    between two immediately-sequential calls."""
    state_path = tmp_path / "swap_activity.json"
    counters = [(1000, 2000), (1500, 2000)]  # pswpin increased by 500 pages
    times = [1_000_000.0, 1_000_010.0]  # 10s apart
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: counters.pop(0))
    monkeypatch.setattr(time, "time", lambda: times.pop(0))
    da.detect_swap_activity(state_path=state_path)
    second = da.detect_swap_activity(state_path=state_path)
    assert second is True


def test_detect_swap_activity_fails_closed_when_prior_sample_is_stale(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 1000, "pswpout": 2000, "sampled_at": time.time() - 999999}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1000, 2000))
    assert da.detect_swap_activity(state_path=state_path) is True  # identical counters, but sample too old to trust


def test_detect_swap_activity_fails_closed_on_unreadable_vmstat(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: None)
    assert da.detect_swap_activity(state_path=state_path) is True


# ---- SWAP-ACTIVITY RATE NORMALIZATION (2026-09-06 fix) ----------------------
# Mandatory test matrix A-J: the natural-cycle defect proved "any nonzero
# cumulative pswpin/pswpout delta = active" false-positives on trivial
# incidental movement. These pin time.time() directly (rather than relying
# on real wall-clock gaps between two sequential calls) so elapsed time,
# and therefore the normalized rate, is exact and deterministic.

def test_case_A_detect_swap_activity_fails_closed_with_no_prior_sample(tmp_path):
    state_path = tmp_path / "swap_activity.json"
    assert da.detect_swap_activity(state_path=state_path) is True


def test_case_B_detect_swap_activity_fails_closed_on_unreadable_current_counters(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: None)
    assert da.detect_swap_activity(state_path=state_path) is True


def test_case_C_detect_swap_activity_fails_closed_on_corrupt_prior_state(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1000, 2000))
    for corrupt_body in ('{"not": "the right shape"}', "[1, 2, 3]", '"just a string"', "not even json"):
        state_path.write_text(corrupt_body)
        assert da.detect_swap_activity(state_path=state_path) is True


def test_case_D_detect_swap_activity_fails_closed_when_prior_sample_older_than_max_age(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({
        "pswpin": 1000, "pswpout": 2000,
        "sampled_at": 1_000_000.0 - (da.SWAP_SAMPLE_MAX_AGE_S + 1),
    }))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1000, 2000))
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    assert da.detect_swap_activity(state_path=state_path) is True


def test_case_E_detect_swap_activity_fails_closed_on_negative_elapsed_time_clock_skew(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 1000, "pswpout": 2000, "sampled_at": 1_000_100.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1000, 2000))
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)  # "now" before the durable sample's timestamp
    assert da.detect_swap_activity(state_path=state_path) is True


def test_case_F_detect_swap_activity_exact_zero_movement_is_cold(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 1000, "pswpout": 2000, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1000, 2000))
    monkeypatch.setattr(time, "time", lambda: 1_000_900.0)  # 15 minutes later, zero movement
    assert da.detect_swap_activity(state_path=state_path) is False


def test_case_G_detect_swap_activity_tiny_delta_over_normal_15min_cycle_is_cold(monkeypatch, tmp_path):
    """Mirrors the real natural-cycle defect evidence: 45 combined pages
    (11 in + 34 out) over a 900s (~15 minute) natural-cycle interval."""
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 422546838, "pswpout": 447543254, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (422546849, 447543288))
    monkeypatch.setattr(time, "time", lambda: 1_000_900.0)  # +900s = 15 minutes
    assert da.detect_swap_activity(state_path=state_path) is False


def test_case_H_detect_swap_activity_tiny_delta_over_long_valid_interval_is_cold(monkeypatch, tmp_path):
    """Same tiny 45-page delta, this time stretched across nearly the full
    SWAP_SAMPLE_MAX_AGE_S window — still trivially below the rate threshold."""
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 422546838, "pswpout": 447543254, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (422546849, 447543288))
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0 + da.SWAP_SAMPLE_MAX_AGE_S - 1)
    assert da.detect_swap_activity(state_path=state_path) is False


def test_case_I_detect_swap_activity_meaningful_sustained_movement_is_active(monkeypatch, tmp_path):
    """200 combined pages over 60s = ~3.3 pages/s, comfortably over
    SWAP_ACTIVITY_RATE_THRESHOLD_PAGES_PER_S (1.0) — real sustained paging,
    not incidental noise."""
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 1000, "pswpout": 2000, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (1100, 2100))  # +100 in, +100 out
    monkeypatch.setattr(time, "time", lambda: 1_000_060.0)  # 60s later
    assert da.detect_swap_activity(state_path=state_path) is True


def test_case_J_detect_swap_activity_heavy_thrashing_is_active(monkeypatch, tmp_path):
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 1000, "pswpout": 2000, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (51000, 62000))  # +50000 in, +60000 out
    monkeypatch.setattr(time, "time", lambda: 1_000_010.0)  # 10s later -> ~11,000 pages/s
    assert da.detect_swap_activity(state_path=state_path) is True


def test_detect_swap_activity_fails_closed_on_counters_moving_backwards(monkeypatch, tmp_path):
    """A reboot or counter reset makes the delta meaningless — must never be
    read as 'negative movement therefore cold'."""
    state_path = tmp_path / "swap_activity.json"
    state_path.write_text(json.dumps({"pswpin": 5000, "pswpout": 6000, "sampled_at": 1_000_000.0}))
    monkeypatch.setattr(da, "_read_vmstat_swap_counters", lambda: (100, 200))  # counters lower than the prior sample
    monkeypatch.setattr(time, "time", lambda: 1_000_060.0)
    assert da.detect_swap_activity(state_path=state_path) is True


def test_read_resource_vector_includes_swap_active_field(tmp_path, monkeypatch):
    """Smoke test against the real host, but with an isolated state path so
    it never touches the real durable sample file."""
    monkeypatch.setattr(da, "SWAP_ACTIVITY_STATE_PATH", tmp_path / "swap_activity.json")
    v = da.read_resource_vector()
    assert isinstance(v.swap_active, bool)


# ---- Founder-requested case matrix (2026-09-05) ----------------------------
# A-H exactly as specified: swap-cold+healthy admits (bounded); thrashing,
# low-RAM, elevated-PSI, and OOM-evidence all independently block; ambiguous
# telemetry defers (fails closed); concurrency stays bounded; and the actual
# real host state (including a killed background poller) is proven NOT to
# be falsely classified as safe merely because nominal free RAM exists.

def test_case_A_cold_swap_healthy_ram_low_psi_admits_bounded():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=8700, psi_memory_avg10=0.1, oom_kill_recent=False)
    allowed, _ = da.admission_decision(v)
    assert allowed is True
    concurrency = da.dynamic_safe_concurrency(v, per_worker_mem_mb=64)
    assert 0 < concurrency <= da.COLD_SWAP_BOUNDED_CONCURRENCY_CAP


def test_case_B_swap_thrashing_blocks_regardless_of_everything_else():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=True,
                mem_available_mb=64000, psi_memory_avg10=0.0, oom_kill_recent=False)
    allowed, _ = da.admission_decision(v)
    assert allowed is False
    assert da.dynamic_safe_concurrency(v) == 0


def test_case_C_low_available_ram_blocks():
    v = _vector(mem_available_mb=500)  # swap not even full here — independent gate
    allowed, reason = da.admission_decision(v)
    assert allowed is False
    assert "memory" in reason


def test_case_D_elevated_psi_blocks_the_cold_swap_override():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=8700, psi_memory_avg10=25.0, oom_kill_recent=False)
    allowed, reason = da.admission_decision(v)
    assert allowed is False
    assert da.dynamic_safe_concurrency(v) == 0
    # DIRECT_PSI_OOM_GATE (2026-09-08): admission_decision() now checks PSI
    # directly, upfront, before ever reaching the swap-triggered override
    # branch this test originally exercised — same real signal, a more
    # precise refusal reason, still refused.
    assert "memory pressure (PSI" in reason
    assert "25.0%" in reason


def test_direct_psi_gate_refuses_admission_even_with_no_swap_configured(monkeypatch):
    """DIRECT_PSI_OOM_GATE (2026-09-08): real, live incident this closes —
    node_capability_registry_state's real render-forge-01 record has
    swap_total_mb==0.0 (no swap configured at all), so it could never reach
    _cold_swap_override_eligible()'s own PSI/OOM check. Despite reporting
    20GB 'available' memory, its real psi_memory_avg10 was 100.0 and
    oom_kill_recent was True — admission_decision() admitted it anyway
    before this fix. This is the regression guard, using that exact real
    shape (swapless host, huge nominal headroom, saturated PSI + real OOM)."""
    v = _vector(swap_total_mb=0.0, swap_used_mb=0.0, mem_available_mb=20363.0,
                psi_memory_avg10=100.0, oom_kill_recent=True)
    allowed, reason = da.admission_decision(v)
    assert allowed is False
    assert "OOM-kill" in reason


def test_direct_psi_gate_healthy_swapless_host_still_admitted():
    v = _vector(swap_total_mb=0.0, swap_used_mb=0.0, mem_available_mb=20363.0,
                psi_memory_avg10=0.0, oom_kill_recent=False)
    allowed, reason = da.admission_decision(v)
    assert allowed is True, reason


def test_case_E_recent_oom_kill_blocks_even_with_everything_else_healthy():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=64000, psi_memory_avg10=0.0, oom_kill_recent=True)
    allowed, _ = da.admission_decision(v)
    assert allowed is False
    assert da.dynamic_safe_concurrency(v) == 0


def test_case_F_ambiguous_telemetry_fails_closed_to_defer():
    """Unreadable PSI/OOM/swap-activity evidence must never be treated as
    'therefore safe' — every relevant field's dataclass default is the
    conservative/unsafe value, proven directly here with a vector built
    from ONLY the fields admission cares about, omitting the rest."""
    ambiguous = da.ResourceVector(
        cpu_count=8, load_avg_1m=1.0, mem_total_mb=16000, mem_available_mb=8700,
        swap_total_mb=2000, swap_used_mb=2000,
        disk_total_gb=300, disk_free_gb=150, disk_used_pct=50.0,
    )  # swap_active, psi_memory_avg10, oom_kill_recent all left at their fail-closed defaults
    assert ambiguous.swap_active is True
    assert ambiguous.psi_memory_avg10 == 100.0
    assert ambiguous.oom_kill_recent is True
    allowed, _ = da.admission_decision(ambiguous)
    assert allowed is False
    assert da.dynamic_safe_concurrency(ambiguous) == 0


def test_case_G_concurrency_after_cold_admission_strictly_bounded_even_with_huge_headroom():
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=1_000_000, psi_memory_avg10=0.0, oom_kill_recent=False,
                cpu_count=256, load_avg_1m=0.0)
    concurrency = da.dynamic_safe_concurrency(v, per_worker_mem_mb=1)
    assert concurrency == da.COLD_SWAP_BOUNDED_CONCURRENCY_CAP  # never scales past the explicit cap, no matter how favorable the math


def test_case_H_real_current_host_state_not_falsely_admitted(monkeypatch, tmp_path):
    """Reproduces the EXACT real evidence this session gathered: swap
    100%, but with an unfavorable PSI/OOM read (simulating the moment this
    campaign's own background poller was killed) — must still refuse, not
    be waved through merely because mem_available_mb looks fine."""
    v = _vector(swap_total_mb=2000, swap_used_mb=2000, swap_active=False,
                mem_available_mb=8700, psi_memory_avg10=0.0, oom_kill_recent=True)
    allowed, _ = da.admission_decision(v)
    assert allowed is False, "a recent memory-kill signal must block admission even when RAM looks nominally available"


def test_read_psi_memory_avg10_smoke_on_real_host():
    """Real, read-only /proc/pressure/memory parse — this host's own PSI
    value happened to be genuinely low when this campaign checked it."""
    value = da._read_psi_memory_avg10()
    assert 0.0 <= value <= 100.0


def test_detect_recent_oom_kill_smoke_on_real_host():
    """Real, bounded, read-only journalctl -k check — must return a bool,
    never raise, regardless of what the real host's kernel log contains."""
    result = da._detect_recent_oom_kill(lookback_s=60)
    assert isinstance(result, bool)
