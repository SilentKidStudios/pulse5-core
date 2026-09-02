"""
OmniEngineer Agent — the local coding agent side of OmniEngineer V0.1's TARGET
LOOP (see omniengineer_harness.py for the orchestrator this plugs into):

    TASK -> PLAN -> SANDBOX -> LIST/SEARCH/READ -> PATCH -> TEST ->
    INSPECT FAILURE -> BOUNDED RETRY -> VALIDATE -> CANARY -> PROMOTION CANDIDATE

This module owns the middle of that loop (LIST/SEARCH/READ -> PATCH -> TEST ->
INSPECT FAILURE), driven by qwen3-coder:30b on the same Ollama server
local_model_bridge.py talks to. PLAN, SANDBOX setup, whole-loop BOUNDED RETRY,
VALIDATE, CANARY, and PROMOTION CANDIDATE are the harness's job, not this
module's.

Unlike Claude Code and Codex, Ollama's /api/generate is a bare completion
endpoint with no agentic tool-use harness of its own — so this module builds
one: a bounded ReAct loop that asks the model for exactly one JSON tool call
per turn, executes that tool against the sandbox, and feeds the result back
as the next turn's context. Every tool is a plain Python function scoped to
one job's sandbox directory; nothing here ever grants the model raw shell,
network, or any path outside that directory. The one tool that runs a real
subprocess (`run_command`) is jailed exactly the way validation.py jails its
own checks: argv[0] must be python3 or bash, the full command is checked
against authority_policy.GATED_KEYWORDS, and it stays inside the sandbox.

Bounded by construction, not by asking the model nicely:
  - MAX_ITERATIONS tool calls per run (plan: iteration_ceiling)
  - MAX_MALFORMED_RETRIES malformed-JSON retries per turn before the run
    auto-escalates (plan: bounded_repair)
  - TOOL_TIMEOUT_S per run_command invocation (plan: 60s per operation)
  - an optional wall-clock timeout_s budget for the whole run (checked before
    every model call; exceeding it ends the run with final_action="timeout",
    never a silent hang)
  - `finish` and `escalate` are the only two terminal tool calls; running out
    of iterations or time is treated as an implicit stop, never a silent one
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import validation
from authority_policy import GATED_KEYWORDS, GATED_PATH_MARKERS
from env_util import minimal_env
from local_model_bridge import DEFAULT_MODEL, OLLAMA_BASE_URL
import backend_contract

MAX_ITERATIONS = 10
MAX_MALFORMED_RETRIES = 3
TOOL_TIMEOUT_S = 60
MODEL_CALL_TIMEOUT_S = 120
ALLOWED_BINARIES = frozenset({"python3", "bash"})
MAX_READ_CHARS = 8000
MAX_LIST_ENTRIES = 500
MAX_GREP_MATCHES = 50

# Resource budgets (#6 hardening) — harness-enforced, never chosen by the
# model. Distinct from MAX_ITERATIONS: a single iteration can cost up to
# MAX_MALFORMED_RETRIES model calls, so MAX_MODEL_CALLS is a separate, tighter
# hard stop that catches a pathological "malformed JSON on every turn" run
# before it burns the full iteration*retry worst case.
MAX_MODEL_CALLS = 20
MAX_FILES_CHANGED = 25
# V10.5 real-incident fix (Founder decision, ANDROID WALK-AWAY CLOSURE
# directive section 1, 2026-08-27): raised from 200_000 to the Founder's
# own explicitly authorized bounded ceiling (<=327_680) after a real,
# live closed-repair-loop attempt found the OLD 200_000 limit blocked
# apply_patch_sandbox from EVER patching MR. SILENT's real ~230KB Founder
# OS HTML asset, regardless of how small the actual intended edit was --
# the check ran against the FULL resulting file size, not the size of
# what the model actually generated. This constant now governs the
# WHOLE-FILE ceiling (write_file_sandbox's full content; apply_patch_
# sandbox's create-new-file content; and apply_patch_sandbox's resulting
# post-patch file size). It deliberately does NOT bound what the model
# must GENERATE in a single apply_patch_sandbox replace call -- see
# MAX_PATCH_GENERATED_CHARS below, which keeps that separately and much
# more tightly bounded regardless of the target file's own size. The
# Founder was explicit: do not remove this safety limit globally, do not
# set it unbounded, and do not exceed 327_680 merely for convenience --
# this file's real, current size (~230KB) is the actual evidence for the
# value chosen, not an arbitrary round number.
MAX_FILE_SIZE_CHARS = 327_680  # <=Founder-authorized ceiling; ~230KB real asset + headroom, never merely for convenience
# The real per-call "chunked edit" bound: apply_patch_sandbox's old_string/
# new_string primitive already only ever requires the MODEL to generate a
# small, targeted replacement snippet (found via grep first), never the
# whole file -- this constant makes that bound explicit and independently
# enforced, so a large target file's own size can never be used to justify
# an unbounded single-call replacement.
MAX_PATCH_GENERATED_CHARS = 200_000
MAX_TRANSCRIPT_CHARS = 60_000  # context-size safeguard; oldest turns truncated first

# OLLAMA_NUM_CTX_REPAIR (Founder-authorized, 2026-09-02): _call_ollama() below
# never sent an explicit num_ctx, so Ollama silently used its own small
# default context window (confirmed live: 4096 tokens) regardless of
# MAX_TRANSCRIPT_CHARS above -- a real ~21KB seeded source file plus a few
# turns of transcript can exceed 4096 tokens on its own, causing the model
# to lose the exact file content apply_patch_sandbox needs for an exact-match
# old_string, well before MAX_TRANSCRIPT_CHARS's own 60,000-character budget
# is reached. 32768 tokens was chosen empirically, not assumed: confirmed via
# a real /api/generate load call to hold comfortable headroom over
# MAX_TRANSCRIPT_CHARS (60,000 chars is ~15,000-24,000 tokens depending on
# content density -- comfortably under 32768 in every realistic case), stays
# far below qwen3-coder:30b's own model-reported max (262144, confirmed via
# /api/show's model_info.qwen3moe.context_length), and was confirmed to load
# fully GPU-resident with headroom to spare (~1GB free of 20GB VRAM) rather
# than spilling to slower CPU/RAM. Finite, bounded, never "unlimited".
OLLAMA_NUM_CTX = 32768

TOOL_NAMES = frozenset({
    "list_files", "read_file", "grep", "inspect_diff",
    "write_file_sandbox", "apply_patch_sandbox",
    "run_validator", "run_command", "finish", "escalate",
})

SYSTEM_PROMPT = """You are OmniEngineer, a bounded local coding agent working inside ONE isolated sandbox directory. You cannot see or touch anything outside it. You have exactly these tools:

- list_files {{"path": "."}} -> list files under path (relative to sandbox root)
- read_file {{"path": "..."}} -> read a file's contents
- grep {{"pattern": "...", "path": "."}} -> regex-search files under path
- inspect_diff {{}} -> see everything you've changed so far this run
- write_file_sandbox {{"path": "...", "content": "..."}} -> create a NEW file, or rewrite a file created during this run; a seeded file that existed at agent-run start cannot be whole-file replaced, so edit seeded mature source surgically with apply_patch_sandbox instead [SEEDED_TOOL_CONTRACT_ALIGNMENT_V2]
- apply_patch_sandbox {{"path": "...", "old_string": "...", "new_string": "..."}} -> replace ONE exact, unique occurrence of old_string with new_string in an existing file (old_string="" means create a NEW file with new_string as its full content)
- run_validator {{}} -> run the automatic validation gate against your current changes
- run_command {{"argv": ["python3", ...]}} -> run an allowlisted command (python3 or bash only) inside the sandbox, e.g. to run tests
- finish {{"summary": "..."}} -> you are done; stop
- escalate {{"reason": "..."}} -> you cannot safely/correctly proceed; stop and ask a human

Respond with EXACTLY ONE JSON object per turn, nothing else, in this shape:
{{"thought": "brief reasoning", "tool": "<one of the tool names above>", "args": {{...}}}}

Rules:
- You get at most {max_iterations} tool calls total. Use them efficiently.
- apply_patch_sandbox requires old_string to match EXACTLY ONCE in the file — if it's not found or matches more than once, you'll be told and should try a more specific old_string.
- If run_command shows a test FAILING, your very next tool call must be apply_patch_sandbox (or write_file_sandbox) to fix the actual file — do not spend multiple turns re-running ad-hoc diagnostic commands (e.g. python3 -c "...") without persisting a fix in between. Diagnose once, then patch.
- Call run_validator before finish whenever you've changed files, and read its result.
- If you're stuck, unsure the task is safe/well-specified, or about to guess destructively, call escalate instead of guessing.
"""


@dataclass
class ToolCall:
    thought: str
    tool: str
    args: dict[str, Any]


@dataclass
class TurnRecord:
    iteration: int
    tool: str
    args: dict[str, Any]
    thought: str
    result_excerpt: str
    ok: bool
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AgentRunResult:
    task: str
    sandbox: str
    model: str
    final_action: str  # finish | escalate | iteration_ceiling_reached | timeout | model_unavailable | error
    summary_or_reason: str
    provider: str = "ollama"  # SINGLE_PROVIDER_DEPENDENCY: which inference provider actually served this run
    turns: list[dict[str, Any]] = field(default_factory=list)
    commands_executed: list[str] = field(default_factory=list)
    model_calls: int = 0
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0


def _within_sandbox(path: Path, sandbox: Path) -> bool:
    try:
        path.resolve().relative_to(sandbox.resolve())
        return True
    except ValueError:
        return False


def _safe_path(sandbox: Path, rel: str) -> Path | None:
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    p = sandbox / rel
    if not _within_sandbox(p, sandbox):
        return None
    p_str = str(p)
    for marker in GATED_PATH_MARKERS:
        if marker.lower() in p_str.lower():
            return None
    return p


def _call_ollama(prompt: str, *, model: str, timeout_s: int) -> str:
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "format": _TOOL_CALL_JSON_SCHEMA,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read().decode())
        return body.get("response", "")


_TOOL_CALL_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": "string", "enum": sorted(TOOL_NAMES)},
        "args": {"type": "object"},
    },
    "required": ["thought", "tool", "args"],
}


def _call_provider_b(prompt: str, *, model: str, timeout_s: int) -> str:
    """Cross-provider fallback (Founder-authorized 2026-08-18): a genuinely
    independent inference provider (see provider_b_bridge.py), not the
    ollama daemon. Import kept local for the same reason local_model_health
    is imported locally elsewhere in this project — avoids a module-load-
    time dependency for the common (provider="ollama") path.

    Passes a grammar-constrained json_schema matching TOOL_NAMES — this
    provider's underlying model (gpt-oss's harmony chat format) does not
    reliably produce parseable tool-call JSON from instructions alone the
    way Ollama's own `format: "json"` does for _call_ollama() above;
    confirmed live via a real end-to-end job that malformed-retried 3/3
    without this constraint before it was added."""
    import provider_b_bridge
    return provider_b_bridge.generate(prompt, model=model, timeout_s=timeout_s, json_schema=_TOOL_CALL_JSON_SCHEMA)


def _parse_tool_call_strict(text: str) -> ToolCall | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = text.rsplit("```", 1)[0]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    tool = obj.get("tool")
    if tool not in TOOL_NAMES:
        return None
    args = obj.get("args")
    if not isinstance(args, dict):
        args = {}
    return ToolCall(thought=str(obj.get("thought", "")), tool=tool, args=args)


def _extract_balanced_json_object(text: str) -> str | None:
    """Finds the first balanced {...} substring in text, respecting string
    literals so a '}' inside a quoted value can't end the object early.
    Purely structural -- locates where a JSON object starts/ends among
    surrounding prose the model may have added; never alters content inside
    it. Returns None if no balanced object is found."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _strip_trailing_commas(text: str) -> str:
    """Removes a trailing comma immediately before a closing } or ] -- a
    common, unambiguous, purely-structural malformed-JSON pattern. Operates
    on the raw text (not string-literal-aware) but the pattern
    (comma-then-whitespace-then-closer) cannot occur inside a valid JSON
    string value without the string itself already being malformed, so this
    cannot silently corrupt legitimate content."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _repair_tool_call_json(raw: str) -> str | None:
    """Bounded, deterministic, content-preserving repair for near-miss
    tool-call JSON (extra prose/markdown around the object, a trailing
    comma). Returns a candidate string to re-parse, or None if there is
    nothing safe to try. Never invents or alters a field VALUE -- only
    extracts/reformats structure the model already wrote. The result is
    re-validated through the exact same strict schema check as any other
    input, so a repair that still doesn't parse or match the tool/args
    contract is correctly treated as still malformed, not executed."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = text.rsplit("```", 1)[0]
    candidate = _extract_balanced_json_object(text)
    if candidate is None:
        return None
    repaired = _strip_trailing_commas(candidate)
    return repaired if repaired != raw.strip() else None


def _parse_tool_call(raw: str) -> ToolCall | None:
    call = _parse_tool_call_strict(raw)
    if call is not None:
        return call
    repaired = _repair_tool_call_json(raw)
    if repaired is None:
        return None
    return _parse_tool_call_strict(repaired)


# ---- tool implementations ---------------------------------------------------

def _tool_list_files(sandbox: Path, args: dict) -> tuple[bool, str]:
    rel = args.get("path", ".")
    base = sandbox if rel in (".", "") else _safe_path(sandbox, rel)
    if base is None:
        return False, f"refused: path {rel!r} is outside the sandbox or matches a protected marker"
    if not base.exists():
        return False, f"path {rel!r} does not exist"
    entries = sorted(str(p.relative_to(sandbox)) for p in base.rglob("*") if p.is_file())
    truncated = len(entries) > MAX_LIST_ENTRIES
    entries = entries[:MAX_LIST_ENTRIES]
    return True, json.dumps({"files": entries, "truncated": truncated})


def _tool_read_file(sandbox: Path, args: dict) -> tuple[bool, str]:
    rel = args.get("path", "")
    p = _safe_path(sandbox, rel)
    if p is None:
        return False, f"refused: path {rel!r} is outside the sandbox or matches a protected marker"
    if not p.exists() or not p.is_file():
        return False, f"file {rel!r} does not exist"
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        return False, f"could not read {rel!r}: {e}"
    truncated = len(text) > MAX_READ_CHARS
    return True, json.dumps({"content": text[:MAX_READ_CHARS], "truncated": truncated})


def _tool_grep(sandbox: Path, args: dict) -> tuple[bool, str]:
    pattern = args.get("pattern", "")
    rel = args.get("path", ".")
    base = sandbox if rel in (".", "") else _safe_path(sandbox, rel)
    if base is None:
        return False, f"refused: path {rel!r} is outside the sandbox or matches a protected marker"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return False, f"invalid regex {pattern!r}: {e}"
    matches: list[str] = []
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f.relative_to(sandbox)}:{i}: {line.strip()[:200]}")
                    if len(matches) >= MAX_GREP_MATCHES:
                        break
        except OSError:
            continue
        if len(matches) >= MAX_GREP_MATCHES:
            break
    return True, json.dumps({"matches": matches, "truncated": len(matches) >= MAX_GREP_MATCHES})


def _snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _tool_inspect_diff(sandbox: Path, _args: dict, before: dict[str, str]) -> tuple[bool, str]:
    after = _snapshot(sandbox)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    previews = {}
    for rel in (added + modified)[:10]:
        p = sandbox / rel
        try:
            previews[rel] = p.read_text(errors="replace")[:1000]
        except OSError:
            previews[rel] = "<binary or unreadable>"
    return True, json.dumps({"added": added, "modified": modified, "removed": removed, "previews": previews})


def _touched_since(sandbox: Path, before: dict[str, str]) -> set[str]:
    after = _snapshot(sandbox)
    added_or_modified = {k for k in after if k not in before or after[k] != before[k]}
    return added_or_modified


def _check_resource_budget(sandbox: Path, before: dict[str, str], rel: str, content_len: int, *, limit: int = MAX_FILE_SIZE_CHARS, limit_name: str = "MAX_FILE_SIZE_CHARS") -> str | None:
    if content_len > limit:
        return f"refused: {content_len} chars exceeds {limit_name}={limit} for {rel!r}"
    touched = _touched_since(sandbox, before)
    if rel not in touched and len(touched) >= MAX_FILES_CHANGED:
        return f"refused: writing {rel!r} would exceed MAX_FILES_CHANGED={MAX_FILES_CHANGED} (already touched: {len(touched)})"
    return None


def _tool_write_file_sandbox(sandbox: Path, args: dict, before: dict[str, str]) -> tuple[bool, str]:
    rel = args.get("path", "")
    content = args.get("content", "")
    p = _safe_path(sandbox, rel)
    if p is None:
        return False, f"refused: path {rel!r} is outside the sandbox or matches a protected marker"
    if not isinstance(content, str):
        return False, "content must be a string"
    budget_error = _check_resource_budget(sandbox, before, rel, len(content))
    if budget_error:
        return False, budget_error
    p.parent.mkdir(parents=True, exist_ok=True)
    # SEEDED_WHOLE_FILE_REPLACEMENT_GUARD_V2
    seeded_rel = str(p.relative_to(sandbox))
    if seeded_rel in before:
        return False, (
            f"refused: {rel!r} existed at agent-run start; "
            "write_file_sandbox cannot replace seeded source files. "
            "Use apply_patch_sandbox for a surgical edit."
        )

    p.write_text(content)
    return True, f"wrote {len(content)} chars to {rel}"


def _tool_apply_patch_sandbox(sandbox: Path, args: dict, before: dict[str, str]) -> tuple[bool, str]:
    """V0.1 patch primitive: unique-exact-match replace, deliberately mirroring
    this project's own Edit tool semantics (old_string/new_string, must match
    exactly once) rather than a unified-diff engine — far more reliable for a
    30B local model to generate correctly than diff hunks with line offsets."""
    rel = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    p = _safe_path(sandbox, rel)
    if p is None:
        return False, f"refused: path {rel!r} is outside the sandbox or matches a protected marker"
    if not isinstance(old, str) or not isinstance(new, str):
        return False, "old_string and new_string must be strings"
    if old == "":
        if p.exists():
            return False, f"{rel!r} already exists; old_string=\"\" only creates NEW files"
        budget_error = _check_resource_budget(sandbox, before, rel, len(new))
        if budget_error:
            return False, budget_error
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new)
        return True, f"created {rel} ({len(new)} chars)"
    if not p.exists():
        return False, f"file {rel!r} does not exist — use old_string=\"\" to create it"
    # V10.5 real-incident fix (Founder decision, ANDROID WALK-AWAY CLOSURE
    # directive section 1/2, 2026-08-27): two SEPARATE, independently
    # enforced bounds now apply to a patch, replacing the old single
    # whole-resulting-file check that blocked any edit to a real,
    # legitimately-large, already-existing asset regardless of how small
    # the actual replacement was:
    #   1. the NEW content the model itself generated for this one call
    #      (len(new)) must stay within MAX_PATCH_GENERATED_CHARS -- this
    #      is the real per-call "chunked edit" bound: the model was never
    #      required to read or generate the whole file, only a small
    #      targeted replacement (found via grep first), so this bound
    #      does not grow just because the target file is large.
    #   2. the RESULTING whole file must stay within MAX_FILE_SIZE_CHARS
    #      (the Founder-authorized <=327_680 bounded ceiling) -- this is
    #      the real growth guard, preventing the file from being patched
    #      past that ceiling over any number of edits.
    new_content_error = _check_resource_budget(
        sandbox, before, rel, len(new), limit=MAX_PATCH_GENERATED_CHARS, limit_name="MAX_PATCH_GENERATED_CHARS",
    )
    if new_content_error:
        return False, new_content_error
    text = p.read_text(errors="replace")
    before_hash = hashlib.sha256(text.encode()).hexdigest()
    count = text.count(old)
    if count == 0:
        return False, f"old_string not found in {rel!r} — re-read the file and match it exactly"
    if count > 1:
        return False, f"old_string matches {count} times in {rel!r} — make it more specific (include more context)"
    patched = text.replace(old, new, 1)
    whole_file_error = _check_resource_budget(sandbox, before, rel, len(patched))
    if whole_file_error:
        return False, whole_file_error
    p.write_text(patched)
    # Real, durable, audit-visible before/after hash record (Founder
    # requirement, section 1/2: "record before/after hashes") -- both
    # hashes are of the FULL file content, computed the SAME way
    # _snapshot() already hashes every sandbox file for diffing, so this
    # is independently re-derivable/verifiable from the sandbox itself,
    # never a value this tool could fabricate undetected.
    after_hash = hashlib.sha256(patched.encode()).hexdigest()
    return True, f"patched {rel} (1 replacement, {len(patched)} chars) sha256_before={before_hash} sha256_after={after_hash}"


def _run_allowlisted(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int | None, str]:
    if not argv or argv[0] not in ALLOWED_BINARIES:
        bad = argv[0] if argv else ""
        return None, f"refused: {bad!r} is not in ALLOWED_BINARIES {sorted(ALLOWED_BINARIES)}"
    cmd_str = " ".join(argv)
    for pattern in GATED_KEYWORDS:
        if pattern.search(cmd_str):
            return None, f"refused: command matched gated keyword pattern {pattern.pattern!r}"
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), env=minimal_env(),
            capture_output=True, text=True, timeout=min(timeout, TOOL_TIMEOUT_S),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[:4000]
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return None, f"binary not found: {e}"


def _tool_run_command(sandbox: Path, args: dict, commands_executed: list[str]) -> tuple[bool, str]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return False, "argv must be a list of strings"
    commands_executed.append(" ".join(argv))
    exit_code, out = _run_allowlisted(argv, cwd=sandbox, timeout=TOOL_TIMEOUT_S)
    if exit_code is None:
        return False, out
    return exit_code == 0, json.dumps({"exit_code": exit_code, "output": out})


def _tool_run_validator(sandbox: Path, _args: dict, before: dict[str, str]) -> tuple[bool, str]:
    after = _snapshot(sandbox)
    added = sorted(set(after) - set(before))
    modified = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    result = validation.validate(sandbox, {"added": added, "modified": modified, "removed": []}, config=None)
    failed = [c["name"] for c in result.checks if not c.get("passed", True)]
    return result.passed, json.dumps({"passed": result.passed, "failed_checks": failed, "skipped": result.skipped})


def _execute_tool(call: ToolCall, sandbox: Path, before: dict[str, str], commands_executed: list[str]) -> tuple[bool, str]:
    try:
        if call.tool == "list_files":
            return _tool_list_files(sandbox, call.args)
        if call.tool == "read_file":
            return _tool_read_file(sandbox, call.args)
        if call.tool == "grep":
            return _tool_grep(sandbox, call.args)
        if call.tool == "inspect_diff":
            return _tool_inspect_diff(sandbox, call.args, before)
        if call.tool == "write_file_sandbox":
            return _tool_write_file_sandbox(sandbox, call.args, before)
        if call.tool == "apply_patch_sandbox":
            return _tool_apply_patch_sandbox(sandbox, call.args, before)
        if call.tool == "run_validator":
            return _tool_run_validator(sandbox, call.args, before)
        if call.tool == "run_command":
            return _tool_run_command(sandbox, call.args, commands_executed)
    except Exception as e:  # noqa: BLE001 — fed back to the model as a tool error, loop continues
        return False, f"tool {call.tool} raised: {e!r}"
    return False, f"unknown tool {call.tool!r}"


def _truncate_transcript(transcript: str, prefix_len: int) -> str:
    """Context-size safeguard: if the transcript has grown past
    MAX_TRANSCRIPT_CHARS, keep the fixed system+task prefix (never truncated
    — the model must always see its instructions and the original task) and
    the most recent turns, dropping the middle. Cheap and deterministic;
    no summarization, just a clear marker so the model knows history was
    cut, not that nothing happened."""
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    prefix = transcript[:prefix_len]
    marker = "\n[...older turns truncated to stay within the context-size safeguard...]\n"
    tail_budget = MAX_TRANSCRIPT_CHARS - prefix_len - len(marker)
    tail = transcript[-tail_budget:] if tail_budget > 0 else ""
    return prefix + marker + tail



def _ollama_contract_adapter(
    request: backend_contract.BackendRequest,
) -> backend_contract.BackendResponse:
    """Adapt the proven Ollama call path to the normalized backend contract."""
    raw = _call_ollama(
        request.prompt,
        model=request.model,
        timeout_s=request.timeout_s,
    )

    return backend_contract.BackendResponse(
        text=raw,
        provider=backend_contract.OLLAMA_PROVIDER_ID,
        model=request.model,
        metadata={
            "adapter": "omniengineer_agent",
        },
    )


def _provider_b_contract_adapter(
    request: backend_contract.BackendRequest,
) -> backend_contract.BackendResponse:
    """Adapt the proven Provider B call path to the same normalized contract."""
    raw = _call_provider_b(
        request.prompt,
        model=request.model,
        timeout_s=request.timeout_s,
    )

    return backend_contract.BackendResponse(
        text=raw,
        provider=backend_contract.PROVIDER_B_ID,
        model=request.model,
        metadata={
            "adapter": "omniengineer_agent",
        },
    )


def _ensure_builtin_backend_contracts() -> None:
    """Register built-in backends without overwriting an existing adapter."""

    builtins = (
        (
            backend_contract.OLLAMA_PROVIDER_ID,
            _ollama_contract_adapter,
        ),
        (
            backend_contract.PROVIDER_B_ID,
            _provider_b_contract_adapter,
        ),
    )

    for provider_id, invoke_fn in builtins:
        try:
            backend_contract.get_backend(provider_id)
        except backend_contract.BackendNotRegisteredError:
            backend_contract.register_backend(
                backend_contract.BackendAdapter(
                    provider_id=provider_id,
                    invoke_fn=invoke_fn,
                    capabilities=backend_contract.BackendCapabilities(
                        text_generation=True,
                        structured_tool_protocol=True,
                        health_check=True,
                        local_runtime=True,
                    ),
                )
            )


def _call_model_backend(
    prompt: str,
    *,
    model: str,
    provider: str,
    timeout_s: int,
) -> str:
    """Invoke any registered model backend through one stable Omni seam."""

    _ensure_builtin_backend_contracts()

    response = backend_contract.invoke_backend(
        provider,
        prompt=prompt,
        model=model,
        timeout_s=timeout_s,
        metadata={
            "caller": "omniengineer_agent",
        },
    )

    return response.text



# DYNAMIC_TOOL_CONTRACT_V1
def _scrub_unavailable_tool_names_v1(
    text: str,
    allowed_tools,
) -> str:
    """Remove unavailable execution-tool names from model-visible context.

    Runtime authorization remains enforced independently by allowed_tools.
    finish/escalate are terminal actions and remain model-visible.
    """
    if allowed_tools is None:
        return text

    terminal_actions = {"finish", "escalate"}
    granted = set(allowed_tools) | terminal_actions

    unavailable = (
        set(TOOL_NAMES) - granted
    )

    rendered = text

    # Longest-first avoids accidental partial replacement if tool names
    # ever share a prefix in a future tool set.
    for name in sorted(
        unavailable,
        key=len,
        reverse=True,
    ):
        rendered = rendered.replace(
            name,
            "[unavailable tool omitted]",
        )

    return rendered


def _render_dynamic_tool_contract_v1(
    base_prompt: str,
    allowed_tools,
) -> str:
    """Render the effective job-specific tool catalog for the model."""
    rendered = _scrub_unavailable_tool_names_v1(
        base_prompt,
        allowed_tools,
    )

    if allowed_tools is None:
        return rendered

    terminal_actions = {"finish", "escalate"}
    granted = sorted(
        set(allowed_tools) | terminal_actions
    )

    rendered += (
        "\nSYSTEM: DYNAMIC TOOL CONTRACT V1. "
        "The complete and exclusive tool catalog available for this job is: "
        + ", ".join(granted)
        + ". Tool names not listed here are unavailable. "
        "Do not invent or call tools outside this catalog.\n"
    )

    return rendered

def run_agent_loop(
    task: str,
    sandbox: Path,
    *,
    model: str = DEFAULT_MODEL,
    provider: str = "ollama",
    max_iterations: int = MAX_ITERATIONS,
    plan_text: str | None = None,
    timeout_s: int | None = None,
    on_checkpoint: Callable[[dict], None] | None = None,
    allowed_tools: frozenset[str] | None = None,
) -> AgentRunResult:
    """`on_checkpoint`, if given, is called after every completed iteration
    (tool executed, or a terminal finish/escalate) with a plain dict
    ({iteration, tool, model_calls, files_touched, commands_executed}) — the
    harness uses this to write a durable per-iteration ledger checkpoint
    (see job_ledger.py). Never raises into the loop: a checkpoint failure is
    swallowed (logged into the turn record as a note) rather than aborting
    an otherwise-healthy agent run over a bookkeeping problem.

    `allowed_tools` (Founder-authorized 2026-08-20, OMNI_ENGINEER_REAL_
    SOURCE_REPAIR_PARITY): None (the default) means every tool in TOOL_NAMES
    is available, exactly as before this parameter existed — zero behavior
    change for any existing caller. When given, any tool call NOT in the set
    (finish/escalate are always implicitly allowed regardless) is refused
    with a clear tool-error result fed back to the model, WITHOUT ever
    calling `_execute_tool()` for it — a structural guarantee (like Claude
    Code's own `--allowedTools`), not merely a prompt instruction the model
    could ignore. This is what gives investigate()-style read-only Omni
    Engineer runs the same real read-first guarantee bridge.py's Read/Grep/
    Glob-only tool grant already gives Claude Code."""
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    deadline = (t0 + timeout_s) if timeout_s else None
    before_snapshot = _snapshot(sandbox)
    commands_executed: list[str] = []
    turns: list[TurnRecord] = []
    model_calls = 0

    # EDIT_FAILURE_REINSPECTION_V1
    # A failed mutation is authoritative evidence that the model's current
    # edit assumptions are wrong. Require a fresh read/grep before allowing
    # another mutation so local models cannot burn the bounded turn budget
    # repeating stale write/patch attempts.
    require_edit_reinspection = False

    # INSPECTION_STAGNATION_V1
    # Bound inspection-only behavior without increasing max_iterations
    # or supplying any task-specific repair.
    inspection_streak = 0
    INSPECTION_STAGNATION_LIMIT = 6

    # EXECUTION_COMPLETION_GATE_V1
    # These markers are supplied only by jobs whose orchestration requires
    # concrete execution evidence before finish may succeed.
    execution_require_mutation_before_finish = (
        "EXECUTION_CONTRACT_REQUIRE_MUTATION_BEFORE_FINISH=YES"
        in task
    )

    execution_required_tools_before_finish = set()

    if (
        "EXECUTION_CONTRACT_REQUIRE_RUN_COMMAND_BEFORE_FINISH=YES"
        in task
    ):
        execution_required_tools_before_finish.add(
            "run_command"
        )

    if (
        "EXECUTION_CONTRACT_REQUIRE_RUN_VALIDATOR_BEFORE_FINISH=YES"
        in task
    ):
        execution_required_tools_before_finish.add(
            "run_validator"
        )

    if (
        "EXECUTION_CONTRACT_REQUIRE_INSPECT_DIFF_BEFORE_FINISH=YES"
        in task
    ):
        execution_required_tools_before_finish.add(
            "inspect_diff"
        )

    successful_execution_tools = set()

    # EXECUTION_COMPLETION_GATE_V1_RUNTIME_FIX
    # This state must exist before any terminal finish decision.
    execution_mutation_tools = {
        "write_file_sandbox",
        "apply_patch_sandbox",
    }

    transcript = _render_dynamic_tool_contract_v1(SYSTEM_PROMPT.format(max_iterations=max_iterations), allowed_tools)

    # TOOL_SCOPE_ADHERENCE_V1
    # Make runtime tool authority explicit to the model. The allowed_tools
    # gate remains authoritative regardless of model behavior.
    if allowed_tools is not None:
        transcript += (
            "SYSTEM: TOOL AUTHORITY FOR THIS JOB: only the following "
            "execution tools are granted: "
            + ", ".join(sorted(allowed_tools))
            + ". Any other execution tool is unavailable and will be "
            "refused. Do not call unavailable tools. Terminal actions "
            "finish and escalate remain available when appropriate.\n"
        )
    if allowed_tools is not None:
        denied = sorted(TOOL_NAMES - {"finish", "escalate"} - allowed_tools)
        if denied:
            transcript += (
                f"\nIMPORTANT: for this run you do NOT have access to: {', '.join(denied)}. "
                f"Calling any denied tool will be refused. Use only the tools that remain granted for this run. "
                f"When your permitted work is complete, call finish.\n"
            )
    transcript += f"\n\nTASK:\n{task}\n"
    if plan_text:
        transcript += f"\nPLAN:\n{plan_text}\n"
    transcript += "\nBegin. Respond with your first tool call now.\n"
    prefix_len = len(transcript)

    final_action = "iteration_ceiling_reached"
    summary_or_reason = f"used all {max_iterations} iterations without calling finish or escalate"

    def _checkpoint(iteration: int, tool: str) -> None:
        if on_checkpoint is None:
            return
        try:
            on_checkpoint({
                "iteration": iteration, "tool": tool, "model_calls": model_calls,
                "files_touched": _touched_since(sandbox, before_snapshot),
                "commands_executed": list(commands_executed),
            })
        except Exception:  # noqa: BLE001 — checkpointing must never break the agent run
            pass

    for i in range(1, max_iterations + 1):
        # DYNAMIC_TOOL_CONTRACT_V1 per-turn enforcement
        transcript = _scrub_unavailable_tool_names_v1(
            transcript,
            allowed_tools,
        )
        if deadline and time.monotonic() > deadline:
            final_action = "timeout"
            summary_or_reason = f"exceeded {timeout_s}s wall-clock budget before iteration {i}"
            break
        if model_calls >= MAX_MODEL_CALLS:
            final_action = "escalate"
            summary_or_reason = f"exceeded MAX_MODEL_CALLS={MAX_MODEL_CALLS} (malformed-output retries counted) before iteration {i}"
            turns.append(TurnRecord(i, "escalate", {}, "", summary_or_reason, False))
            break

        call: ToolCall | None = None
        raw = ""
        for attempt in range(1, MAX_MALFORMED_RETRIES + 1):
            if model_calls >= MAX_MODEL_CALLS:
                break
            remaining = (deadline - time.monotonic()) if deadline else MODEL_CALL_TIMEOUT_S
            call_timeout = max(5, min(MODEL_CALL_TIMEOUT_S, int(remaining)))
            transcript = _truncate_transcript(transcript, prefix_len)
            try:
                raw = _call_model_backend(
                    transcript,
                    model=model,
                    provider=provider,
                    timeout_s=call_timeout,
                )
                model_calls += 1
            except urllib.error.URLError as e:
                final_action, summary_or_reason = "model_unavailable", f"{provider} request failed: {e}"
                return _finish(task, sandbox, model, provider, final_action, summary_or_reason, turns, commands_executed, model_calls, started_at, t0)
            except Exception as e:  # noqa: BLE001
                final_action, summary_or_reason = "error", f"unexpected error calling model via {provider}: {e!r}"
                return _finish(task, sandbox, model, provider, final_action, summary_or_reason, turns, commands_executed, model_calls, started_at, t0)
            call = _parse_tool_call(raw)
            if call is not None:
                break
            transcript += (
                f"\nASSISTANT (turn {i}, attempt {attempt}): {raw[:500]}\n"
                "SYSTEM: invalid tool call. Respond with EXACTLY ONE JSON object and nothing else. "
                "Required keys: thought, tool, args. args MUST be a JSON object. "
                f"Valid tools: {', '.join(sorted(TOOL_NAMES))}.\n"
            )
        if call is None:
            final_action = "error"
            summary_or_reason = (
                f"exceeded MAX_MODEL_CALLS={MAX_MODEL_CALLS} while retrying iteration {i}"
                if model_calls >= MAX_MODEL_CALLS else
                f"model failed to produce a valid tool call after {MAX_MALFORMED_RETRIES} attempts on iteration {i}"
            )
            turns.append(TurnRecord(i, "error", {}, "", summary_or_reason, False))
            break

        transcript += f"\nASSISTANT (turn {i}): {json.dumps(asdict(call))}\n"

        # EXECUTION_COMPLETION_GATE_V1
        finish_readiness_satisfied = (
            (
                not execution_require_mutation_before_finish
                or bool(
                    successful_execution_tools
                    & execution_mutation_tools
                )
            )
            and execution_required_tools_before_finish.issubset(
                successful_execution_tools
            )
        )
        
        if call.tool == "finish" and finish_readiness_satisfied:
            final_action = "finish"
            summary_or_reason = str(call.args.get("summary", ""))
            turns.append(TurnRecord(i, "finish", call.args, call.thought, summary_or_reason, True))
            _checkpoint(i, "finish")
            break
        if call.tool == "escalate":
            final_action = "escalate"
            summary_or_reason = str(call.args.get("reason", ""))
            turns.append(TurnRecord(i, "escalate", call.args, call.thought, summary_or_reason, True))
            _checkpoint(i, "escalate")
            break

        mutation_tools = {"write_file_sandbox", "apply_patch_sandbox"}
        inspection_tools = {"read_file", "grep"}

        if require_edit_reinspection and call.tool in mutation_tools:
            ok, result_text = False, (
                "refused: the previous mutation failed. Re-inspect the affected "
                "source with read_file or grep before attempting another mutation. "
                "Do not repeat the previous patch blindly."
            )
        elif allowed_tools is not None and call.tool not in allowed_tools:
            ok, result_text = False, f"refused: tool {call.tool!r} is not granted for this run (read-only investigation)"
        else:
            ok, result_text = _execute_tool(call, sandbox, before_snapshot, commands_executed)

        if call.tool in mutation_tools and not ok:
            require_edit_reinspection = True
        elif call.tool in inspection_tools and ok:
            require_edit_reinspection = False


        # INSPECTION_STAGNATION_V1
        # A mutation/test/action attempt resets the inspection streak.
        # After six consecutive successful inspection-only turns, the
        # model stops receiving additional inspection context and is told
        # to act using granted engineering tools or escalate truthfully.
        if call.tool in inspection_tools and ok:
            inspection_streak += 1
            if inspection_streak > INSPECTION_STAGNATION_LIMIT:
                ok = False
                result_text = (
                    "refused: inspection-only work reached the bounded "
                    "stagnation limit. Use the evidence already gathered "
                    "to attempt the next appropriate engineering action "
                    "with a granted tool, or escalate truthfully if you "
                    "cannot proceed. Do not spend the remaining iterations "
                    "repeating inspection-only work."
                )
        elif call.tool not in inspection_tools and ok:
            # TOOL_SCOPE_ADHERENCE_V1
            # Only a successful non-inspection engineering action earns a
            # stagnation reset. Refused/failed/unavailable tool attempts must
            # not be usable as a way to escape the bounded inspection guard.
            inspection_streak = 0


        # EXECUTION_COMPLETION_GATE_V1
        # Only genuinely successful tools count toward finish readiness.
        if ok:
            successful_execution_tools.add(
                call.tool
            )

        turns.append(TurnRecord(i, call.tool, call.args, call.thought, result_text[:1000], ok))
        transcript += f"TOOL RESULT ({'ok' if ok else 'error'}): {result_text[:2000]}\n"

        if call.tool in mutation_tools and not ok:
            transcript += (
                "SYSTEM: EDIT RECOVERY REQUIRED. The mutation did NOT occur. "
                "Treat the TOOL RESULT error as authoritative. Re-read or grep the "
                "target, obtain current exact unique context, then formulate a new "
                "surgical patch. Seeded files must never be replaced wholesale.\n"
            )

        _checkpoint(i, call.tool)

    return _finish(task, sandbox, model, provider, final_action, summary_or_reason, turns, commands_executed, model_calls, started_at, t0)


def _finish(task, sandbox, model, provider, final_action, summary_or_reason, turns, commands_executed, model_calls, started_at, t0) -> AgentRunResult:
    ended_at = datetime.now(timezone.utc).isoformat()
    return AgentRunResult(
        task=task, sandbox=str(sandbox), model=model, provider=provider,
        final_action=final_action, summary_or_reason=summary_or_reason,
        turns=[asdict(t) for t in turns], commands_executed=commands_executed,
        model_calls=model_calls,
        started_at=started_at, ended_at=ended_at, duration_s=time.monotonic() - t0,
    )
