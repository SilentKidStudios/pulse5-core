"""Tests for the SECRET / CREDENTIAL SOURCE-STAGING HARDENING campaign
(2026-09-02), continuing from the source_paths schema-propagation fix
(commit b19c67c).

Uses only SYNTHETIC fixture paths for secret-like locations -- these paths are
never read, and several point at files/directories that don't even need to
exist (is_file()/is_dir() naturally returns False for a nonexistent path,
which is a safe, harmless outcome, not a test bug). No secret file content is
read, printed, hashed, copied, or otherwise inspected by any test here."""
import sys
from pathlib import Path

ROOT = Path("/opt/pulse5-core")
sys.path.insert(0, str(ROOT / "services" / "th3_mcp"))
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))

import uuid

import pytest
import tools as T
import mcp_core
import authority_policy
import context_staging
import secret_path_policy
from evolution import proposal as proposal_mod


@pytest.fixture
def cleanup_proposals():
    created = []
    yield created
    for work_id in created:
        try:
            p = proposal_mod.load(work_id)
            if p.status not in proposal_mod.CLOSED_STATUSES:
                proposal_mod.advance(work_id, proposal_mod.ProposalStatus.REJECTED, note="test cleanup")
        except Exception:
            pass


def _submit(cleanup_proposals, **kwargs):
    kwargs.setdefault("objective", "TEST-ONLY: path staging hardening regression check, safe to reject")
    kwargs.setdefault("task_type", "code_edit")
    kwargs.setdefault("idempotency_key", f"secret-test-{uuid.uuid4()}")
    result = T.submit_work(**kwargs)
    if result.get("accepted") and result.get("work_id"):
        cleanup_proposals.append(result["work_id"])
    return result


# --- SECURE_KEYS_SUBMISSION_BLOCKED ---------------------------------------------

def test_secure_keys_submission_blocked_relative(cleanup_proposals):
    result = _submit(cleanup_proposals, source_paths=["secure_keys/anthropic.key"])
    assert result["classification"] == "founder_gated"


def test_secure_keys_submission_blocked_absolute(cleanup_proposals):
    result = _submit(cleanup_proposals, source_paths=["/opt/pulse5-core/secure_keys/anthropic.key"])
    assert result["classification"] == "founder_gated"


# --- SECURE_KEYS_CONTEXT_STAGING_BLOCKED ----------------------------------------

def test_secure_keys_context_staging_blocked():
    allowed, excluded = context_staging.stage_context_source_paths([Path("secure_keys/anthropic.key")])
    assert allowed == []
    assert len(excluded) == 1
    assert excluded[0]["path"] == "secure_keys/anthropic.key"
    # never assert on / print the secret's contents -- only the exclusion marker


# --- ABSOLUTE / RELATIVE variants -----------------------------------------------

def test_absolute_secure_keys_path_blocked_by_authority_policy():
    decision = authority_policy.classify(
        task_description="", requested_tools=set(),
        sandbox_root=ROOT / "mrsilent_bridge" / "jobs" / "fake-test-job" / "workdir",
        source_paths=[Path("/opt/pulse5-core/secure_keys/th3_mcp_http_bearer.key")],
    )
    assert decision.risk_class == authority_policy.RiskClass.FOUNDER_GATED


def test_relative_secure_keys_path_blocked_by_authority_policy():
    decision = authority_policy.classify(
        task_description="", requested_tools=set(),
        sandbox_root=ROOT / "mrsilent_bridge" / "jobs" / "fake-test-job" / "workdir",
        source_paths=[Path("secure_keys/th3_mcp_http_bearer.key")],
    )
    assert decision.risk_class == authority_policy.RiskClass.FOUNDER_GATED


# --- NORMALIZED_TRAVERSAL_TO_SECURE_KEYS_BLOCKED --------------------------------

def test_traversal_to_secure_keys_blocked_via_normalize_source_path():
    # A path that does NOT literally contain "secure_keys" as a naive substring
    # in its raw form is still caught because _normalize_source_path resolves
    # '..' before any marker check runs.
    normalized = T._normalize_source_path("mrsilent_bridge/../secure_keys/anthropic.key")
    assert normalized == "/opt/pulse5-core/secure_keys/anthropic.key"
    assert "secure_keys" in normalized  # confirms resolution collapsed the traversal


def test_traversal_submit_work_still_blocked(cleanup_proposals):
    result = _submit(cleanup_proposals, source_paths=["mrsilent_bridge/../secure_keys/anthropic.key"])
    assert result["classification"] == "founder_gated"


def test_traversal_out_of_root_blocked(cleanup_proposals):
    # Escapes the repo entirely via traversal -- must be rejected by the
    # ROOT-boundary check even though it names no secret marker at all.
    result = _submit(cleanup_proposals, source_paths=["mrsilent_bridge/../../etc/hostname"])
    assert result["classification"] == "founder_gated"
    assert any("outside the canonical repository root" in r for r in result["classification_reasons"])


def test_symlink_env_central_resolves_outside_root_and_is_blocked():
    # .env.central is a real symlink -> /etc/pulse5.env (outside ROOT).
    # Confirmed via path metadata only (readlink), never its target's content.
    import os
    target = os.readlink(ROOT / ".env.central") if (ROOT / ".env.central").is_symlink() else None
    if target is None:
        pytest.skip(".env.central is not a symlink in this environment")
    resolved = secret_path_policy.resolve_path(ROOT / ".env.central")
    assert not secret_path_policy.is_within_root(resolved)


# --- OTHER_CONFIRMED_SECRET_PATHS_BLOCKED ---------------------------------------

@pytest.mark.parametrize("secret_path", [
    "secrets/openai_api_key.txt",
    "secure/cloudflare.env",
    "services/th3_mcp/oauth_state/tokens.json",
    "omniscraper/founder_session_bridge/whatever.json",
    "omniscraper/social_cookie_vault/whatever.json",
    ".config/doctl/config.yaml",
    "client_secret.json",
    "config/ops_console_token.txt",
    "config/mr_silent_auth.json",
])
def test_other_confirmed_secret_paths_blocked_at_staging(secret_path):
    allowed, excluded = context_staging.stage_context_source_paths([Path(secret_path)])
    assert allowed == [], f"{secret_path} should have been excluded but was allowed"
    assert len(excluded) == 1


@pytest.mark.parametrize("secret_path", [
    "secrets/openai_api_key.txt",
    "secure/cloudflare.env",
    "services/th3_mcp/oauth_state/tokens.json",
    "omniscraper/founder_session_bridge/whatever.json",
    "omniscraper/social_cookie_vault/whatever.json",
])
def test_other_confirmed_secret_paths_blocked_at_submit(cleanup_proposals, secret_path):
    result = _submit(cleanup_proposals, source_paths=[secret_path])
    assert result["classification"] == "founder_gated"


# --- SECRET_FILE_CONTENT_NEVER_READ_BY_TEST -------------------------------------

def test_secret_file_content_never_read_by_this_suite(monkeypatch):
    # Defense-in-depth self-check: patch builtins.open to fail if anything in
    # this process tries to literally open a real secret file's content while
    # exercising the policy functions used above.
    import builtins
    real_open = builtins.open

    def guarded_open(file, *a, **k):
        s = str(file)
        if "secure_keys" in s or ("/secrets/" in s and "test" not in s.lower()):
            raise AssertionError(f"a test attempted to open a real secret path: {s}")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    # Exercise the policy path (not file I/O) -- proves classify()/staging
    # never open() the target, only stat/resolve it.
    authority_policy.classify(
        task_description="", requested_tools=set(),
        sandbox_root=ROOT / "mrsilent_bridge" / "jobs" / "fake-test-job" / "workdir",
        source_paths=[Path("/opt/pulse5-core/secure_keys/anthropic.key")],
    )
    context_staging.stage_context_source_paths([Path("/opt/pulse5-core/secure_keys/anthropic.key")])


# --- MANUAL_CANDIDATES_STILL_BLOCKED --------------------------------------------

def test_manual_candidates_still_blocked_at_staging():
    allowed, excluded = context_staging.stage_context_source_paths([Path("mrsilent_bridge/manual_candidates/x.py")])
    assert allowed == []
    assert excluded[0]["excluded_marker"] == "manual_candidates"


# --- NORMAL_AUTHORIZED_SOURCE_STILL_STAGES --------------------------------------

def test_normal_authorized_source_still_allowed_at_staging():
    allowed, excluded = context_staging.stage_context_source_paths([Path("mrsilent_bridge/context_staging.py")])
    assert allowed == [Path("mrsilent_bridge/context_staging.py")]
    assert excluded == []


def test_normal_authorized_source_still_submits_low_risk(cleanup_proposals):
    result = _submit(cleanup_proposals, source_paths=["mrsilent_bridge/context_staging.py"])
    assert result["classification"] == "low"
    assert result["accepted"] is True


# --- SOURCE_PATHS_WORK_FACING_SCHEMA_STILL_PASS / MCP_TO_PROPOSAL_STILL_PASS ----

def test_work_facing_schema_still_has_source_paths():
    resp = mcp_core.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    submit_spec = next(t for t in resp["result"]["tools"] if t["name"] == "submit_work")
    assert "source_paths" in submit_spec["inputSchema"]["properties"]


def test_source_paths_still_survive_mcp_to_proposal(cleanup_proposals):
    import json
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "submit_work", "arguments": {
            "objective": "TEST-ONLY: hardened source_paths still propagate",
            "task_type": "code_edit",
            "source_paths": ["mrsilent_bridge/context_staging.py"],
        }},
    }
    resp = mcp_core.handle_request(msg)
    assert resp["result"]["isError"] is False
    result = json.loads(resp["result"]["content"][0]["text"])
    cleanup_proposals.append(result["work_id"])
    p = proposal_mod.load(result["work_id"])
    assert p.source_paths == ["/opt/pulse5-core/mrsilent_bridge/context_staging.py"]


# --- PAID_RESOURCE_GATE_PRESERVED -----------------------------------------------

def test_paid_resource_gate_preserved_alongside_secret_path_hardening(cleanup_proposals):
    # A blocked secret source_path and paid_resources_allowed=False must both
    # independently hold -- neither weakens the other.
    result = _submit(cleanup_proposals, source_paths=["secure_keys/anthropic.key"], paid_resources_allowed=False)
    assert result["classification"] == "founder_gated"
    result2 = _submit(cleanup_proposals, source_paths=["mrsilent_bridge/context_staging.py"], paid_resources_allowed=False)
    if result2.get("accepted"):
        p = proposal_mod.load(result2["work_id"])
        assert p.paid_resources_allowed is False


# --- PRODUCTION_PROMOTION_OCCURRED ----------------------------------------------

def test_no_proposal_from_this_suite_is_ever_promoted(cleanup_proposals):
    result = _submit(cleanup_proposals, source_paths=["mrsilent_bridge/context_staging.py"])
    if result.get("accepted"):
        p = proposal_mod.load(result["work_id"])
        assert p.status != proposal_mod.ProposalStatus.PROMOTED
