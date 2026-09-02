"""Tests for the submit_work source_paths schema-propagation fix (2026-09-02):
Work-facing MCP schema -> tools.submit_work -> Proposal creation. The downstream
half of the pipeline (Proposal.source_paths -> advance.py -> omniengineer_harness
-> authority_policy.classify()/context_staging.py) already existed and is NOT
re-implemented here -- these tests prove the propagation into it, and that the
pre-existing downstream gates (GATED_PATH_MARKERS, context_staging exclusions)
are unchanged and still real.

Creates real Proposal objects on disk (the same live pipeline every other caller
uses -- this codebase's own convention, see mrsilent_bridge/tests/
test_th3_omni_router_integration.py) and cleans each one up to REJECTED afterward."""
import sys
from pathlib import Path

ROOT = Path("/opt/pulse5-core")
sys.path.insert(0, str(ROOT / "services" / "th3_mcp"))
sys.path.insert(0, str(ROOT / "mrsilent_bridge"))

import pytest
import tools as T
import mcp_core
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
    kwargs.setdefault("objective", "TEST-ONLY: source_paths propagation test, safe to reject")
    kwargs.setdefault("task_type", "code_edit")
    kwargs.setdefault("idempotency_key", f"test-{id(kwargs)}-{kwargs['objective']}")
    result = T.submit_work(**kwargs)
    if result.get("accepted") and result.get("work_id"):
        cleanup_proposals.append(result["work_id"])
    return result


# --- path normalization (real bug found via live canary, see tools.py's
#     _normalize_source_path docstring) ---------------------------------------

def test_relative_source_path_normalized_to_absolute_under_root():
    normalized = T._normalize_source_path("mrsilent_bridge/context_staging.py")
    assert normalized == "/opt/pulse5-core/mrsilent_bridge/context_staging.py"
    assert Path(normalized).is_file()


def test_already_absolute_source_path_passed_through_unchanged():
    assert T._normalize_source_path("/opt/pulse5-core/mrsilent_bridge/context_staging.py") == "/opt/pulse5-core/mrsilent_bridge/context_staging.py"


def test_normalized_path_resolves_correctly_from_a_different_cwd(tmp_path, monkeypatch):
    # Reproduces the exact real bug: mrsilent-autonomous-cycle.service's cwd is
    # /opt/pulse5-core/mrsilent_bridge, not /opt/pulse5-core, so a bare relative
    # path silently resolved wrong there. Normalizing in tools.py must make the
    # result cwd-independent.
    import os
    original_cwd = os.getcwd()
    try:
        os.chdir("/opt/pulse5-core/mrsilent_bridge")
        normalized = T._normalize_source_path("mrsilent_bridge/context_staging.py")
        assert Path(normalized).is_file()  # true regardless of the calling process's cwd
    finally:
        os.chdir(original_cwd)


def test_source_paths_in_proposal_are_absolute(cleanup_proposals):
    result = _submit(cleanup_proposals, objective="TEST-ONLY: normalized paths land absolute on the Proposal", source_paths=["mrsilent_bridge/context_staging.py"])
    p = proposal_mod.load(result["work_id"])
    assert all(Path(sp).is_absolute() for sp in p.source_paths)
    assert p.source_paths == ["/opt/pulse5-core/mrsilent_bridge/context_staging.py"]


# --- schema-level ---------------------------------------------------------------

def test_work_facing_mcp_schema_exposes_source_paths():
    resp = mcp_core.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    submit_spec = next(t for t in resp["result"]["tools"] if t["name"] == "submit_work")
    assert "source_paths" in submit_spec["inputSchema"]["properties"]
    prop = submit_spec["inputSchema"]["properties"]["source_paths"]
    assert prop["type"] == "array"
    assert prop["maxItems"] == 10


def test_work_result_schema_mentions_source_paths_in_description():
    resp = mcp_core.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    work_result_spec = next(t for t in resp["result"]["tools"] if t["name"] == "work_result")
    assert "source_paths" in work_result_spec["description"]


# --- propagation: MCP call -> tools.submit_work -> Proposal ---------------------

def test_source_paths_survive_mcp_to_proposal(cleanup_proposals):
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "submit_work", "arguments": {
            "objective": "TEST-ONLY: authorized source_paths propagation",
            "task_type": "code_edit",
            "source_paths": ["mrsilent_bridge/context_staging.py"],
        }},
    }
    resp = mcp_core.handle_request(msg)
    assert resp["result"]["isError"] is False
    import json
    result = json.loads(resp["result"]["content"][0]["text"])
    cleanup_proposals.append(result["work_id"])

    p = proposal_mod.load(result["work_id"])
    assert p.source_paths == ["/opt/pulse5-core/mrsilent_bridge/context_staging.py"]


def test_default_empty_source_paths_unchanged_for_existing_callers(cleanup_proposals):
    # No source_paths passed at all -- must behave byte-for-byte as before.
    result = _submit(cleanup_proposals, objective="TEST-ONLY: no source_paths, legacy call shape")
    assert result["accepted"] is True
    p = proposal_mod.load(result["work_id"])
    assert p.source_paths == []


# --- bounding ---------------------------------------------------------------------

def test_source_paths_capped_at_ten(cleanup_proposals):
    many_paths = [f"mrsilent_bridge/fake_path_{i}.py" for i in range(25)]
    result = _submit(cleanup_proposals, objective="TEST-ONLY: over-limit source_paths list", source_paths=many_paths)
    assert len(result["routing"]["source_paths"]) == 10
    p = proposal_mod.load(result["work_id"])
    assert len(p.source_paths) == 10


def test_source_path_string_truncated_at_500_chars(cleanup_proposals):
    long_path = "a" * 900
    result = _submit(cleanup_proposals, objective="TEST-ONLY: overlong source_path string", source_paths=[long_path])
    assert len(result["routing"]["source_paths"][0]) == 500


# --- authorized source staging (downstream, pre-existing, unchanged) ------------

def test_authorized_source_staging_allows_real_repo_file():
    sys.path.insert(0, str(ROOT / "mrsilent_bridge"))
    import context_staging
    allowed, excluded = context_staging.stage_context_source_paths([Path("mrsilent_bridge/context_staging.py")])
    assert allowed == [Path("mrsilent_bridge/context_staging.py")]
    assert excluded == []


def test_manual_candidates_blocked_at_staging(cleanup_proposals):
    import context_staging
    allowed, excluded = context_staging.stage_context_source_paths([Path("mrsilent_bridge/manual_candidates/x.py")])
    assert allowed == []
    assert excluded == [{"path": "mrsilent_bridge/manual_candidates/x.py", "excluded_marker": "manual_candidates"}]


def test_secrets_marker_blocked_at_staging():
    import context_staging
    allowed, excluded = context_staging.stage_context_source_paths([Path("some/secrets/api_key.txt")])
    assert allowed == []
    assert excluded[0]["excluded_marker"] == "secrets"


# --- unauthorized / gated path rejection (first pass, tools.py level) -----------

def test_unauthorized_gated_path_marker_rejected_first_pass(cleanup_proposals):
    # "containerd" is a GATED_PATH_MARKERS entry but not a GATED_KEYWORDS regex
    # match, so this specifically exercises the new source_paths -> path-marker
    # check added in _first_pass_classify (rather than the pre-existing keyword scan).
    result = _submit(
        cleanup_proposals, objective="TEST-ONLY: gated path marker in source_paths",
        source_paths=["some/containerd/config.json"],
    )
    assert result["classification"] == "founder_gated"
    assert any("protected marker" in r for r in result["classification_reasons"])


def test_gated_keyword_in_source_path_rejected_first_pass(cleanup_proposals):
    # authority_policy.GATED_KEYWORDS matches free text, including source_paths
    # now that they're folded into the combined_text scan.
    result = _submit(
        cleanup_proposals, objective="TEST-ONLY: gated keyword via source_paths text",
        source_paths=["path/containing/rm -rf/marker"],
    )
    assert result["classification"] == "founder_gated"


# --- paid_resources_allowed hard gate (unaffected by source_paths) --------------

def test_paid_resource_gate_preserved_alongside_source_paths(cleanup_proposals):
    result = _submit(
        cleanup_proposals, objective="TEST-ONLY: paid gate still enforced with source_paths present",
        source_paths=["mrsilent_bridge/context_staging.py"], paid_resources_allowed=False,
    )
    assert result.get("accepted") in (True, False)
    if result.get("accepted"):
        p = proposal_mod.load(result["work_id"])
        assert p.paid_resources_allowed is False


# --- production promotion never occurs from this path ---------------------------

def test_submit_work_never_promotes_or_executes(cleanup_proposals):
    result = _submit(cleanup_proposals, objective="TEST-ONLY: submit_work must never promote/execute")
    assert result["accepted"] is True
    p = proposal_mod.load(result["work_id"])
    assert p.status not in (proposal_mod.ProposalStatus.PROMOTED,)
    assert result["execution_path"] != "promoted"


# --- work_result surfaces source_paths -------------------------------------------

def test_work_result_surfaces_source_paths(cleanup_proposals):
    result = _submit(cleanup_proposals, objective="TEST-ONLY: work_result source_paths visibility", source_paths=["mrsilent_bridge/context_staging.py"])
    wr = T.work_result(result["work_id"])
    assert wr["source_paths"] == ["/opt/pulse5-core/mrsilent_bridge/context_staging.py"]
