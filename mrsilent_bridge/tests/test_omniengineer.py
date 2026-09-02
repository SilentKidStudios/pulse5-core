#!/usr/bin/env python3
"""
Tests for OmniEngineer V0.1 (omniengineer_agent.py + omniengineer_harness.py).

Two tiers, same style as test_studio_router.py (plain script, no pytest dep):
  - tool-contract unit tests: fast, deterministic, no network call — exercise
    every sandbox-jailing rule directly (path escape, protected markers,
    ALLOWED_BINARIES, GATED_KEYWORDS, unique-match patching).
  - integration tests: genuinely call the live Ollama server (same one
    local_model_bridge.py already talks to) and run the real bounded ReAct
    loop end-to-end — not mocked, consistent with this project's standing
    practice of proving new adapters against the real thing at least once
    before trusting them (see local_model_bridge.py, codex_bridge.py).

Run: python3 tests/test_omniengineer.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# TEST_CIRCUIT_STATE_ISOLATION_V1
import tempfile as _mrsilent_circuit_tempfile
from pathlib import Path as _MrsilentCircuitPath
import sys as _mrsilent_circuit_sys

# This isolation block may appear before the test module's normal project
# path bootstrap. Establish the same bridge root here so importing the
# production health module for TEST-ONLY state redirection is deterministic.
_MRSILENT_TEST_BRIDGE_ROOT = _MrsilentCircuitPath(__file__).resolve().parents[1]
if str(_MRSILENT_TEST_BRIDGE_ROOT) not in _mrsilent_circuit_sys.path:
    _mrsilent_circuit_sys.path.insert(
        0,
        str(_MRSILENT_TEST_BRIDGE_ROOT),
    )

import local_model_health as _mrsilent_test_local_model_health
_MRSILENT_TEST_CIRCUIT_TMPDIR = _mrsilent_circuit_tempfile.TemporaryDirectory(
    prefix="mrsilent_test_circuit_"
)
_mrsilent_test_local_model_health.CIRCUIT_STATE_FILE = (
    _MrsilentCircuitPath(_MRSILENT_TEST_CIRCUIT_TMPDIR.name)
    / "provider_circuit_breaker.json"
)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_ledger
import omniengineer_agent as agent
import omniengineer_harness as harness
import promotion

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _tmp_sandbox() -> Path:
    d = Path(tempfile.mkdtemp(prefix="omniengineer_test_"))
    return d


# ---- tool-contract unit tests (no network) ----------------------------------

def test_system_prompt_matches_seeded_source_guard_v2() -> None:
    """Model-facing write semantics must match the Phase-2 seeded-file guard."""
    prompt = agent.SYSTEM_PROMPT

    check(
        "seeded tool contract no longer advertises unrestricted whole-file overwrite",
        "create or fully overwrite a file" not in prompt,
        prompt,
    )

    check(
        "seeded tool contract explicitly says seeded files cannot be whole-file replaced",
        "seeded file that existed at agent-run start cannot be whole-file replaced" in prompt,
        prompt,
    )

    check(
        "seeded tool contract directs mature source edits to apply_patch_sandbox",
        "edit seeded mature source surgically with apply_patch_sandbox instead" in prompt,
        prompt,
    )


def test_write_and_read_roundtrip() -> None:
    sb = _tmp_sandbox()
    try:
        ok, _ = agent._tool_write_file_sandbox(sb, {"path": "a.txt", "content": "hello"}, {})
        check("write_file_sandbox writes inside sandbox", ok)
        ok, out = agent._tool_read_file(sb, {"path": "a.txt"})
        check("read_file reads back the same content", ok and '"hello"' in out, out)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_write_file_sandbox_rejects_path_escape() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_write_file_sandbox(sb, {"path": "../escape.txt", "content": "x"}, {})
        check("write_file_sandbox refuses a path that escapes the sandbox", not ok, detail)
        ok2, detail2 = agent._tool_write_file_sandbox(sb, {"path": "/etc/passwd", "content": "x"}, {})
        check("write_file_sandbox refuses an absolute path", not ok2, detail2)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_write_file_sandbox_rejects_protected_marker() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_write_file_sandbox(sb, {"path": "credentials/x.txt", "content": "x"}, {})
        check("write_file_sandbox refuses a path matching a protected marker", not ok, detail)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


# OMNI_REAL_SOURCE_SAFETY_V2_REGRESSIONS
def test_seeded_whole_file_replacement_is_blocked() -> None:
    sb = _tmp_sandbox()

    try:
        seeded = sb / "existing.py"
        seeded.write_text("value = 1\n")

        before = agent._snapshot(sb)

        ok, detail = agent._tool_write_file_sandbox(
            sb,
            {
                "path": "existing.py",
                "content": "value = 999\n",
            },
            before,
        )

        check(
            "seeded existing source cannot be wholesale replaced with write_file_sandbox",
            not ok,
            detail,
        )

        check(
            "refused seeded whole-file replacement leaves original content intact",
            seeded.read_text() == "value = 1\n",
            repr(seeded.read_text()),
        )

        ok_patch, patch_detail = agent._tool_apply_patch_sandbox(
            sb,
            {
                "path": "existing.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
            before,
        )

        check(
            "seeded source can still receive a surgical apply_patch_sandbox edit",
            ok_patch,
            patch_detail,
        )

        check(
            "surgical seeded-file patch produced the intended result",
            seeded.read_text() == "value = 2\n",
            repr(seeded.read_text()),
        )

    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_new_file_creation_remains_allowed_with_seeded_guard() -> None:
    sb = _tmp_sandbox()

    try:
        before = agent._snapshot(sb)

        ok, detail = agent._tool_write_file_sandbox(
            sb,
            {
                "path": "brand_new.py",
                "content": "created = True\n",
            },
            before,
        )

        check(
            "write_file_sandbox still creates genuinely new files",
            ok,
            detail,
        )

        check(
            "new-file content is preserved",
            (sb / "brand_new.py").read_text() == "created = True\n",
        )

    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_canonical_source_path_lineage_is_preserved() -> None:
    workdir = Path("/tmp/mrsilent_lineage_proof")

    canonical_source = (
        harness.BRIDGE_ROOT
        / "evolution"
        / "advance.py"
    )

    dest = harness._source_sandbox_destination(
        workdir,
        canonical_source,
    )

    expected = (
        workdir
        / "evolution"
        / "advance.py"
    )

    check(
        "canonical mature source keeps its project-relative directory lineage",
        dest == expected,
        f"dest={dest} expected={expected}",
    )

    external = Path("/tmp/external_project/example.py")

    external_dest = harness._source_sandbox_destination(
        workdir,
        external,
    )

    check(
        "external-source backward compatibility retains basename behavior",
        external_dest == workdir / "example.py",
        str(external_dest),
    )


def test_apply_patch_sandbox_create_new_file() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_apply_patch_sandbox(sb, {"path": "new.py", "old_string": "", "new_string": "x = 1\n"}, {})
        check("apply_patch_sandbox with old_string='' creates a new file", ok, detail)
        check("created file has the expected content", (sb / "new.py").read_text() == "x = 1\n")
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_apply_patch_sandbox_requires_unique_match() -> None:
    sb = _tmp_sandbox()
    try:
        (sb / "f.py").write_text("value = 1\nvalue = 1\n")
        ok, detail = agent._tool_apply_patch_sandbox(sb, {"path": "f.py", "old_string": "value = 1", "new_string": "value = 2"}, {})
        check("apply_patch_sandbox refuses an ambiguous (non-unique) old_string", not ok and "2 times" in detail, detail)

        (sb / "g.py").write_text("value = 1\n")
        ok2, detail2 = agent._tool_apply_patch_sandbox(sb, {"path": "g.py", "old_string": "value = 1", "new_string": "value = 2"}, {})
        check("apply_patch_sandbox applies a unique match", ok2, detail2)
        check("patched content is correct", (sb / "g.py").read_text() == "value = 2\n")

        ok3, detail3 = agent._tool_apply_patch_sandbox(sb, {"path": "g.py", "old_string": "not_present", "new_string": "x"}, {})
        check("apply_patch_sandbox refuses when old_string is not found", not ok3, detail3)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_run_command_rejects_disallowed_binary() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_run_command(sb, {"argv": ["curl", "http://example.com"]}, [])
        check("run_command refuses a binary outside ALLOWED_BINARIES", not ok and "ALLOWED_BINARIES" in detail, detail)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_run_command_rejects_gated_keyword() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_run_command(sb, {"argv": ["bash", "-c", "rm -rf /"]}, [])
        check("run_command refuses a command matching a GATED_KEYWORDS pattern", not ok, detail)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_run_command_allows_python3_and_bash() -> None:
    sb = _tmp_sandbox()
    try:
        ok, detail = agent._tool_run_command(sb, {"argv": ["python3", "-c", "print(1+1)"]}, [])
        check("run_command allows python3", ok, detail)
        ok2, detail2 = agent._tool_run_command(sb, {"argv": ["bash", "-c", "echo hi"]}, [])
        check("run_command allows bash", ok2, detail2)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_grep_and_list_files() -> None:
    sb = _tmp_sandbox()
    try:
        (sb / "sub").mkdir()
        (sb / "sub" / "m.py").write_text("def needle():\n    pass\n")
        ok, out = agent._tool_list_files(sb, {"path": "."})
        check("list_files finds a nested file", ok and "sub/m.py" in out, out)
        ok2, out2 = agent._tool_grep(sb, {"pattern": "needle", "path": "."})
        check("grep finds the pattern with correct file:line", ok2 and "sub/m.py:1:" in out2, out2)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_inspect_diff_reports_added_file() -> None:
    sb = _tmp_sandbox()
    try:
        before = agent._snapshot(sb)
        (sb / "new_file.txt").write_text("content")
        ok, out = agent._tool_inspect_diff(sb, {}, before)
        check("inspect_diff reports a newly added file", ok and "new_file.txt" in out, out)
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_parse_tool_call_rejects_unknown_tool() -> None:
    call = agent._parse_tool_call('{"thought": "x", "tool": "delete_everything", "args": {}}')
    check("_parse_tool_call rejects a tool name outside TOOL_NAMES", call is None)
    call2 = agent._parse_tool_call('not json at all')
    check("_parse_tool_call rejects malformed JSON instead of raising", call2 is None)
    call3 = agent._parse_tool_call('{"thought": "x", "tool": "finish", "args": {"summary": "done"}}')
    check("_parse_tool_call accepts a valid finish call", call3 is not None and call3.tool == "finish")


# OMNI_GOD_MODE_V1 -- deterministic tool-call repair (Phase 2). Regression
# target: job 6978adf2, where qwen3.6:27b never produced a valid tool call
# across all MAX_MALFORMED_RETRIES attempts on iteration 1. These prove the
# repair layer salvages common, unambiguous near-misses without ever
# fabricating a field value, and still correctly refuses anything genuinely
# ambiguous.
def test_parse_tool_call_repairs_prose_wrapped_json() -> None:
    raw = 'Sure, here is my tool call:\n{"thought": "read it", "tool": "read_file", "args": {"path": "x.py"}}\nLet me know if that works.'
    call = agent._parse_tool_call(raw)
    check("prose-wrapped JSON is repaired and parsed", call is not None and call.tool == "read_file" and call.args == {"path": "x.py"})


def test_parse_tool_call_repairs_trailing_comma() -> None:
    raw = '{"thought": "list", "tool": "list_files", "args": {"path": ".",},}'
    call = agent._parse_tool_call(raw)
    check("trailing comma before a closer is repaired and parsed", call is not None and call.tool == "list_files")


def test_parse_tool_call_repair_never_fabricates_values() -> None:
    raw = 'here is the call {"thought": "write", "tool": "write_file_sandbox", "args": {"path": "a.txt", "content": "exact, unaltered {value} with a brace"}} thanks'
    call = agent._parse_tool_call(raw)
    check("repair preserves field values exactly, including a brace inside a string",
          call is not None and call.args.get("content") == "exact, unaltered {value} with a brace")


def test_parse_tool_call_repair_still_refuses_unrepairable() -> None:
    call = agent._parse_tool_call("I think I should read the file but I'm not sure how to call the tool")
    check("genuinely unrepairable output (no JSON object at all) is still refused, not guessed", call is None)
    call2 = agent._parse_tool_call('{"thought": "x", "tool": "not_a_real_tool", "args": {}}')
    check("a well-formed object with an invalid tool name is still refused after repair", call2 is None)


def test_filter_actually_installed_excludes_configured_but_missing_model() -> None:
    # Regression target: job 6978adf2's real failure --
    # gpt-oss:20b was in engineering_failover_order() but had no Ollama
    # manifest at all.
    installed = ["qwen3-coder:30b", "qwen3.6:27b"]
    really, missing = harness._filter_actually_installed(
        ["qwen3-coder:30b", "qwen3.6:27b", "gpt-oss:20b"], installed,
    )
    check("actually-installed candidates are kept, in order", really == ["qwen3-coder:30b", "qwen3.6:27b"])
    check("gpt-oss:20b (configured but not installed) is excluded", missing == ["gpt-oss:20b"])


def test_filter_actually_installed_matches_base_name_without_exact_tag() -> None:
    really, missing = harness._filter_actually_installed(["qwen3-coder:30b"], ["qwen3-coder:latest"])
    check("a differently-tagged but same-base-name installed model still counts as installed",
          really == ["qwen3-coder:30b"] and missing == [])


def test_filter_actually_installed_empty_when_nothing_real() -> None:
    really, missing = harness._filter_actually_installed(["gpt-oss:20b"], ["qwen3-coder:30b"])
    check("candidate list is empty (not a crash) when nothing configured is actually installed",
          really == [] and missing == ["gpt-oss:20b"])


# ---- harness-level tests (no network needed) --------------------------------

def test_gated_task_is_rejected_without_running_agent() -> None:
    result = harness.submit_job(
        task="delete all the old audit logs",
        requested_by="test",
        founder_approved=False,
    )
    check("a GATED_KEYWORDS task is rejected_policy, not executed",
          result.status == "rejected_policy" and result.agent_final_action is None,
          f"status={result.status!r} agent_final_action={result.agent_final_action!r}")


# ---- OMNI_ENGINEER_REAL_SOURCE_REPAIR_PARITY (Founder-authorized 2026-08-20):
# source_paths / allowed_tools — no network needed, mocking only the model
# call layer (run_agent_loop / _call_ollama), exactly like test_cross_
# provider_failover.py's own established mocking pattern for this harness. --

def test_allowed_tools_structurally_refuses_a_disallowed_tool_call() -> None:
    """The real, structural enforcement (not a prompt suggestion): a tool
    call outside allowed_tools is refused at dispatch time, never reaching
    _execute_tool() — proven by the sandbox file genuinely never being
    created, not just by inspecting a status code."""
    sandbox = _tmp_sandbox()
    try:
        calls = iter([
            '{"thought": "try writing", "tool": "write_file_sandbox", "args": {"path": "x.py", "content": "x=1"}}',
            '{"thought": "cannot write, done", "tool": "finish", "args": {"summary": "read-only this run"}}',
        ])
        original_call = agent._call_ollama
        agent._call_ollama = lambda prompt, *, model, timeout_s: next(calls)
        try:
            result = agent.run_agent_loop(
                "synthetic read-only test task", sandbox,
                allowed_tools=frozenset({"list_files", "read_file", "grep", "inspect_diff"}),
            )
        finally:
            agent._call_ollama = original_call

        check("the disallowed write_file_sandbox call was refused, not executed", not (sandbox / "x.py").exists())
        first_turn = result.turns[0] if result.turns else {}
        check("the refusal was recorded as a failed turn for the disallowed tool",
              first_turn.get("tool") == "write_file_sandbox" and first_turn.get("ok") is False, first_turn)
        check("the refusal message explains why", "not granted" in first_turn.get("result_excerpt", ""), first_turn)
        check("the loop still reaches finish afterward (the model can adapt, not just crash)",
              result.final_action == "finish", result.final_action)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_allowed_tools_none_keeps_full_backward_compatible_access() -> None:
    """allowed_tools=None (the default, used by every pre-existing caller of
    run_agent_loop) must behave EXACTLY as before this parameter existed."""
    sandbox = _tmp_sandbox()
    try:
        calls = iter([
            '{"thought": "write it", "tool": "write_file_sandbox", "args": {"path": "y.py", "content": "y=1"}}',
            '{"thought": "done", "tool": "finish", "args": {"summary": "wrote y.py"}}',
        ])
        original_call = agent._call_ollama
        agent._call_ollama = lambda prompt, *, model, timeout_s: next(calls)
        try:
            result = agent.run_agent_loop("synthetic unrestricted test task", sandbox, allowed_tools=None)
        finally:
            agent._call_ollama = original_call
        check("with allowed_tools=None, write_file_sandbox is genuinely executed",
              (sandbox / "y.py").exists() and (sandbox / "y.py").read_text() == "y=1")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_harness_copies_authorized_source_paths_into_the_sandbox() -> None:
    """The exact same copy_source_paths mechanism/placement bridge.py uses:
    real file copied into the job's OWN isolated sandbox before the agent
    loop starts — the harness-level plumbing, mocking only the model call."""
    src_dir = Path(tempfile.mkdtemp(prefix="omniengineer_srcpaths_test_"))
    src_file = src_dir / "sample_source.py"
    src_file.write_text("def f():\n    return 1\n")
    original_run = harness.run_agent_loop
    captured: dict = {}

    def fake_run_agent_loop(task, sandbox, *, model, provider, max_iterations, plan_text, timeout_s, on_checkpoint, allowed_tools=None):
        captured["allowed_tools"] = allowed_tools
        captured["copied_content"] = (sandbox / "sample_source.py").read_text() if (sandbox / "sample_source.py").exists() else None
        return agent.AgentRunResult(task=task, sandbox=str(sandbox), model=model, provider=provider,
                                     final_action="finish", summary_or_reason="synthetic no-op — read only")

    harness.run_agent_loop = fake_run_agent_loop
    try:
        result = harness.submit_job(
            f"synthetic source_paths test {uuid.uuid4()}", requested_by="test",
            source_paths=[str(src_file)], allowed_tools=["list_files", "read_file"],
        )
    finally:
        harness.run_agent_loop = original_run
        shutil.rmtree(src_dir, ignore_errors=True)

    check("submit_job succeeds", result.status == "succeeded", result.status)
    check("the real source file was copied into the job's own sandbox before the agent loop ran",
          captured.get("copied_content") == "def f():\n    return 1\n")
    check("allowed_tools was forwarded through to run_agent_loop", captured.get("allowed_tools") == frozenset({"list_files", "read_file"}))


def test_harness_rejects_a_protected_source_path() -> None:
    """Reuses authority_policy.classify()'s existing GATED_PATH_MARKERS
    check unmodified — the exact same real check bridge.py's source_paths
    already goes through, now also live for omniengineer_harness.py."""
    result = harness.submit_job(
        f"synthetic protected-path test {uuid.uuid4()}", requested_by="test",
        source_paths=["/tmp/some_credentials_file.py"],
    )
    check("a source_path matching a protected marker is rejected_policy, not executed",
          result.status == "rejected_policy", result.status)
    check("the policy reason names the protected marker",
          any("credentials" in reason for reason in result.policy_reasons), result.policy_reasons)


def test_resume_never_recopies_source_paths_after_partial_edits() -> None:
    """RESTART_FROM_SANDBOX resume-safety, mirrored from bridge.py: a job
    interrupted mid-edit must never have its source_paths re-copied over
    whatever the model already partially wrote."""
    src_dir = Path(tempfile.mkdtemp(prefix="omniengineer_resume_test_"))
    src_file = src_dir / "resume_sample.py"
    src_file.write_text("original content\n")
    job_id = str(uuid.uuid4())
    workdir = harness.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "resume_sample.py").write_text("PARTIALLY EDITED by a prior interrupted attempt\n")
    job_ledger.create(job_id, task="synthetic resume test", requested_by="test", sandbox_path=str(workdir),
                       model="qwen3-coder:30b", max_iterations=5,
                       submit_params={"source_paths": [str(src_file)], "allowed_tools": None, "validation_config": None})
    job_ledger.checkpoint(job_id, job_ledger.JobState.EDITING, note="synthetic: interrupted mid-edit")
    # Force staleness so classify() returns RESTART_FROM_SANDBOX, not ESCALATE.
    import datetime as _dt
    record = job_ledger.load(job_id)
    record.heartbeat = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
    job_ledger._atomic_write_json(job_ledger._path(job_id), __import__("dataclasses").asdict(record))

    original_run = harness.run_agent_loop

    def fake_run_agent_loop(task, sandbox, **kw):
        return agent.AgentRunResult(task=task, sandbox=str(sandbox), model=kw.get("model", ""),
                                     provider=kw.get("provider", "ollama"), final_action="finish",
                                     summary_or_reason="synthetic resume no-op")

    harness.run_agent_loop = fake_run_agent_loop
    try:
        harness.resume_job(job_id, requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        shutil.rmtree(src_dir, ignore_errors=True)

    check("the partially-edited sandbox content survived resume (source_paths were NOT re-copied over it)",
          (workdir / "resume_sample.py").read_text() == "PARTIALLY EDITED by a prior interrupted attempt\n")




def test_json_safe_submit_value_contract() -> None:
    """H1: durable submission values are normalized deterministically."""
    norm = harness._json_safe_submit_value

    check(
        "H1 set normalizes deterministically",
        norm({"read_file", "grep"}) == ["grep", "read_file"],
    )
    check(
        "H1 frozenset normalizes deterministically",
        norm(frozenset({"z", "a"})) == ["a", "z"],
    )
    check(
        "H1 tuple normalizes to list",
        norm(("a", "b")) == ["a", "b"],
    )
    check(
        "H1 Path normalizes to string",
        norm(Path("/tmp/h1-proof")) == "/tmp/h1-proof",
    )

    nested = norm({
        "tools": {"read_file", "grep"},
        "paths": (Path("/tmp/a"), Path("/tmp/b")),
    })
    check(
        "H1 nested values normalize recursively",
        nested == {
            "tools": ["grep", "read_file"],
            "paths": ["/tmp/a", "/tmp/b"],
        },
        str(nested),
    )

    try:
        norm(object())
    except TypeError:
        unsupported_rejected = True
    else:
        unsupported_rejected = False

    check(
        "H1 unsupported arbitrary object is rejected",
        unsupported_rejected,
    )

    try:
        norm({1: "invalid"})
    except TypeError:
        invalid_key_rejected = True
    else:
        invalid_key_rejected = False

    check(
        "H1 non-string mapping key is rejected",
        invalid_key_rejected,
    )


def test_submit_job_json_safe_boundary_accepts_set() -> None:
    """H1 regression: set-valued allowed_tools must survive durable creation."""
    original_execute = harness._execute
    captured = {}

    def fake_execute(job_id, workdir, task, **kwargs):
        captured["job_id"] = job_id
        captured["allowed_tools"] = kwargs.get("allowed_tools")
        captured["source_paths"] = kwargs.get("source_paths")

        record = job_ledger.load(job_id)

        job_ledger.checkpoint(
            job_id,
            job_ledger.JobState.COMPLETED,
            note="H1 permanent JSON-safe boundary regression",
        )

        return {
            "job_id": job_id,
            "submit_params": record.submit_params,
        }

    harness._execute = fake_execute

    try:
        harness.submit_job(
            task="synthetic H1 JSON safe boundary regression",
            requested_by="test_omniengineer",
            founder_approved=True,
            allowed_tools={"read_file"},
            source_paths=(),
            max_iterations=1,
        )
    finally:
        harness._execute = original_execute

    job_id = captured.get("job_id")

    check(
        "H1 set allowed_tools survives durable job creation",
        bool(job_id),
    )

    record = job_ledger.load(job_id)

    check(
        "H1 durable allowed_tools is JSON-safe list",
        record.submit_params.get("allowed_tools") == ["read_file"],
        str(record.submit_params),
    )

    check(
        "H1 tuple source_paths is durable JSON-safe list",
        record.submit_params.get("source_paths") == [],
        str(record.submit_params),
    )

    check(
        "H1 runtime tool authority remains frozenset",
        captured.get("allowed_tools") == frozenset({"read_file"}),
        repr(captured.get("allowed_tools")),
    )




def test_h2_ledger_fields_are_backward_compatible() -> None:
    """H2: new durable routing fields remain optional for historical ledgers."""
    import dataclasses

    field_map = {
        f.name: f
        for f in dataclasses.fields(job_ledger.LedgerRecord)
    }

    check(
        "H2 LedgerRecord has provider field",
        "provider" in field_map,
    )

    check(
        "H2 LedgerRecord has fallback_reason field",
        "fallback_reason" in field_map,
    )

    check(
        "H2 historical provider defaults to None",
        field_map["provider"].default is None,
    )

    check(
        "H2 historical fallback_reason defaults to None",
        field_map["fallback_reason"].default is None,
    )


def test_h2_finalize_persists_provider_and_fallback_reason() -> None:
    """H2: terminal execution routing truth survives a fresh ledger reload."""
    job_id = str(uuid.uuid4())
    task = "synthetic H2 durable execution metadata regression"

    workdir = harness.JOBS_ROOT / job_id / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    job_ledger.create(
        job_id,
        task=task,
        requested_by="test_omniengineer",
        sandbox_path=str(workdir),
        model="qwen3-coder:30b",
        max_iterations=3,
        submit_params={
            "source_paths": [],
            "allowed_tools": ["read_file"],
            "validation_config": None,
        },
    )

    fallback_reason = (
        "synthetic primary model failure; "
        "cross-provider failover selected provider_b"
    )

    job_ledger.checkpoint(
        job_id,
        job_ledger.JobState.COMPLETED,
        note="synthetic H2 terminal routing proof",
        model="gpt-oss:20b",
        attempted_models=[
            "qwen3-coder:30b",
            "qwen3.6:27b",
            "gpt-oss:20b",
        ],
        attempted_providers=[
            "ollama",
            "provider_b",
        ],
        model_failure_reasons={
            "qwen3-coder:30b": "synthetic protocol failure",
            "qwen3.6:27b": "synthetic protocol failure",
        },
        terminal_result="succeeded",
    )

    result = harness.JobResult(
        job_id=job_id,
        task=task,
        adapter="omni_engineer",
        model="gpt-oss:20b",
        risk_class="low",
        approval_state="not_required",
        status="succeeded",
        workdir=str(workdir),
        started_at="2026-08-22T00:00:00+00:00",
        ended_at="2026-08-22T00:00:01+00:00",
        duration_s=1.0,
        files_changed={
            "added": [],
            "modified": [],
            "removed": [],
        },
        plan_text=None,
        agent_final_action="finish",
        agent_summary_or_reason="synthetic H2 proof",
        retried=True,
        attempted_models=[
            "qwen3-coder:30b",
            "qwen3.6:27b",
            "gpt-oss:20b",
        ],
        model_failure_reasons={
            "qwen3-coder:30b": "synthetic protocol failure",
            "qwen3.6:27b": "synthetic protocol failure",
        },
        fallback_reason=fallback_reason,
        provider="provider_b",
        attempted_providers=[
            "ollama",
            "provider_b",
        ],
    )

    original_audit_record = harness.audit.record
    harness.audit.record = lambda **kwargs: None

    try:
        harness._finalize(
            result,
            requested_by="test_omniengineer",
            final_disposition="succeeded",
        )
    finally:
        harness.audit.record = original_audit_record

    record = job_ledger.load(job_id)

    check(
        "H2 terminal ledger reload succeeds",
        record is not None,
    )

    check(
        "H2 final model remains durable",
        record.model == "gpt-oss:20b",
        repr(record.model),
    )

    check(
        "H2 final provider is durable",
        record.provider == "provider_b",
        repr(record.provider),
    )

    check(
        "H2 fallback reason is durable",
        record.fallback_reason == fallback_reason,
        repr(record.fallback_reason),
    )

    check(
        "H2 attempted providers remain durable",
        record.attempted_providers == ["ollama", "provider_b"],
        repr(record.attempted_providers),
    )

    check(
        "H2 attempted models remain durable",
        record.attempted_models == [
            "qwen3-coder:30b",
            "qwen3.6:27b",
            "gpt-oss:20b",
        ],
        repr(record.attempted_models),
    )




def test_h3_canonical_identity_compatibility() -> None:
    """H3: historical/internal names resolve to one canonical Studio organ."""
    import engine_identity as identity

    check(
        "H3 stable machine ID remains omni_engineer",
        identity.OMNI_ENGINEER_ID == "omni_engineer",
        repr(identity.OMNI_ENGINEER_ID),
    )

    check(
        "H3 canonical Studio name is Omni Engineer Codex",
        identity.OMNI_ENGINEER_CANONICAL_NAME == "Omni Engineer Codex",
        repr(identity.OMNI_ENGINEER_CANONICAL_NAME),
    )

    for alias in (
        "omni_engineer",
        "Omni Engineer",
        "OmniEngineer",
        "Omni Engineer Codex",
        "OmniEngineer Codex",
    ):
        resolved = identity.resolve_engine_identity(alias)

        check(
            f"H3 alias {alias!r} resolves",
            resolved is not None,
        )

        check(
            f"H3 alias {alias!r} resolves to stable machine ID",
            resolved.engine_id == "omni_engineer",
            repr(resolved.engine_id),
        )

        check(
            f"H3 alias {alias!r} resolves to canonical Studio name",
            resolved.canonical_name == "Omni Engineer Codex",
            repr(resolved.canonical_name),
        )

    for backend in (
        "qwen3-coder:30b",
        "qwen3.6:27b",
        "gpt-oss:20b",
        "Claude Code",
        "Codex CLI",
        "ollama",
        "provider_b",
    ):
        check(
            f"H3 backend {backend!r} is not reclassified as engineering organ",
            identity.resolve_engine_identity(backend) is None,
        )

    check(
        "H3 unknown value is not silently classified",
        identity.resolve_engine_identity("unknown_engine") is None,
    )

    check(
        "H3 None remains unresolved",
        identity.resolve_engine_identity(None) is None,
    )

    try:
        identity.resolve_engine_identity(123)
    except TypeError:
        rejected = True
    else:
        rejected = False

    check(
        "H3 non-string identity input is rejected",
        rejected,
    )


def test_h3_jobresult_preserves_machine_id_and_canonical_name() -> None:
    """H3: runtime result exposes canonical name without changing machine ID."""
    import engine_identity as identity

    result = harness.JobResult(
        job_id=str(uuid.uuid4()),
        task="synthetic H3 identity regression",
        adapter=identity.OMNI_ENGINEER_ID,
        model="qwen3-coder:30b",
        risk_class="low",
        approval_state="not_required",
        status="succeeded",
        workdir="/tmp/h3-identity-regression",
        started_at="2026-08-22T00:00:00+00:00",
        ended_at="2026-08-22T00:00:01+00:00",
        duration_s=1.0,
        files_changed={
            "added": [],
            "modified": [],
            "removed": [],
        },
        plan_text=None,
        agent_final_action="finish",
        agent_summary_or_reason="synthetic H3 proof",
        retried=False,
    )

    check(
        "H3 JobResult adapter preserves internal machine ID",
        result.adapter == "omni_engineer",
        repr(result.adapter),
    )

    check(
        "H3 JobResult carries canonical Studio name",
        result.engine_name == "Omni Engineer Codex",
        repr(result.engine_name),
    )

    resolved = identity.resolve_engine_identity(result.adapter)

    check(
        "H3 JobResult machine identity resolves to canonical organ",
        resolved is not None
        and resolved.engine_id == result.adapter
        and resolved.canonical_name == result.engine_name,
    )




def test_h4_backend_contract_core() -> None:
    """H4: every inference backend crosses one normalized contract."""
    import backend_contract as bc

    bc.clear_registry_for_tests()

    captured = {}

    def synthetic_backend(request):
        captured["prompt"] = request.prompt
        captured["model"] = request.model
        captured["timeout_s"] = request.timeout_s
        captured["metadata"] = dict(request.metadata)

        return bc.BackendResponse(
            text="synthetic normalized response",
            provider="synthetic_backend",
            model=request.model,
            metadata={"test": True},
        )

    bc.register_backend(
        bc.BackendAdapter(
            provider_id="synthetic_backend",
            invoke_fn=synthetic_backend,
        )
    )

    response = bc.invoke_backend(
        "synthetic_backend",
        prompt="synthetic normalized request",
        model="synthetic-model-v1",
        timeout_s=17,
        metadata={"origin": "H4 regression"},
    )

    check(
        "H4 normalized request preserves prompt",
        captured["prompt"] == "synthetic normalized request",
    )

    check(
        "H4 normalized request preserves model",
        captured["model"] == "synthetic-model-v1",
    )

    check(
        "H4 normalized request preserves timeout",
        captured["timeout_s"] == 17,
    )

    check(
        "H4 normalized request preserves metadata",
        captured["metadata"] == {
            "origin": "H4 regression",
        },
    )

    check(
        "H4 normalized response preserves text",
        response.text == "synthetic normalized response",
    )

    check(
        "H4 normalized response preserves provider",
        response.provider == "synthetic_backend",
    )

    check(
        "H4 normalized response preserves model",
        response.model == "synthetic-model-v1",
    )

    try:
        bc.get_backend("definitely_missing_backend")
    except bc.BackendNotRegisteredError:
        missing_rejected = True
    else:
        missing_rejected = False

    check(
        "H4 unknown backend is rejected",
        missing_rejected,
    )

    bc.clear_registry_for_tests()


def test_h4_builtin_backend_behavioral_parity() -> None:
    """H4: Ollama and Provider B retain proven low-level behavior."""
    import backend_contract as bc
    import omniengineer_agent as agent

    original_ollama = agent._call_ollama
    original_provider_b = agent._call_provider_b

    ollama_capture = {}
    provider_b_capture = {}

    def fake_ollama(prompt, *, model, timeout_s):
        ollama_capture.update(
            prompt=prompt,
            model=model,
            timeout_s=timeout_s,
        )
        return (
            '{"tool":"finish",'
            '"args":{"summary":"H4_OLLAMA_PARITY"}}'
        )

    def fake_provider_b(prompt, *, model, timeout_s):
        provider_b_capture.update(
            prompt=prompt,
            model=model,
            timeout_s=timeout_s,
        )
        return (
            '{"tool":"finish",'
            '"args":{"summary":"H4_PROVIDER_B_PARITY"}}'
        )

    try:
        bc.clear_registry_for_tests()

        agent._call_ollama = fake_ollama
        agent._call_provider_b = fake_provider_b

        ollama_raw = agent._call_model_backend(
            "ollama synthetic transcript",
            model="qwen3-coder:30b",
            provider="ollama",
            timeout_s=29,
        )

        provider_b_raw = agent._call_model_backend(
            "provider-b synthetic transcript",
            model="gpt-oss:20b",
            provider="provider_b",
            timeout_s=31,
        )

        check(
            "H4 Ollama adapter preserves prompt/model/timeout",
            ollama_capture == {
                "prompt": "ollama synthetic transcript",
                "model": "qwen3-coder:30b",
                "timeout_s": 29,
            },
        )

        check(
            "H4 Ollama adapter preserves raw response",
            "H4_OLLAMA_PARITY" in ollama_raw,
        )

        check(
            "H4 Provider B adapter preserves prompt/model/timeout",
            provider_b_capture == {
                "prompt": "provider-b synthetic transcript",
                "model": "gpt-oss:20b",
                "timeout_s": 31,
            },
        )

        check(
            "H4 Provider B adapter preserves raw response",
            "H4_PROVIDER_B_PARITY" in provider_b_raw,
        )

        check(
            "H4 Ollama registered through normalized contract",
            bc.get_backend("ollama").provider_id == "ollama",
        )

        check(
            "H4 Provider B registered through normalized contract",
            bc.get_backend("provider_b").provider_id == "provider_b",
        )

    finally:
        agent._call_ollama = original_ollama
        agent._call_provider_b = original_provider_b
        bc.clear_registry_for_tests()


def test_h4_future_backend_requires_no_agent_branch() -> None:
    """H4: a new backend can enter without editing the agent loop."""
    import backend_contract as bc
    import omniengineer_agent as agent

    bc.clear_registry_for_tests()

    def future_backend(request):
        return bc.BackendResponse(
            text=(
                '{"tool":"finish",'
                '"args":{"summary":"H4_FUTURE_BACKEND_OK"}}'
            ),
            provider="future_backend",
            model=request.model,
        )

    try:
        bc.register_backend(
            bc.BackendAdapter(
                provider_id="future_backend",
                invoke_fn=future_backend,
            )
        )

        raw = agent._call_model_backend(
            "future backend transcript",
            model="future-model-v1",
            provider="future_backend",
            timeout_s=11,
        )

        check(
            "H4 future backend uses same agent invocation seam",
            "H4_FUTURE_BACKEND_OK" in raw,
        )

        check(
            "H4 future backend remains a backend, not Omni identity",
            __import__(
                "engine_identity"
            ).resolve_engine_identity(
                "future_backend"
            ) is None,
        )

    finally:
        bc.clear_registry_for_tests()


def test_h4_agent_loop_uses_normalized_backend_seam() -> None:
    """H4: provider-specific binary dispatch stays out of agent loop."""
    import inspect
    import omniengineer_agent as agent

    source = inspect.getsource(
        agent.run_agent_loop
    )

    check(
        "H4 agent loop calls normalized backend seam",
        "_call_model_backend(" in source,
    )

    check(
        "H4 agent loop has no direct Ollama binary branch",
        'if provider == "ollama"' not in source
        and "if provider == 'ollama'" not in source,
    )

    check(
        "H4 existing low-level Ollama call remains available",
        callable(agent._call_ollama),
    )

    check(
        "H4 existing low-level Provider B call remains available",
        callable(agent._call_provider_b),
    )


# ---- OLLAMA_NUM_CTX_REPAIR (Founder-authorized, 2026-09-02) ----------------
#
# _call_ollama() never sent an explicit num_ctx, so Ollama silently ran the
# model at its own small default context window (confirmed live: 4096
# tokens) regardless of MAX_TRANSCRIPT_CHARS's 60,000-character budget --
# causing exact-match patches against a real ~21KB seeded file to fail
# repeatedly. Fixed by sending options.num_ctx=OLLAMA_NUM_CTX (32768,
# empirically confirmed to load fully GPU-resident with headroom) on every
# real Ollama request, scoped to _call_ollama() only -- Provider B and any
# other non-Ollama backend are structurally untouched.

def test_ollama_request_carries_explicit_finite_num_ctx() -> None:
    """A/B/C: the real Ollama request payload explicitly carries
    options.num_ctx, it is finite and greater than the old silent default
    (4096), and it matches the module's configured, documented-safe
    constant (itself well below qwen3-coder:30b's real model-reported max
    of 262144, confirmed live via /api/show)."""
    import json as _json
    import urllib.request as _urlreq

    captured: dict = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"response": '{"thought":"x","tool":"finish","args":{"summary":"ok"}}'}).encode()

    def fake_urlopen(req, timeout=None):
        captured["data"] = _json.loads(req.data.decode())
        return _FakeResponse()

    original_urlopen = _urlreq.urlopen
    _urlreq.urlopen = fake_urlopen
    try:
        agent._call_ollama("hello", model="qwen3-coder:30b", timeout_s=5)
    finally:
        _urlreq.urlopen = original_urlopen

    check("the real Ollama request payload includes an options.num_ctx field",
          "options" in captured.get("data", {}) and "num_ctx" in captured["data"].get("options", {}),
          captured.get("data"))

    sent_num_ctx = captured.get("data", {}).get("options", {}).get("num_ctx")
    check("num_ctx sent on the wire is a finite int greater than the old silent default (4096)",
          isinstance(sent_num_ctx, int) and sent_num_ctx > 4096, sent_num_ctx)
    check("num_ctx sent on the wire equals the module's configured OLLAMA_NUM_CTX constant",
          sent_num_ctx == agent.OLLAMA_NUM_CTX, (sent_num_ctx, agent.OLLAMA_NUM_CTX))
    check("OLLAMA_NUM_CTX itself is finite and stays well below qwen3-coder:30b's real model-reported max (262144)",
          isinstance(agent.OLLAMA_NUM_CTX, int) and 4096 < agent.OLLAMA_NUM_CTX <= 262144, agent.OLLAMA_NUM_CTX)


def test_provider_b_path_is_structurally_unaffected_by_ollama_num_ctx() -> None:
    """D: non-Ollama providers are unaffected. Provider B's call path does
    not go through _call_ollama() at all -- it cannot ever carry an
    Ollama-specific options.num_ctx field, and the fix touched no shared
    code either backend depends on."""
    import inspect
    source = inspect.getsource(agent._call_provider_b)
    check("_call_provider_b's implementation never references OLLAMA_NUM_CTX or num_ctx",
          "OLLAMA_NUM_CTX" not in source and "num_ctx" not in source, source)
    check("_call_provider_b delegates to provider_b_bridge.generate, a structurally separate call path from _call_ollama",
          "provider_b_bridge.generate(" in source, source)


# ---- SINGLE_SHOT_PATCH_STRATEGY_V1 (Founder-authorized, 2026-09-02) --------

def _single_shot_sandbox(seed_files: dict[str, str]) -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="single_shot_test_"))
    for rel, content in seed_files.items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return sandbox


def test_single_shot_mode_is_explicitly_selected_not_silently_global() -> None:
    """1: single-shot mode is opt-in only. submit_job_auto()'s automatic
    routing (task_router.py, evolution/advance.py's real callers) never
    reaches run_single_shot_patch()/submit_job_single_shot() -- a caller
    must name it directly, exactly like submit_job_decomposed() already
    requires today."""
    import inspect
    auto_source = inspect.getsource(harness.submit_job_auto)
    check("submit_job_auto() never references run_single_shot_patch or submit_job_single_shot",
          "run_single_shot_patch" not in auto_source and "submit_job_single_shot" not in auto_source, auto_source)
    check("submit_job_single_shot is a real, independently-callable function",
          callable(harness.submit_job_single_shot))


def test_normal_multi_turn_agent_loop_remains_available() -> None:
    """2: the existing multi-turn tool-calling agent loop is completely
    unchanged and still the default/only path for every existing caller."""
    check("run_agent_loop still exists and is callable", callable(agent.run_agent_loop))
    check("submit_job (simple, non-decomposed) still routes through run_agent_loop, not the single-shot path",
          "run_agent_loop" in __import__("inspect").getsource(harness._execute)
          and "run_single_shot_patch" not in __import__("inspect").getsource(harness._execute))


def test_single_shot_rejects_a_path_outside_the_allowed_paths_allowlist() -> None:
    """3: only authorized staged paths may be patched -- a path not in
    allowed_paths is refused even though it is otherwise a normal relative
    path with no traversal/protected-marker issue."""
    sandbox = _single_shot_sandbox({"a.py": "X = 1\n"})
    original = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: json.dumps({
        "files": [{"path": "b.py", "op": "create", "content": "Y = 2\n"}]
    })
    try:
        run = agent.run_single_shot_patch("test", sandbox, allowed_paths=frozenset({"a.py"}))
    finally:
        agent._call_model_backend = original
    check("out-of-allowlist path is refused, not applied", not (sandbox / "b.py").exists())
    check("final_action reflects nothing succeeded", run.final_action == "error", run.final_action)


def test_single_shot_rejects_path_traversal() -> None:
    """4: path traversal is rejected -- inherited unchanged from
    _safe_path()/_tool_apply_patch_sandbox(), the same primitive
    run_agent_loop() itself relies on."""
    sandbox = _single_shot_sandbox({"a.py": "X = 1\n"})
    original = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: json.dumps({
        "files": [{"path": "../escape.py", "op": "create", "content": "Y = 2\n"}]
    })
    try:
        agent.run_single_shot_patch("test", sandbox, allowed_paths=frozenset({"../escape.py"}))
    finally:
        agent._call_model_backend = original
    check("traversal path never escapes the sandbox", not (sandbox.parent / "escape.py").exists())


def test_single_shot_rejects_a_protected_secret_path() -> None:
    """5: protected/secret paths are rejected -- inherited unchanged from
    _safe_path()'s GATED_PATH_MARKERS check."""
    sandbox = _single_shot_sandbox({})
    original = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: json.dumps({
        "files": [{"path": "secrets/key.py", "op": "create", "content": "X = 1\n"}]
    })
    try:
        run = agent.run_single_shot_patch("test", sandbox, allowed_paths=frozenset({"secrets/key.py"}))
    finally:
        agent._call_model_backend = original
    check("protected/secret-marked path is never written", not (sandbox / "secrets" / "key.py").exists())
    check("nothing succeeded", run.final_action == "error", run.final_action)


def test_single_shot_rejects_malformed_model_output_safely() -> None:
    """6: malformed model output is rejected safely -- no crash, a clean
    error final_action, nothing applied."""
    sandbox = _single_shot_sandbox({"a.py": "X = 1\n"})
    original = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: "this is not JSON at all {{{"
    try:
        run = agent.run_single_shot_patch("test", sandbox, allowed_paths=frozenset({"a.py"}))
    finally:
        agent._call_model_backend = original
    check("malformed output yields a clean error, not a crash", run.final_action == "error", run.final_action)
    check("nothing was changed", (sandbox / "a.py").read_text() == "X = 1\n")


def test_single_shot_rejects_out_of_scope_patch_exceeding_max_files() -> None:
    """7: out-of-scope patches (touching more files than the bounded
    max_files) are rejected safely, nothing applied."""
    sandbox = _single_shot_sandbox({})
    allowed = frozenset({f"f{i}.py" for i in range(4)})
    original = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: json.dumps({
        "files": [{"path": f"f{i}.py", "op": "create", "content": "X = 1\n"} for i in range(4)]
    })
    try:
        run = agent.run_single_shot_patch("test", sandbox, allowed_paths=allowed, max_files=3)
    finally:
        agent._call_model_backend = original
    check("nothing was written when the patch exceeds max_files",
          not any((sandbox / f"f{i}.py").exists() for i in range(4)))
    check("final_action is escalate, a safe refusal not a crash", run.final_action == "escalate", run.final_action)


def test_single_shot_validation_failure_does_not_promote() -> None:
    """8: validation failure does not promote work -- a syntactically
    broken file gets written (the model's patch itself succeeds
    mechanically) but validation.validate()'s python_compile check fails
    it, so promotion_eligible stays False and status reflects the failure,
    through the exact same governed validation gate every other execution
    path uses."""
    original_call = agent._call_model_backend
    agent._call_model_backend = lambda prompt, *, model, provider, timeout_s: json.dumps({
        "files": [{"path": "broken.py", "op": "create", "content": "def broken(:\n"}]
    })
    try:
        result = harness.submit_job_single_shot(
            "TEST-ONLY: single-shot validation-failure gate", requested_by="test",
            allowed_paths=frozenset({"broken.py"}),
        )
    finally:
        agent._call_model_backend = original_call
    check("the mechanically-broken file was actually written to the job's own sandbox",
          "broken.py" in result.files_changed.get("added", []), result.files_changed)
    check("promotion_eligible is False on a validation failure", result.promotion_eligible is False, result.promotion_eligible)
    check("status reflects the validation failure, never a bare success",
          result.status == "succeeded_validation_failed", result.status)


def test_single_shot_paid_resource_gate_preserved() -> None:
    """9: the paid-resource gate remains intact -- submit_job_single_shot
    hardcodes provider='ollama' with no parameter through which a caller
    could select a paid provider; the underlying model call is the exact
    same free/local _call_ollama() path."""
    import inspect
    source = inspect.getsource(harness.submit_job_single_shot)
    check("submit_job_single_shot always calls run_single_shot_patch with provider='ollama'",
          'provider="ollama"' in source, source)
    check("submit_job_single_shot's signature exposes no provider/paid-route override parameter",
          "provider" not in inspect.signature(harness.submit_job_single_shot).parameters, source)


def test_single_shot_founder_promotion_gate_preserved() -> None:
    """10: the Founder production-promotion gate remains intact --
    submit_job_single_shot never reaches PROMOTED or calls promotion.py;
    it stops, like every other engine, at PROMOTION_CANDIDATE at most."""
    import inspect
    source = inspect.getsource(harness.submit_job_single_shot)
    check("submit_job_single_shot never references PROMOTED or promotion.py's promote function",
          "ProposalStatus.PROMOTED" not in source and "promotion.promote(" not in source, source)
    check("the only lifecycle states this function checkpoints toward are PROMOTION_CANDIDATE/COMPLETED, never PROMOTED",
          "JobState.PROMOTION_CANDIDATE" in source and "JobState.COMPLETED" in source, source)


def test_single_shot_path_also_sends_the_bounded_num_ctx() -> None:
    """11: num_ctx remains explicitly bounded at 32768 for this Ollama path
    too -- run_single_shot_patch() reuses _call_model_backend()/
    _call_ollama() exactly like run_agent_loop() does, so the num_ctx fix
    applies automatically, with no separate/duplicated configuration."""
    import urllib.request as _urlreq

    captured: dict = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"response": json.dumps({"files": [{"path": "a.py", "op": "patch", "old_string": "X = 1", "new_string": "X = 2"}]})}).encode()

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        return _FakeResponse()

    sandbox = _single_shot_sandbox({"a.py": "X = 1\n"})
    original_urlopen = _urlreq.urlopen
    _urlreq.urlopen = fake_urlopen
    try:
        agent.run_single_shot_patch("test", sandbox, model="qwen3-coder:30b", allowed_paths=frozenset({"a.py"}))
    finally:
        _urlreq.urlopen = original_urlopen

    check("the single-shot path's real Ollama request also carries options.num_ctx",
          captured.get("data", {}).get("options", {}).get("num_ctx") == agent.OLLAMA_NUM_CTX,
          captured.get("data"))


# ---- integration tests (live Ollama call — genuinely proven, not mocked) ---

def test_full_target_loop_live() -> None:
    result = harness.submit_job(
        task=(
            "Use write_file_sandbox to create calc2.py with a function multiply(a, b) that "
            "INCORRECTLY returns a + b (a bug, on purpose). Then use write_file_sandbox to create "
            "test_calc2.py with a unittest test checking multiply(3, 4) == 12. Use run_command to "
            "run the tests with python3 -m unittest — it will fail. Then use apply_patch_sandbox to "
            "fix multiply so it actually multiplies. Run the tests again to confirm they pass, call "
            "run_validator, then finish."
        ),
        requested_by="test_omniengineer_integration",
        timeout_s=280,
    )
    check("live run reaches status=succeeded", result.status == "succeeded",
          f"status={result.status!r} agent_final_action={result.agent_final_action!r} reason={result.agent_summary_or_reason!r}")
    check("live run is promotion_eligible (validation + canary both passed)", result.promotion_eligible is True)
    tools_used = {t["tool"] for t in result.turns}
    check("the loop actually exercised write_file_sandbox", "write_file_sandbox" in tools_used, str(tools_used))
    check("the loop actually exercised apply_patch_sandbox (the INSPECT FAILURE -> repair step)",
          "apply_patch_sandbox" in tools_used, str(tools_used))
    check("the loop stayed within the iteration ceiling", len(result.turns) <= agent.MAX_ITERATIONS)

    # PROVE reuse of the existing, unmodified promotion.py against this
    # OmniEngineer job's result.json — read-only plan(), never writes.
    if result.status == "succeeded":
        plan = promotion.plan(result.job_id, str(Path(harness.BRIDGE_ROOT) / "_omniengineer_test_target_not_written"))
        check("promotion.plan() works unmodified against an omni_engineer job",
              isinstance(plan.files, list) and len(plan.files) >= 2,
              f"plan.files={plan.files}")


# ---- OMNI_GOD_MODE_V1 PHASE 2 -- bounded task decomposition ----------------
# All monkeypatch harness.run_agent_loop directly (same pattern as
# test_evolution_advance.py's _ENGINE_RUNNERS patching) so these are fast,
# deterministic, and spend zero real model calls. Real regression target:
# job 6978adf2 (18-iteration exhaustion on one undecomposed loop).

import validation as _god2_validation


def _god2_fake_run(final_action: str = "finish", summary: str = "did the thing", commands=None) -> agent.AgentRunResult:
    return agent.AgentRunResult(
        task="x", sandbox="x", model="qwen3-coder:30b", final_action=final_action,
        summary_or_reason=summary, commands_executed=commands or [],
    )


def _god2_sequenced_runner(results: list[agent.AgentRunResult]):
    calls = {"phase_names": []}
    it = iter(results)

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        calls["phase_names"].append((model, allowed_tools))
        try:
            return next(it)
        except StopIteration:
            return _god2_fake_run("finish")
    return runner, calls


def test_decomposed_happy_path_records_all_phases_and_succeeds() -> None:
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate
    runner, calls = _god2_sequenced_runner([_god2_fake_run() for _ in range(3)])
    harness.run_agent_loop = runner
    _god2_validation.validate = lambda *a, **k: type("V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: happy path", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate

    check("happy-path decomposed job succeeds", r.status == "succeeded", r.status)
    check("all 3 base phases were run (inspect/implement/test)", len(calls["phase_names"]) == 3, calls["phase_names"])
    record = job_ledger.load(r.job_id)
    check("ledger.phases has one durable entry per phase run", len(record.phases) == 3, record.phases)
    check("phases are recorded in the correct order", [p["name"] for p in record.phases] == ["inspect", "implement", "test"], record.phases)
    check("inspect phase was structurally denied write tools (allowed_tools)",
          "write_file_sandbox" not in (calls["phase_names"][0][1] or set()), calls["phase_names"][0][1])
    check("test phase was granted run_command", "run_command" in (calls["phase_names"][2][1] or set()), calls["phase_names"][2][1])


def test_decomposed_escalate_stops_immediately_no_later_phases() -> None:
    original_run = harness.run_agent_loop
    runner, calls = _god2_sequenced_runner([_god2_fake_run("finish"), _god2_fake_run("escalate", "stuck, need a human")])
    harness.run_agent_loop = runner
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: escalation", requested_by="test")
    finally:
        harness.run_agent_loop = original_run

    check("job status is escalated", r.status == "escalated", r.status)
    check("only 2 phases ran (inspect, implement) -- test phase never started after escalate",
          len(calls["phase_names"]) == 2, calls["phase_names"])
    record = job_ledger.load(r.job_id)
    check("ledger reflects exactly 2 recorded phases", len(record.phases) == 2, record.phases)
    check("ledger terminal state is escalated", record.state == job_ledger.JobState.ESCALATED.value, record.state)
    # PARTIAL_IMPLEMENTATION (Phase 3): the record of what completed vs.
    # what didn't must survive the escalation, not just files_touched.
    check("partial_progress names the phase that actually completed (inspect)",
          r.partial_progress is not None and r.partial_progress.get("phases_completed") == ["inspect"], r.partial_progress)
    check("partial_progress names the phase that escalated (implement)",
          r.partial_progress is not None and r.partial_progress.get("phase_that_escalated") == "implement", r.partial_progress)
    check("JobResult.decomposed=True on an escalated decomposed job", r.decomposed is True, r.decomposed)


def test_decomposed_validation_failure_triggers_bounded_repair_then_succeeds() -> None:
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate
    runner, calls = _god2_sequenced_runner([_god2_fake_run() for _ in range(4)])  # inspect, implement, test, repair_1
    harness.run_agent_loop = runner
    validate_calls = {"n": 0}

    def fake_validate(*a, **k):
        validate_calls["n"] += 1
        passed = validate_calls["n"] >= 2  # first VALIDATE fails, repair happens, second (post-repair) VALIDATE passes
        return type("V", (), {"passed": passed, "to_json": lambda self, p=passed: {"passed": p, "checks": []}})()
    _god2_validation.validate = fake_validate
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: repair cycle", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate

    check("job succeeds after exactly one repair cycle", r.status == "succeeded", r.status)
    check("exactly 4 phases ran (inspect, implement, test, repair_1)", len(calls["phase_names"]) == 4, calls["phase_names"])
    record = job_ledger.load(r.job_id)
    check("ledger shows the repair_1 phase durably", any(p["name"] == "repair_1" for p in record.phases), record.phases)


def test_decomposed_repair_cycles_are_bounded() -> None:
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate
    runner, calls = _god2_sequenced_runner([_god2_fake_run() for _ in range(10)])
    harness.run_agent_loop = runner
    _god2_validation.validate = lambda *a, **k: type("V", (), {"passed": False, "to_json": lambda self: {"passed": False, "checks": []}})()
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: never-passing validation", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate

    check("job ends as succeeded_validation_failed, not an infinite loop", r.status == "succeeded_validation_failed", r.status)
    check(f"repair cycles bounded to DECOMPOSED_MAX_REPAIR_CYCLES={harness.DECOMPOSED_MAX_REPAIR_CYCLES}, total phases = 3 base + that many repairs",
          len(calls["phase_names"]) == 3 + harness.DECOMPOSED_MAX_REPAIR_CYCLES, calls["phase_names"])


def test_decomposed_gated_task_never_runs_a_phase() -> None:
    original_run = harness.run_agent_loop
    called = {"n": 0}
    def runner(*a, **k):
        called["n"] += 1
        return _god2_fake_run()
    harness.run_agent_loop = runner
    try:
        r = harness.submit_job_decomposed("delete the credentials file and rotate the api_key", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
    check("gated task is rejected_policy before any phase runs", r.status == "rejected_policy", r.status)
    check("no phase ever called run_agent_loop for a gated task", called["n"] == 0, called["n"])


def test_decomposed_phase_state_survives_simulated_crash_and_resume_reads_it() -> None:
    """No process is actually killed (that would destabilize the live
    runtime) -- instead, the ledger IS the durable state by construction
    (checkpointed after every phase via job_ledger.checkpoint, atomic
    write). This proves the real property that matters: after a phase
    completes and before the NEXT phase starts, a totally fresh read of the
    job (phases_from_ledger(), simulating a new process after a crash) sees
    the completed phase and would not need to repeat it."""
    original_run = harness.run_agent_loop
    seen_after_first_phase = {}
    call_count = {"n": 0}

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        call_count["n"] += 1
        if call_count["n"] == 2 and not seen_after_first_phase:
            # This is the SECOND run_agent_loop call (start of the
            # implement phase) -- phase 1 (inspect) has already been
            # checkpointed by _record_phase by this point. Simulate "crash
            # after phase 1, fresh process reads the ledger" right here,
            # before this (phase 2) call's own result exists.
            seen_after_first_phase["phases"] = list(job_ledger.load(job_id_holder["id"]).phases)
        return _god2_fake_run()

    job_id_holder: dict[str, str] = {}
    original_create = job_ledger.create

    def create_and_capture(job_id, **kw):
        job_id_holder["id"] = job_id
        return original_create(job_id, **kw)

    harness.run_agent_loop = runner
    job_ledger.create = create_and_capture
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: crash-resume evidence", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        job_ledger.create = original_create

    check("job still completed normally (no real crash, just a mid-run durable read)", r.status in ("succeeded", "succeeded_validation_failed", "succeeded_canary_failed"), r.status)
    check("a fresh ledger read taken BEFORE phase 2 already shows phase 1 durably recorded",
          seen_after_first_phase.get("phases") and seen_after_first_phase["phases"][0]["name"] == "inspect",
          seen_after_first_phase.get("phases"))
    check("that pre-phase-2 read would not need to repeat the already-completed inspect phase",
          len(seen_after_first_phase.get("phases", [])) == 1, seen_after_first_phase.get("phases"))


def test_decomposed_model_failover_within_a_phase_uses_real_installed_model_only() -> None:
    original_run = harness.run_agent_loop
    original_check = _mrsilent_test_local_model_health.check
    original_order = _mrsilent_test_local_model_health.engineering_failover_order
    attempts = []

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        attempts.append(model)
        if model == "qwen3-coder:30b":
            return _god2_fake_run("iteration_ceiling_reached", "burned all iterations")
        return _god2_fake_run("finish")

    _mrsilent_test_local_model_health.check = lambda **k: type("H", (), {"available": True, "models": ["qwen3-coder:30b", "qwen3.6:27b"]})()
    _mrsilent_test_local_model_health.engineering_failover_order = lambda exclude=None: [m for m in ["qwen3-coder:30b", "qwen3.6:27b", "gpt-oss:20b"] if m not in (exclude or [])]
    harness.run_agent_loop = runner
    try:
        run, mdl, attempted, _attempt_history = harness._run_phase(
            "inspect", "objective", Path(tempfile.mkdtemp()), parent_task="x", prior_summary="",
            model="qwen3-coder:30b", max_iterations=6, timeout_s=60,
        )
    finally:
        harness.run_agent_loop = original_run
        _mrsilent_test_local_model_health.check = original_check
        _mrsilent_test_local_model_health.engineering_failover_order = original_order

    check("phase failed over from qwen3-coder:30b to the next REAL installed model (qwen3.6:27b), not the unavailable gpt-oss:20b",
          attempted == ["qwen3-coder:30b", "qwen3.6:27b"], attempted)
    check("phase ultimately finished cleanly after failover", run.final_action == "finish", run.final_action)


def test_decomposed_ledger_record_carries_real_files_touched_and_promotion_eligible() -> None:
    """Regression test for a real bug found via a live work_result check
    after this feature shipped: the final job_ledger.checkpoint() call
    computed files_touched/promotion_eligible correctly for the in-memory
    JobResult but never wrote them back onto the durable LedgerRecord, so
    job_ledger.load(job_id) (what work_result-style lookups actually read)
    showed files_touched={} and promotion_eligible=False even for a job that
    had genuinely succeeded with real file changes."""
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        # Simulate a real successful tool execution: actually write a file,
        # the same durable effect a real write_file_sandbox call would have.
        (workdir / "output.txt").write_text("real content")
        return _god2_fake_run()

    harness.run_agent_loop = runner
    _god2_validation.validate = lambda *a, **k: type("V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()
    try:
        r = harness.submit_job_decomposed("synthetic decomposed test: ledger fields regression", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate

    check("JobResult reports promotion_eligible=True", r.promotion_eligible is True, r.promotion_eligible)
    record = job_ledger.load(r.job_id)
    check("durable ledger record ALSO shows promotion_eligible=True (not just the in-memory result)",
          record.promotion_eligible is True, record.promotion_eligible)
    check("durable ledger record's files_touched reflects the real added file",
          "output.txt" in record.files_touched.get("added", []), record.files_touched)


# ---- OMNI_GOD_MODE_V1 PHASE 3: complexity classification / router integration ----

_PHASE3_SYNTHETIC_COMPLEX_TASK = """
OMNI ENGINEER SYNTHETIC CERTIFICATION TASK -- Phase 3 test fixture only.

PHASE 1 -- INSPECT
Inspect widgets.py and test_widgets.py to understand the existing Widget class.

PHASE 2 -- IMPLEMENT
Design and implement a bounded extension: add a discount() method to
widgets.py that validates its percentage argument.

PHASE 3 -- TEST
Write and run tests in test_widgets.py covering the new discount() method,
including an invalid-percentage case.

PHASE 4 -- REPAIR
If any test fails, repair widgets.py and re-run the tests until they pass,
then validate the full change-set.
""".strip()


def test_classify_complexity_simple_one_file_edit_is_not_eligible() -> None:
    d = harness.classify_complexity("Fix the typo in README.md")
    check("a simple one-file edit is NOT decomposition-eligible (fail-conservative)",
          d.decomposition_eligible is False, d.reasons)


def test_classify_complexity_length_alone_is_not_enough() -> None:
    long_but_shapeless = "please help with this task. " * 100  # >1200 chars, zero structural signals
    d = harness.classify_complexity(long_but_shapeless)
    check("length alone (no stage keywords, no file mentions, no phase markers) is NOT enough",
          d.decomposition_eligible is False, d.signals)


def test_classify_complexity_multi_stage_synthetic_task_is_eligible() -> None:
    """Calibrated against the SHAPE (length, PHASE N markers, multiple named
    files, multiple stage keywords) of real historical job 6978adf2 -- using
    an entirely separate, synthetic fixture task, never that job's own
    content (which is explicitly out of scope for this campaign)."""
    d = harness.classify_complexity(_PHASE3_SYNTHETIC_COMPLEX_TASK)
    check("a multi-phase, multi-file, multi-stage-keyword synthetic task IS decomposition-eligible",
          d.decomposition_eligible is True, d.signals)
    check(">=2 independent signal categories actually fired",
          len(d.reasons) >= 2 and not any("only" in r and "independent signal categor" in r for r in d.reasons), d.reasons)


def test_submit_job_auto_dispatches_simple_task_to_submit_job() -> None:
    original_simple = harness.submit_job
    original_decomposed = harness.submit_job_decomposed
    calls = {"simple": 0, "decomposed": 0}
    harness.submit_job = lambda *a, **k: (calls.__setitem__("simple", calls["simple"] + 1), _god2_fake_jobresult())[1]
    harness.submit_job_decomposed = lambda *a, **k: (calls.__setitem__("decomposed", calls["decomposed"] + 1), _god2_fake_jobresult())[1]
    try:
        harness.submit_job_auto("Fix the typo in README.md", requested_by="test")
    finally:
        harness.submit_job = original_simple
        harness.submit_job_decomposed = original_decomposed
    check("simple task routed to submit_job()", calls == {"simple": 1, "decomposed": 0}, calls)


def test_submit_job_auto_dispatches_complex_task_to_submit_job_decomposed() -> None:
    original_simple = harness.submit_job
    original_decomposed = harness.submit_job_decomposed
    calls = {"simple": 0, "decomposed": 0}
    harness.submit_job = lambda *a, **k: (calls.__setitem__("simple", calls["simple"] + 1), _god2_fake_jobresult())[1]
    harness.submit_job_decomposed = lambda *a, **k: (calls.__setitem__("decomposed", calls["decomposed"] + 1), _god2_fake_jobresult())[1]
    try:
        harness.submit_job_auto(_PHASE3_SYNTHETIC_COMPLEX_TASK, requested_by="test")
    finally:
        harness.submit_job = original_simple
        harness.submit_job_decomposed = original_decomposed
    check("complex task routed to submit_job_decomposed()", calls == {"simple": 0, "decomposed": 1}, calls)


def test_decomposed_phase_iteration_ceiling_is_ten_and_finite() -> None:
    """Founder-authorized bounded test-limit increase (GOD MODE JOURNEY 10.7
    walk-away acceptance run, 2026-09-02): DECOMPOSED_MAX_ITERATIONS_PER_PHASE
    raised from 6 to 10. Proves the new ceiling actually reaches every real
    phase call (not silently ignored or unbounded) and remains a genuine,
    small, finite bound rather than an accidental disable."""
    check("the ceiling is a genuine finite bound, not unlimited",
          isinstance(harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE, int) and 0 < harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE <= 20,
          harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE)
    check("the authorized test value is exactly 10", harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE == 10,
          harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE)

    original_run = harness.run_agent_loop
    seen_max_iterations = []

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        seen_max_iterations.append(max_iterations)
        return _god2_fake_run("finish")

    harness.run_agent_loop = runner
    try:
        harness.submit_job_decomposed("synthetic test: iteration ceiling propagation", requested_by="test")
    finally:
        harness.run_agent_loop = original_run

    check("submit_job_decomposed's real phase calls receive the new ceiling (10), not the old default (6) or unbounded",
          bool(seen_max_iterations) and all(m == 10 for m in seen_max_iterations), seen_max_iterations)


def test_submit_job_auto_decomposed_path_still_wired_to_the_module_ceiling() -> None:
    """submit_job_auto() is the single sanctioned entry point every real
    caller (task_router.py, evolution/advance.py's _run_omni_engineer())
    uses -- it hardcodes max_iterations_per_phase=DECOMPOSED_MAX_ITERATIONS_PER_PHASE
    with no per-call override, so the module constant is the effective
    ceiling for every decomposed job, system-wide. Proves that wiring still
    holds after the bump: a real submit_job_auto() call on a genuinely
    complex/decomposition-eligible task threads the current module constant
    through to the real phase calls, not a stale hardcoded literal."""
    original_run = harness.run_agent_loop
    seen_max_iterations = []

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        seen_max_iterations.append(max_iterations)
        return _god2_fake_run("finish")

    harness.run_agent_loop = runner
    try:
        harness.submit_job_auto(_PHASE3_SYNTHETIC_COMPLEX_TASK, requested_by="test")
    finally:
        harness.run_agent_loop = original_run

    check("submit_job_auto's decomposed path uses the current module ceiling",
          bool(seen_max_iterations) and all(m == harness.DECOMPOSED_MAX_ITERATIONS_PER_PHASE for m in seen_max_iterations),
          seen_max_iterations)


def _god2_fake_jobresult() -> "harness.JobResult":
    return harness.JobResult(
        job_id="fake", task="x", adapter="omni_engineer_v1", model="qwen3-coder:30b",
        risk_class="low", approval_state="not_required", status="succeeded", workdir="x",
        started_at="x", ended_at="x", duration_s=0.0,
        files_changed={"added": [], "modified": [], "removed": []}, plan_text=None,
        agent_final_action="finish", agent_summary_or_reason="x", retried=False,
    )


def test_context_staging_excludes_secret_paths_by_default() -> None:
    """GOD_MODE_V1 FINAL GAP CLOSURE: defense-in-depth -- the context-
    staging filter itself excludes secret/key-marked paths, independent of
    (in addition to) authority_policy.GATED_PATH_MARKERS rejecting the
    whole job for the same paths."""
    secret_paths = [
        Path("/opt/pulse5-core/somewhere/secrets/api_key.json"),
        Path("/opt/pulse5-core/somewhere/.env"),
        Path("/home/user/.ssh/id_rsa"),
        Path("/home/user/.aws/credentials"),
    ]
    clean = [Path("/opt/pulse5-core/mrsilent_bridge/studio_router.py")]
    allowed, excluded = harness._stage_context_source_paths(secret_paths + clean)
    check("all 4 secret/key-marked paths were excluded by the staging filter itself",
          len(excluded) == 4, excluded)
    check("none of the excluded paths leaked into the allowed list", not any(p in allowed for p in secret_paths), allowed)
    check("the normal canonical file remains allowed", clean[0] in allowed, allowed)


def test_context_staging_excludes_manual_candidates_and_backups_by_default() -> None:
    noisy = [
        Path("/opt/pulse5-core/mrsilent_bridge/manual_candidates/foo/bar.py"),
        Path("/opt/pulse5-core/render_forge_01_continuity_backup/20260821/x.py"),
        Path("/opt/pulse5-core/mrsilent_bridge/jobs/deadbeef-1234/workdir/y.py"),
    ]
    clean = [Path("/opt/pulse5-core/mrsilent_bridge/studio_router.py")]
    allowed, excluded = harness._stage_context_source_paths(noisy + clean)
    check("manual_candidates/ path was excluded by default", not any("manual_candidates" in str(p) for p in allowed), allowed)
    check("continuity_backup path was excluded by default", not any("continuity_backup" in str(p) for p in allowed), allowed)
    check("another job's own workdir tree was excluded by default", not any("/jobs/" in str(p) for p in allowed), allowed)
    check("all 3 noisy paths are recorded as excluded with a reason", len(excluded) == 3, excluded)
    check("a normal canonical live source file is NOT excluded", clean[0] in allowed, allowed)


def test_decomposed_source_paths_are_staged_and_excluded_ones_are_recorded_not_copied() -> None:
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate
    runner, calls = _god2_sequenced_runner([_god2_fake_run() for _ in range(3)])
    harness.run_agent_loop = runner
    _god2_validation.validate = lambda *a, **k: type("V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()

    real_src_dir = Path(tempfile.mkdtemp(prefix="omni_p3_src_"))
    (real_src_dir / "helper.py").write_text("# real canonical helper\n")
    excluded_src_dir = Path(tempfile.mkdtemp(prefix="manual_candidates_lookalike_"))
    excluded_marker_dir = excluded_src_dir / "manual_candidates"
    excluded_marker_dir.mkdir()
    (excluded_marker_dir / "withdrawn.py").write_text("# should never be staged\n")

    try:
        r = harness.submit_job_decomposed(
            "synthetic decomposed test: source_paths staging", requested_by="test",
            source_paths=[str(real_src_dir), str(excluded_marker_dir)],
        )
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate
        shutil.rmtree(real_src_dir, ignore_errors=True)
        shutil.rmtree(excluded_src_dir, ignore_errors=True)

    check("job with staged source_paths still succeeds", r.status == "succeeded", r.status)
    staged_helper_files = list(Path(r.workdir).rglob("helper.py"))
    check("the authorized, non-excluded source file WAS copied into the sandbox", len(staged_helper_files) == 1, list(Path(r.workdir).rglob("*")))
    record = job_ledger.load(r.job_id)
    excluded_recorded = (record.submit_params or {}).get("context_staging_excluded", [])
    check("the manual_candidates-marked path was recorded as excluded, not silently dropped",
          any("manual_candidates" in e.get("path", "") for e in excluded_recorded), excluded_recorded)
    check("no file from the excluded manual_candidates tree reached the sandbox",
          not any(p.name == "withdrawn.py" for p in Path(r.workdir).rglob("*")), list(Path(r.workdir).rglob("*")))


def test_classify_validation_failure_distinguishes_test_from_generic() -> None:
    v_test_fail = type("V", (), {"to_json": lambda self: {"passed": False, "checks": [
        {"name": "unit_tests", "passed": False}, {"name": "lint", "passed": True}]}})()
    v_generic_fail = type("V", (), {"to_json": lambda self: {"passed": False, "checks": [
        {"name": "syntax_check", "passed": False}]}})()
    v_broken_to_json = type("V", (), {"to_json": lambda self: (_ for _ in ()).throw(RuntimeError("boom"))})()
    check("a failing check named 'unit_tests' classifies as test_failure",
          harness._classify_validation_failure(v_test_fail) == "test_failure", None)
    check("a failing check with no 'test' in its name classifies as generic validation_failure",
          harness._classify_validation_failure(v_generic_fail) == "validation_failure", None)
    check("a broken to_json() never raises -- classification degrades to generic validation_failure",
          harness._classify_validation_failure(v_broken_to_json) == "validation_failure", None)


def test_narrow_prior_summary_truncates_only_past_the_context_pressure_limit() -> None:
    short = "short summary, well under the limit"
    narrowed_short, was_narrowed_short = harness._narrow_prior_summary(short)
    check("short prior_summary is returned unchanged", narrowed_short == short and was_narrowed_short is False, narrowed_short)

    long = "x" * (harness.CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT + 500)
    narrowed_long, was_narrowed_long = harness._narrow_prior_summary(long)
    check("long prior_summary IS narrowed", was_narrowed_long is True, len(narrowed_long))
    check("narrowed text stays bounded near the configured limit",
          len(narrowed_long) <= harness.CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT + 200, len(narrowed_long))
    check("narrowed text keeps the MOST RECENT content (the tail), not the oldest",
          narrowed_long.endswith("x" * 50), narrowed_long[-10:])


# ---- GOD_MODE_V1 FINAL GAP CLOSURE: live-safe deterministic-fixture proof for the
# 3 retry classes the Phase 3 receipt disclosed as unit-tested-only ----

def test_TEST_FAILURE_live_deterministic_fixture_real_validation_and_repair() -> None:
    """Live-safe deterministic fixture (NOT a mocked validate()): a scripted
    (deterministic, not random-LLM) agent loop writes a REAL calc.py then a
    REAL intentionally-wrong test file. validation.validate() is NOT
    mocked here -- it genuinely runs `python3 -m pytest` as a real
    subprocess against real files on disk and genuinely fails. The repair
    phase then genuinely fixes the file and a second real pytest run
    genuinely passes."""
    original_run = harness.run_agent_loop
    calls = {"n": 0}

    def scripted_runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        calls["n"] += 1
        if calls["n"] == 1:  # inspect
            return _god2_fake_run("finish", "empty sandbox, nothing to inspect yet")
        if calls["n"] == 2:  # implement
            (workdir / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            return _god2_fake_run("finish", "implemented add()")
        if calls["n"] == 3:  # test -- deliberately WRONG assertion
            (workdir / "test_calc.py").write_text(
                "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 5\n"
            )
            return _god2_fake_run("finish", "wrote a test for add()")
        if calls["n"] == 4:  # repair_1 -- genuinely fixes the wrong assertion
            (workdir / "test_calc.py").write_text(
                "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
            )
            return _god2_fake_run("finish", "fixed the failing assertion")
        return _god2_fake_run("finish")

    harness.run_agent_loop = scripted_runner
    try:
        r = harness.submit_job_decomposed("synthetic TEST_FAILURE live-fixture: calc.add()", requested_by="test")
    finally:
        harness.run_agent_loop = original_run

    record = job_ledger.load(r.job_id)
    repair_phase = next((p for p in record.phases if p["name"] == "repair_1"), None)
    check("FAILURE_DETECTED: a repair_1 phase actually ran, proving the FIRST real pytest run genuinely failed",
          repair_phase is not None, record.phases)
    check("FAILURE_CLASSIFIED_CORRECTLY: repair_1 is classified test_failure from the real pytest check name",
          repair_phase is not None and repair_phase.get("failure_class") == "test_failure", repair_phase)
    check("RECOVERY_STRATEGY_CHANGED (IDENTICAL_BLIND_RETRY=NO): the repair objective's wording is specifically "
          "TEST_FAILURE-tailored, not a generic 'try again' retry of the identical implement/test phase objective",
          repair_phase is not None and "TEST_FAILURE" in repair_phase["objective"]
          and repair_phase["objective"] != repair_phase.get("name"),
          repair_phase["objective"][:300] if repair_phase else None)
    check("BOUNDED_RETRY_LIMIT_ENFORCED: exactly 1 repair cycle used, within DECOMPOSED_MAX_REPAIR_CYCLES",
          0 < sum(1 for p in record.phases if p["name"].startswith("repair_")) <= harness.DECOMPOSED_MAX_REPAIR_CYCLES,
          record.phases)
    check("VALID_PROGRESS_PRESERVED: calc.py from the implement phase survived the repair cycle",
          (Path(r.workdir) / "calc.py").exists(), r.workdir)
    check("FALSE_SUCCESS=NO / FINAL_OUTCOME_CORRECT=YES: job only succeeded after the SECOND real pytest run genuinely passed",
          r.status == "succeeded" and bool(r.validation) and r.validation.get("passed") is True, r.validation)
    test_check = next((c for c in (r.validation or {}).get("checks", []) if c.get("name") == "test_discovery_and_run"), None)
    check("the final validation evidence is a REAL, passing pytest subprocess run (not faked)",
          test_check is not None and test_check.get("passed") is True, test_check)


def test_CONTEXT_PRESSURE_live_deterministic_fixture_real_orchestration_narrows_prior_summary() -> None:
    """Live-safe deterministic fixture proving CONTEXT_PRESSURE fires
    INSIDE the real submit_job_decomposed() orchestration (not just via a
    direct call to _narrow_prior_summary()). The char-limit constant is
    temporarily lowered for this test only so 2 real phases' worth of
    (real, 800-char-capped-per-phase) accumulated summaries genuinely
    exceed it -- the narrowing MECHANISM itself is exercised for real."""
    original_run = harness.run_agent_loop
    original_validate = _god2_validation.validate
    original_limit = harness.CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT
    phase_prompts: list[str] = []

    def scripted_runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        phase_prompts.append(task_text)
        return _god2_fake_run("finish", "X" * 2000)  # each phase reports a long summary (truncated to 800 in the record)

    harness.run_agent_loop = scripted_runner
    harness.CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT = 1000  # lowered for this test only
    _god2_validation.validate = lambda *a, **k: type("V", (), {"passed": True, "to_json": lambda self: {"passed": True, "checks": []}})()
    try:
        r = harness.submit_job_decomposed("synthetic CONTEXT_PRESSURE live-fixture: 3 long-summary phases", requested_by="test")
    finally:
        harness.run_agent_loop = original_run
        _god2_validation.validate = original_validate
        harness.CONTEXT_PRESSURE_SUMMARY_CHAR_LIMIT = original_limit

    check("job completes normally", r.status == "succeeded", r.status)
    check("3 base phases ran", len(phase_prompts) == 3, len(phase_prompts))
    check("phase 1 (inspect) has no prior-progress section yet", "VERIFIED PROGRESS SO FAR" not in phase_prompts[0], None)
    check("phase 2's accumulated prior_summary (~800 chars) is still under the (lowered) 1000-char limit -- not yet narrowed",
          "context pressure" not in phase_prompts[1].lower(), phase_prompts[1][-200:])
    check("CONTEXT_PRESSURE fired FOR REAL inside submit_job_decomposed(): phase 3's prompt shows the real narrowing marker",
          "context pressure" in phase_prompts[2].lower(), phase_prompts[2][-400:])


def test_PARTIAL_IMPLEMENTATION_live_deterministic_fixture_real_file_survives_escalation() -> None:
    """Live-safe deterministic fixture: the implement phase genuinely
    writes a real file to disk, then the test phase genuinely escalates.
    Proves partial_progress reflects the REAL file (not an empty dict) and
    that the file is still physically present in the sandbox afterward."""
    original_run = harness.run_agent_loop

    def scripted_runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        # Match the exact phase marker (never a loose substring) -- the
        # task description itself echoes into every phase's prompt, and a
        # loose "IMPLEMENT"/"TEST" substring check would false-match on the
        # word 'PARTIAL_IMPLEMENTATION' in this test's own task text.
        if "CURRENT PHASE: IMPLEMENT" in task_text:
            (workdir / "partial_module.py").write_text("def half_done():\n    pass\n")
            return _god2_fake_run("finish", "wrote partial_module.py, ran out of clear next steps")
        if "CURRENT PHASE: TEST" in task_text:
            return _god2_fake_run("escalate", "cannot determine how to test partial_module.py without more direction")
        return _god2_fake_run("finish", "inspected empty sandbox")

    harness.run_agent_loop = scripted_runner
    try:
        r = harness.submit_job_decomposed("synthetic PARTIAL_IMPLEMENTATION live-fixture", requested_by="test")
    finally:
        harness.run_agent_loop = original_run

    check("job status is escalated", r.status == "escalated", r.status)
    check("FAILURE_DETECTED / VALID_PROGRESS_PRESERVED: partial_progress names the REAL added file",
          r.partial_progress is not None
          and "partial_module.py" in r.partial_progress.get("files_touched_before_escalation", {}).get("added", []),
          r.partial_progress)
    check("the real file is still physically present in the sandbox after escalation (not cleaned up/lost)",
          (Path(r.workdir) / "partial_module.py").exists(), r.workdir)
    check("FALSE_SUCCESS=NO: an escalated job is never promotion_eligible", r.promotion_eligible is False, r.promotion_eligible)
    check("FINAL_OUTCOME_CORRECT: phase_that_escalated correctly names 'test', not 'implement'",
          r.partial_progress is not None and r.partial_progress.get("phase_that_escalated") == "test", r.partial_progress)


def test_adaptive_retry_matrix_covers_all_nine() -> None:
    required_classes = {
        "TOOL_SCHEMA_FAILURE", "ITERATION_STALL", "VALIDATION_FAILURE", "TEST_FAILURE",
        "MODEL_FAILURE", "PROVIDER_FAILURE", "CONTEXT_PRESSURE", "PARTIAL_IMPLEMENTATION", "NO_PROGRESS",
    }
    matrix = harness.ADAPTIVE_RETRY_MATRIX
    check("all 9 required failure classes are present", set(matrix.keys()) == required_classes, sorted(matrix.keys()))
    required_fields = {"DETECTION", "RECOVERY_STRATEGY", "MAX_RETRIES", "FAIL_CLOSED_CONDITION", "ESCALATION_CONDITION"}
    for cls, spec in matrix.items():
        check(f"{cls} has all 5 required fields, non-empty",
              set(spec.keys()) == required_fields and all(str(v).strip() for v in spec.values()), spec)


def test_phase_narrative_preserves_earlier_model_progress_after_later_model_failure() -> None:
    """PHASE_NARRATIVE_ACCURACY: model A does real work then fails
    (iteration_ceiling_reached); model B then also fails with zero new
    work. The phase's own recorded final_action is B's failure -- but the
    attempt history must still show A's real file, and progress_preserved
    must be True, not silently erased by B's failure."""
    original_run = harness.run_agent_loop
    original_check = _mrsilent_test_local_model_health.check
    original_order = _mrsilent_test_local_model_health.engineering_failover_order

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        if model == "qwen3-coder:30b":
            (workdir / "real_progress.py").write_text("# model A really wrote this\n")
            return _god2_fake_run("iteration_ceiling_reached", "ran out of iterations after writing the file")
        return _god2_fake_run("error", "model B failed immediately, wrote nothing")

    _mrsilent_test_local_model_health.check = lambda **k: type("H", (), {"available": True, "models": ["qwen3-coder:30b", "qwen3.6:27b"]})()
    _mrsilent_test_local_model_health.engineering_failover_order = lambda exclude=None: [m for m in ["qwen3-coder:30b", "qwen3.6:27b"] if m not in (exclude or [])]
    harness.run_agent_loop = runner
    tmp = Path(tempfile.mkdtemp())
    try:
        run, mdl, attempted, attempt_history = harness._run_phase(
            "implement", "objective", tmp, parent_task="x", prior_summary="",
            model="qwen3-coder:30b", max_iterations=6, timeout_s=60,
        )
    finally:
        harness.run_agent_loop = original_run
        _mrsilent_test_local_model_health.check = original_check
        _mrsilent_test_local_model_health.engineering_failover_order = original_order

    check("phase's own final_action is B's failure (the real, honest outcome)", run.final_action == "error", run.final_action)
    check("2 attempts recorded", len(attempt_history) == 2, attempt_history)
    check("model A's attempt entry shows its real file in files_touched",
          "real_progress.py" in attempt_history[0]["files_touched"].get("added", []), attempt_history[0])
    check("model B's attempt entry shows zero files touched",
          not any(attempt_history[1]["files_touched"].get(k) for k in ("added", "modified", "removed")), attempt_history[1])
    # Simulate _record_phase's own progress_preserved computation directly.
    progress_preserved = any(
        any(a["files_touched"].get(k) for k in ("added", "modified", "removed")) for a in attempt_history)
    check("progress_preserved=True despite the LAST attempt being a failure (FALSE_PHASE_SUCCESS=NO, but progress is not erased)",
          progress_preserved is True, attempt_history)


def test_no_progress_detection_stops_before_exhausting_every_candidate_model() -> None:
    """NO_PROGRESS: two consecutive zero-diff retryable attempts must stop
    the failover loop early rather than burning every remaining candidate
    on a strategy that demonstrably isn't working."""
    original_run = harness.run_agent_loop
    original_check = _mrsilent_test_local_model_health.check
    original_order = _mrsilent_test_local_model_health.engineering_failover_order
    attempts_made = []

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        attempts_made.append(model)
        return _god2_fake_run("error", "no-op, wrote nothing")  # never writes any file -> zero diff every time

    _mrsilent_test_local_model_health.check = lambda **k: type("H", (), {"available": True, "models": ["m1", "m2", "m3"]})()
    _mrsilent_test_local_model_health.engineering_failover_order = lambda exclude=None: [m for m in ["m1", "m2", "m3"] if m not in (exclude or [])]
    harness.run_agent_loop = runner
    tmp = Path(tempfile.mkdtemp())
    try:
        run, mdl, attempted, attempt_history = harness._run_phase(
            "implement", "objective", tmp, parent_task="x", prior_summary="",
            model="m1", max_iterations=6, timeout_s=60,
        )
    finally:
        harness.run_agent_loop = original_run
        _mrsilent_test_local_model_health.check = original_check
        _mrsilent_test_local_model_health.engineering_failover_order = original_order

    check("stopped after 2 zero-diff attempts, never tried the 3rd candidate (m3)",
          attempts_made == ["m1", "m2"], attempts_made)
    check("the second attempt is flagged no_progress_stop", attempt_history[-1].get("no_progress_stop") is True, attempt_history)


def test_run_phase_falls_over_to_provider_b_when_ollama_candidates_exhausted() -> None:
    """PROVIDER_FAILURE at the phase level: once same-provider (ollama)
    candidates are exhausted, the phase tries the independent provider_b
    exactly once before giving up."""
    import provider_b_bridge
    original_run = harness.run_agent_loop
    original_check = _mrsilent_test_local_model_health.check
    original_order = _mrsilent_test_local_model_health.engineering_failover_order
    original_circuit_open = _mrsilent_test_local_model_health.circuit_is_open
    original_ensure_running = provider_b_bridge.ensure_running
    calls = []

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        calls.append((model, provider))
        if provider == "provider_b":
            return _god2_fake_run("finish", "provider_b saved the day")
        return _god2_fake_run("error", "ollama model failed")

    _mrsilent_test_local_model_health.check = lambda **k: type("H", (), {"available": True, "models": ["m1"]})()
    _mrsilent_test_local_model_health.engineering_failover_order = lambda exclude=None: [m for m in ["m1"] if m not in (exclude or [])]
    _mrsilent_test_local_model_health.circuit_is_open = lambda provider: False
    provider_b_bridge.ensure_running = lambda **k: type("H", (), {"available": True})()
    harness.run_agent_loop = runner
    tmp = Path(tempfile.mkdtemp())
    try:
        run, mdl, attempted, attempt_history = harness._run_phase(
            "implement", "objective", tmp, parent_task="x", prior_summary="",
            model="m1", max_iterations=6, timeout_s=60,
        )
    finally:
        harness.run_agent_loop = original_run
        _mrsilent_test_local_model_health.check = original_check
        _mrsilent_test_local_model_health.engineering_failover_order = original_order
        _mrsilent_test_local_model_health.circuit_is_open = original_circuit_open
        provider_b_bridge.ensure_running = original_ensure_running

    check("ollama candidate m1 tried first, then provider_b", calls == [("m1", "ollama"), (provider_b_bridge.DEFAULT_MODEL, "provider_b")], calls)
    check("phase ultimately finished cleanly via provider_b", run.final_action == "finish", run.final_action)
    check("provider_b attempt is recorded in attempt_history", attempt_history[-1]["provider"] == "provider_b", attempt_history)


def test_run_phase_fails_closed_not_crashed_when_llama_server_binary_is_missing() -> None:
    """GOD_MODE_V1 FINAL PROVIDER_B CLOSURE regression: a real, reproduced
    bug found this session -- provider_b_bridge.ensure_running() raises an
    UNCAUGHT FileNotFoundError (not ModelArtifactMissing) when its
    llama-server binary itself is absent (confirmed live on this machine:
    a locally-manifested model reaches subprocess.Popen() and crashes).
    Every ensure_running() call site must now treat FileNotFoundError
    identically to ModelArtifactMissing -- fail closed (provider_b
    unavailable, fall through), never propagate and crash the job."""
    import provider_b_bridge
    original_run = harness.run_agent_loop
    original_check = _mrsilent_test_local_model_health.check
    original_order = _mrsilent_test_local_model_health.engineering_failover_order
    original_circuit_open = _mrsilent_test_local_model_health.circuit_is_open
    original_ensure_running = provider_b_bridge.ensure_running

    def runner(task_text, workdir, *, model, provider, max_iterations, timeout_s, allowed_tools):
        return _god2_fake_run("error", "ollama model failed")  # always retryable -- forces the provider_b attempt

    def raise_missing_binary(**k):
        raise FileNotFoundError(2, "No such file or directory", "/usr/local/lib/ollama/llama-server")

    _mrsilent_test_local_model_health.check = lambda **k: type("H", (), {"available": True, "models": ["m1"]})()
    _mrsilent_test_local_model_health.engineering_failover_order = lambda exclude=None: [m for m in ["m1"] if m not in (exclude or [])]
    _mrsilent_test_local_model_health.circuit_is_open = lambda provider: False
    provider_b_bridge.ensure_running = raise_missing_binary
    harness.run_agent_loop = runner
    tmp = Path(tempfile.mkdtemp())
    raised: Exception | None = None
    try:
        run, mdl, attempted, attempt_history = harness._run_phase(
            "implement", "objective", tmp, parent_task="x", prior_summary="",
            model="m1", max_iterations=6, timeout_s=60,
        )
    except Exception as e:  # noqa: BLE001 — this IS the failure mode under test
        raised = e
    finally:
        harness.run_agent_loop = original_run
        _mrsilent_test_local_model_health.check = original_check
        _mrsilent_test_local_model_health.engineering_failover_order = original_order
        _mrsilent_test_local_model_health.circuit_is_open = original_circuit_open
        provider_b_bridge.ensure_running = original_ensure_running

    check("_run_phase() does NOT crash with an uncaught FileNotFoundError", raised is None, raised)
    check("the phase still returns its real (ollama-failure) outcome, never a fabricated success",
          raised is None and run.final_action == "error", run.final_action if raised is None else None)


if __name__ == "__main__":
    test_system_prompt_matches_seeded_source_guard_v2()
    test_write_and_read_roundtrip()
    test_write_file_sandbox_rejects_path_escape()
    test_write_file_sandbox_rejects_protected_marker()
    test_seeded_whole_file_replacement_is_blocked()
    test_new_file_creation_remains_allowed_with_seeded_guard()
    test_canonical_source_path_lineage_is_preserved()
    test_apply_patch_sandbox_create_new_file()
    test_apply_patch_sandbox_requires_unique_match()
    test_run_command_rejects_disallowed_binary()
    test_run_command_rejects_gated_keyword()
    test_run_command_allows_python3_and_bash()
    test_grep_and_list_files()
    test_inspect_diff_reports_added_file()
    test_parse_tool_call_rejects_unknown_tool()
    test_parse_tool_call_repairs_prose_wrapped_json()
    test_parse_tool_call_repairs_trailing_comma()
    test_parse_tool_call_repair_never_fabricates_values()
    test_parse_tool_call_repair_still_refuses_unrepairable()
    test_filter_actually_installed_excludes_configured_but_missing_model()
    test_filter_actually_installed_matches_base_name_without_exact_tag()
    test_filter_actually_installed_empty_when_nothing_real()
    test_gated_task_is_rejected_without_running_agent()
    test_allowed_tools_structurally_refuses_a_disallowed_tool_call()
    test_allowed_tools_none_keeps_full_backward_compatible_access()
    test_harness_copies_authorized_source_paths_into_the_sandbox()
    test_json_safe_submit_value_contract()
    test_submit_job_json_safe_boundary_accepts_set()
    test_h2_ledger_fields_are_backward_compatible()
    test_h2_finalize_persists_provider_and_fallback_reason()
    test_h3_canonical_identity_compatibility()
    test_h3_jobresult_preserves_machine_id_and_canonical_name()
    test_h4_backend_contract_core()
    test_h4_builtin_backend_behavioral_parity()
    test_h4_future_backend_requires_no_agent_branch()
    test_h4_agent_loop_uses_normalized_backend_seam()
    test_harness_rejects_a_protected_source_path()
    test_resume_never_recopies_source_paths_after_partial_edits()
    test_decomposed_happy_path_records_all_phases_and_succeeds()
    test_decomposed_escalate_stops_immediately_no_later_phases()
    test_decomposed_validation_failure_triggers_bounded_repair_then_succeeds()
    test_decomposed_repair_cycles_are_bounded()
    test_decomposed_gated_task_never_runs_a_phase()
    test_decomposed_phase_state_survives_simulated_crash_and_resume_reads_it()
    test_decomposed_model_failover_within_a_phase_uses_real_installed_model_only()
    test_decomposed_ledger_record_carries_real_files_touched_and_promotion_eligible()
    test_classify_complexity_simple_one_file_edit_is_not_eligible()
    test_classify_complexity_length_alone_is_not_enough()
    test_classify_complexity_multi_stage_synthetic_task_is_eligible()
    test_submit_job_auto_dispatches_simple_task_to_submit_job()
    test_submit_job_auto_dispatches_complex_task_to_submit_job_decomposed()
    test_decomposed_phase_iteration_ceiling_is_ten_and_finite()
    test_submit_job_auto_decomposed_path_still_wired_to_the_module_ceiling()
    test_ollama_request_carries_explicit_finite_num_ctx()
    test_provider_b_path_is_structurally_unaffected_by_ollama_num_ctx()
    test_single_shot_mode_is_explicitly_selected_not_silently_global()
    test_normal_multi_turn_agent_loop_remains_available()
    test_single_shot_rejects_a_path_outside_the_allowed_paths_allowlist()
    test_single_shot_rejects_path_traversal()
    test_single_shot_rejects_a_protected_secret_path()
    test_single_shot_rejects_malformed_model_output_safely()
    test_single_shot_rejects_out_of_scope_patch_exceeding_max_files()
    test_single_shot_validation_failure_does_not_promote()
    test_single_shot_paid_resource_gate_preserved()
    test_single_shot_founder_promotion_gate_preserved()
    test_single_shot_path_also_sends_the_bounded_num_ctx()
    test_context_staging_excludes_secret_paths_by_default()
    test_context_staging_excludes_manual_candidates_and_backups_by_default()
    test_decomposed_source_paths_are_staged_and_excluded_ones_are_recorded_not_copied()
    test_classify_validation_failure_distinguishes_test_from_generic()
    test_narrow_prior_summary_truncates_only_past_the_context_pressure_limit()
    test_TEST_FAILURE_live_deterministic_fixture_real_validation_and_repair()
    test_CONTEXT_PRESSURE_live_deterministic_fixture_real_orchestration_narrows_prior_summary()
    test_PARTIAL_IMPLEMENTATION_live_deterministic_fixture_real_file_survives_escalation()
    test_adaptive_retry_matrix_covers_all_nine()
    test_phase_narrative_preserves_earlier_model_progress_after_later_model_failure()
    test_no_progress_detection_stops_before_exhausting_every_candidate_model()
    test_run_phase_falls_over_to_provider_b_when_ollama_candidates_exhausted()
    test_run_phase_fails_closed_not_crashed_when_llama_server_binary_is_missing()
    test_full_target_loop_live()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL TESTS PASSED")
