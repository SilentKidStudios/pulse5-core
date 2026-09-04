"""Tests for session_ownership.py — the campaign/session ownership registry
built during the WALK-AWAY CONVERGENCE gap-closure pass (2026-09-04).

Isolation: each test monkeypatches session_ownership.LEASES_DIR to a fresh
tmp_path, so these tests never read/write the real leases directory (which,
once this module has a real caller, will hold genuine session state) —
matching this project's own hard-learned lesson (see tests/_test_isolation.py)
that a shared production ledger must never be touched by test runs.
"""
from __future__ import annotations

import os

import session_ownership as so


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(so, "LEASES_DIR", tmp_path / "leases")


def test_register_creates_active_lease_with_owned_paths(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    lease = so.register("session-a", "test-campaign", ["mrsilent_bridge/evolution"])
    assert lease.status == "active"
    assert lease.owned_paths == ["mrsilent_bridge/evolution"]
    loaded = so.load("session-a")
    assert loaded is not None
    assert loaded.session_id == "session-a"


def test_register_is_idempotent_and_merges_paths(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("session-a", "test-campaign", ["path/one"])
    lease = so.register("session-a", "test-campaign", ["path/two"])
    assert set(lease.owned_paths) == {"path/one", "path/two"}
    assert len(so.list_all()) == 1  # updated in place, not duplicated


def test_path_level_collision_blocks_external_session(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("session-a", "campaign-a", ["mrsilent_bridge/evolution"])
    status, owner = so.classify_path_ownership("mrsilent_bridge/evolution/founder_request.py", "session-b")
    assert status == so.STATUS_BLOCKED_EXTERNAL
    assert owner == "session-a"


def test_path_level_collision_is_prefix_precise_not_global(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("session-a", "campaign-a", ["mrsilent_bridge/evolution"])
    # a sibling directory sharing only a common parent must NOT be blocked —
    # collision detection is path-prefix precise, not "any lease exists".
    status, owner = so.classify_path_ownership("mrsilent_bridge/session_ownership_state", "session-b")
    assert status == so.STATUS_AVAILABLE
    assert owner is None


def test_self_session_sees_its_own_lease_as_owned_not_blocked(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("session-a", "campaign-a", ["some/path"])
    status, owner = so.classify_path_ownership("some/path/file.py", "session-a")
    assert status == so.STATUS_OWNED_BY_SELF
    assert owner == "session-a"


def test_real_collision_fixture_walkaway_final_gap(monkeypatch, tmp_path):
    """Reconstructs the actual collision this campaign's read-only audit
    discovered live (pulse5-core-9e, tmux "mrsilent-walkaway-final-gap",
    founder_request.py modified-but-uncommitted) as a concrete regression
    fixture: an overlapping path is blocked, an unrelated path stays
    schedulable, and the block lifts naturally once ownership clears —
    exactly the acceptance evidence the campaign asked for."""
    _fresh(monkeypatch, tmp_path)
    so.register(
        "pulse5-core-9e", "mrsilent-walkaway-final-gap",
        ["mrsilent_bridge/evolution/founder_request.py",
         "mr_silent_spine/walkaway_governance/ledger"],
    )

    overlapping, owner = so.classify_path_ownership(
        "mrsilent_bridge/evolution/founder_request.py", "this-session")
    assert overlapping == so.STATUS_BLOCKED_EXTERNAL
    assert owner == "pulse5-core-9e"

    unrelated, _ = so.classify_path_ownership(
        "mrsilent_bridge/session_ownership.py", "this-session")
    assert unrelated == so.STATUS_AVAILABLE  # unrelated work keeps moving

    # ownership clears (session released explicitly) -> blocked branch
    # becomes eligible again naturally, no manual override needed.
    so.release("pulse5-core-9e")
    cleared, _ = so.classify_path_ownership(
        "mrsilent_bridge/evolution/founder_request.py", "this-session")
    assert cleared == so.STATUS_AVAILABLE


def test_stale_lease_is_detected_and_recovered(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    lease = so.register("session-dead", "campaign-x", ["some/path"], )
    # simulate a dead owner: a pid that cannot possibly be alive, on this host
    lease.pid = _unused_pid()
    lease.hostname = os.uname().nodename if hasattr(os, "uname") else lease.hostname
    import json
    p = so._lease_path("session-dead")
    from dataclasses import asdict
    p.write_text(json.dumps(asdict(lease)))

    swept = so.sweep_stale()
    assert "session-dead" in swept
    status, _ = so.classify_path_ownership("some/path/file.py", "session-other")
    assert status == so.STATUS_AVAILABLE  # dead session's claim no longer blocks


def test_stale_lease_from_other_host_falls_back_to_heartbeat_age(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    lease = so.register("remote-session", "campaign-y", ["remote/path"])
    lease.hostname = "some-other-host-entirely"
    lease.heartbeat = "2000-01-01T00:00:00+00:00"  # ancient
    import json
    from dataclasses import asdict
    so._lease_path("remote-session").write_text(json.dumps(asdict(lease)))

    assert so.is_stale(lease) is True


def test_read_only_agent_cannot_mutate(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("audit-fork", "read-only-campaign", ["some/path"], agent_mode=so.AGENT_MODE_READ_ONLY)
    try:
        so.assert_mutation_allowed("audit-fork", "some/path/file.py")
        assert False, "expected MutationNotAllowed"
    except so.MutationNotAllowed:
        pass


def test_read_write_agent_may_mutate(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("worker", "rw-campaign", ["some/path"], agent_mode=so.AGENT_MODE_READ_WRITE)
    so.assert_mutation_allowed("worker", "some/path/file.py")  # must not raise


def test_unregistered_session_is_unrestricted_by_default(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.assert_mutation_allowed("never-registered", "any/path")  # must not raise


def test_duplicate_campaign_work_suppression_by_fingerprint(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    so.register("session-a", "campaign-a", ["path/a"], fingerprint="objective-hash-123")
    found = so.find_active_lease_by_fingerprint("objective-hash-123")
    assert found is not None
    assert found.session_id == "session-a"
    assert so.find_active_lease_by_fingerprint("no-such-hash") is None


def test_released_lease_never_blocks_and_is_not_stale(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    lease = so.register("session-a", "campaign-a", ["some/path"])
    so.release("session-a")
    assert so.is_stale(lease) is False  # released is a distinct terminal state, not "stale"
    status, _ = so.classify_path_ownership("some/path/file.py", "session-b")
    assert status == so.STATUS_AVAILABLE


def _unused_pid() -> int:
    """A pid essentially guaranteed not to be alive on this host, for
    deterministic staleness tests without relying on timing/sleep."""
    candidate = 2_000_000
    while _pid_exists(candidate):
        candidate += 1
    return candidate


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
