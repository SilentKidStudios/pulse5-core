"""
Automatic Sandbox Validation Gate — a reusable Studio component.

    TASK -> ENGINEERING AGENT -> SANDBOX CHANGE -> AUTOMATIC VALIDATION -> PASS/FAIL
    -> promotion eligibility only on PASS

This module knows nothing about Claude, Codex, or any specific agent. It takes
a sandbox directory and a changed-file report and runs a battery of checks
against it. `bridge.py` calls this after any engineering-agent adapter
produces a sandbox change; `promotion.py` refuses to write outside a sandbox
unless the job's recorded validation result has `passed: true`. Nothing in
this module ever writes outside the sandbox it was given, and nothing in it
can promote anything on its own — validation only measures and reports.

Safety posture (this file IS the "validation commands must obey authority
policy" requirement):
  - every subprocess a validator runs is jailed to `cwd=sandbox`
  - argv[0] must be in ALLOWED_BINARIES — no curl/wget/pip/git/rm/etc. can
    ever run as a "validator", which is what keeps this free of network
    calls (no paid services), model downloads, and destructive operations
  - the full command string is still checked against
    authority_policy.GATED_KEYWORDS as defense in depth
  - changed-file inspection resolves every changed path and fails the run if
    anything resolves outside the sandbox (symlink escape) or matches
    authority_policy.GATED_PATH_MARKERS (protected core / Scorpio / models /
    credentials) — this is what "no protected-core modifications" and
    "no Scorpio changes" mean concretely here
  - nothing here modifies an existing file; validators are read/run-only, so
    "backups before modifying existing components" has nothing to apply to
    inside this module (promotion.py, the one place that does modify existing
    files, already backs them up — see promotion.py's docstring)
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authority_policy import GATED_KEYWORDS, GATED_PATH_MARKERS
from env_util import minimal_env

ALLOWED_BINARIES = frozenset({"python3", "bash"})
DEFAULT_CHECK_TIMEOUT_S = 60
MAX_CHECK_TIMEOUT_S = 180
OUTPUT_EXCERPT_CHARS = 4000


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    duration_s: float
    exit_code: int | None = None
    validator_error: bool = False  # SINGLE_VALIDATOR_DEPENDENCY: True means the CHECK ITSELF crashed
    # (a bug/exception inside validation.py), distinct from passed=False (the
    # code under test genuinely failed a real check). Both are real, honest
    # failures for promotion-eligibility purposes, but Founder-facing
    # reporting and experience learning need to tell them apart — a
    # validator_error is a MR. SILENT infrastructure defect worth fixing at
    # the source, not evidence the engineering work itself was bad.


@dataclass
class ValidationResult:
    sandbox: str
    started_at: str
    ended_at: str
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _within_sandbox(path: Path, sandbox: Path) -> bool:
    try:
        path.resolve().relative_to(sandbox.resolve())
        return True
    except ValueError:
        return False


def _run_allowlisted(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int | None, str]:
    """Jailed, allowlisted subprocess runner shared by every validator that
    needs to execute something. Returns (exit_code_or_None, combined_output).
    A None exit code means the command was refused before it ran."""
    if not argv or argv[0] not in ALLOWED_BINARIES:
        return None, f"refused: '{argv[0] if argv else ''}' is not in ALLOWED_BINARIES {sorted(ALLOWED_BINARIES)}"
    cmd_str = " ".join(argv)
    for pattern in GATED_KEYWORDS:
        if pattern.search(cmd_str):
            return None, f"refused: command matched gated keyword pattern {pattern.pattern!r}"
    timeout = min(timeout, MAX_CHECK_TIMEOUT_S)
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), env=minimal_env(),
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[:OUTPUT_EXCERPT_CHARS]
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return None, f"binary not found: {e}"


def _changed_paths(sandbox: Path, files_changed: dict[str, list[str]]) -> list[Path]:
    rels = (files_changed.get("added", []) or []) + (files_changed.get("modified", []) or [])
    return [sandbox / rel for rel in rels]


# ---- individual checks -----------------------------------------------------

def check_changed_file_inspection(sandbox: Path, files_changed: dict[str, list[str]], _config: dict) -> CheckResult:
    t0 = time.monotonic()
    problems = []
    for p in _changed_paths(sandbox, files_changed):
        if not _within_sandbox(p, sandbox):
            problems.append(f"{p} resolves outside sandbox (symlink escape?)")
            continue
        p_str = str(p.resolve())
        for marker in GATED_PATH_MARKERS:
            if marker.lower() in p_str.lower():
                problems.append(f"{p} matches protected marker '{marker}'")
    passed = not problems
    detail = "all changed files stay inside sandbox and touch no protected markers" if passed else "; ".join(problems)
    return CheckResult("changed_file_inspection", passed, detail, time.monotonic() - t0)


def check_expected_files(sandbox: Path, _files_changed: dict, config: dict) -> CheckResult | None:
    expected = config.get("expected_files")
    if not expected:
        return None
    t0 = time.monotonic()
    missing = [rel for rel in expected if not (sandbox / rel).exists()]
    passed = not missing
    detail = "all expected files present" if passed else f"missing: {missing}"
    return CheckResult("expected_file_checks", passed, detail, time.monotonic() - t0)


def check_python_compile(sandbox: Path, files_changed: dict, _config: dict) -> CheckResult | None:
    py_files = [p for p in _changed_paths(sandbox, files_changed) if p.suffix == ".py" and p.exists()]
    if not py_files:
        return None
    t0 = time.monotonic()
    errors = []
    for f in py_files:
        try:
            source = f.read_text()
            compile(source, str(f.relative_to(sandbox)), "exec")
        except SyntaxError as e:
            errors.append(f"{f.relative_to(sandbox)}: {e}")
        except UnicodeDecodeError as e:
            errors.append(f"{f.relative_to(sandbox)}: {e}")
    passed = not errors
    detail = f"{len(py_files)} python file(s) compiled clean" if passed else "; ".join(errors)
    return CheckResult("python_compile_check", passed, detail, time.monotonic() - t0)


def check_json_syntax(sandbox: Path, files_changed: dict, _config: dict) -> CheckResult | None:
    json_files = [p for p in _changed_paths(sandbox, files_changed) if p.suffix == ".json" and p.exists()]
    if not json_files:
        return None
    t0 = time.monotonic()
    errors = []
    for f in json_files:
        try:
            json.loads(f.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            errors.append(f"{f.relative_to(sandbox)}: {e}")
    passed = not errors
    detail = f"{len(json_files)} json file(s) parsed clean" if passed else "; ".join(errors)
    return CheckResult("json_syntax_check", passed, detail, time.monotonic() - t0)


def check_json_schema(sandbox: Path, files_changed: dict, config: dict) -> CheckResult | None:
    schemas = config.get("schemas")  # {relative_path: schema_relative_path_or_dict}
    if not schemas:
        return None
    try:
        import jsonschema
    except ImportError:
        return CheckResult("json_schema_validation", True, "jsonschema not installed, skipped", 0.0)
    t0 = time.monotonic()
    errors = []
    checked = 0
    changed_rel = {str(p.relative_to(sandbox)) for p in _changed_paths(sandbox, files_changed)}
    for rel, schema_ref in schemas.items():
        if rel not in changed_rel:
            continue
        target = sandbox / rel
        if not target.exists():
            continue
        try:
            instance = json.loads(target.read_text())
            schema = schema_ref if isinstance(schema_ref, dict) else json.loads(Path(schema_ref).read_text())
            jsonschema.validate(instance=instance, schema=schema)
            checked += 1
        except jsonschema.ValidationError as e:
            errors.append(f"{rel}: {e.message}")
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"{rel}: {e}")
    if checked == 0 and not errors:
        return None
    passed = not errors
    detail = f"{checked} file(s) validated against schema" if passed else "; ".join(errors)
    return CheckResult("json_schema_validation", passed, detail, time.monotonic() - t0)


def check_other_syntax(sandbox: Path, files_changed: dict, _config: dict) -> CheckResult | None:
    """YAML syntax (if pyyaml available) and shell syntax (bash -n, syntax-only, never executes)."""
    paths = _changed_paths(sandbox, files_changed)
    yaml_files = [p for p in paths if p.suffix in (".yaml", ".yml") and p.exists()]
    sh_files = [p for p in paths if p.suffix == ".sh" and p.exists()]
    if not yaml_files and not sh_files:
        return None
    t0 = time.monotonic()
    errors = []
    checked = 0
    if yaml_files:
        try:
            import yaml
            for f in yaml_files:
                try:
                    yaml.safe_load(f.read_text())
                    checked += 1
                except yaml.YAMLError as e:
                    errors.append(f"{f.relative_to(sandbox)}: {e}")
        except ImportError:
            pass  # pyyaml not installed: skip yaml files silently, not a failure
    for f in sh_files:
        exit_code, out = _run_allowlisted(["bash", "-n", str(f)], cwd=sandbox, timeout=15)
        checked += 1
        if exit_code != 0:
            errors.append(f"{f.relative_to(sandbox)}: {out.strip()}")
    if checked == 0:
        return None
    passed = not errors
    detail = f"{checked} file(s) syntax-checked clean" if passed else "; ".join(errors)
    return CheckResult("other_syntax_checks", passed, detail, time.monotonic() - t0)


def check_static_validation(sandbox: Path, files_changed: dict, _config: dict) -> CheckResult | None:
    """ruff, if installed. This is 'where applicable' — silently absent tooling
    is not a failure, it's an unmet precondition."""
    py_files = [p for p in _changed_paths(sandbox, files_changed) if p.suffix == ".py" and p.exists()]
    if not py_files or not shutil.which("ruff"):
        return None
    t0 = time.monotonic()
    # ruff isn't in ALLOWED_BINARIES (only python3/bash are) — run it via
    # `python3 -m ruff` so it still passes through the same allowlisted runner.
    exit_code, out = _run_allowlisted(
        ["python3", "-m", "ruff", "check", *[str(p.relative_to(sandbox)) for p in py_files]],
        cwd=sandbox, timeout=DEFAULT_CHECK_TIMEOUT_S,
    )
    if exit_code is None:
        return CheckResult("static_validation", True, f"ruff unavailable via python3 -m, skipped ({out})", time.monotonic() - t0)
    passed = exit_code == 0
    return CheckResult("static_validation", passed, out.strip() or "clean", time.monotonic() - t0, exit_code)


def _clear_stale_test_caches(sandbox: Path) -> None:
    """GOD_MODE_V1 FINAL GAP CLOSURE: a decomposed job's repair cycle can
    overwrite a test file and re-run check_tests() against the SAME
    sandbox within the same filesystem mtime granularity. pytest's
    assertion-rewrite cache and __pycache__ are keyed by (path, mtime,
    size) -- a same-second rewrite can collide, causing a STALE compiled
    assertion to run against the CURRENT source text (a real, if narrow,
    false-pass/false-fail risk in the validation truth gate itself, not
    merely a test-harness quirk). Clearing these cache-only artifacts
    (never user source) before every real test run closes it. Best-effort:
    a cleanup failure must never block validation itself."""
    import shutil as _shutil
    for cache_dir in list(sandbox.rglob("__pycache__")) + list(sandbox.rglob(".pytest_cache")):
        try:
            _shutil.rmtree(cache_dir, ignore_errors=True)
        except OSError:
            pass


def check_tests(sandbox: Path, _files_changed: dict, config: dict) -> CheckResult | None:
    if config.get("run_tests", True) is False:
        return None
    has_tests = any(sandbox.rglob("test_*.py")) or any(sandbox.rglob("*_test.py"))
    if not has_tests:
        return None
    _clear_stale_test_caches(sandbox)
    t0 = time.monotonic()
    if importlib.util.find_spec("pytest") is not None:
        exit_code, out = _run_allowlisted(["python3", "-m", "pytest", ".", "-q", "-p", "no:cacheprovider"], cwd=sandbox,
                                            timeout=config.get("test_timeout_s", DEFAULT_CHECK_TIMEOUT_S))
        runner = "pytest"
        # pytest exit code 5 = no tests collected; treat as not-applicable, not a failure
        if exit_code == 5:
            return None
    else:
        exit_code, out = _run_allowlisted(["python3", "-m", "unittest", "discover", "-s", "."], cwd=sandbox,
                                            timeout=config.get("test_timeout_s", DEFAULT_CHECK_TIMEOUT_S))
        runner = "unittest"
    passed = exit_code == 0
    detail = f"[{runner}] " + (out.strip() or ("passed" if passed else "failed"))
    return CheckResult("test_discovery_and_run", passed, detail, time.monotonic() - t0, exit_code)


def check_custom_validators(sandbox: Path, _files_changed: dict, config: dict) -> list[CheckResult]:
    """Caller-supplied (never sandbox-supplied) project-specific validator
    commands. Each entry: {"name": str, "argv": [str, ...], "timeout_s": int?}.
    argv still passes through the same ALLOWED_BINARIES + GATED_KEYWORDS gate
    as everything else — a project can point python3 at its own check script,
    it cannot hand this function an arbitrary shell command."""
    validators = config.get("custom_validators") or []
    results = []
    for v in validators:
        t0 = time.monotonic()
        try:
            exit_code, out = _run_allowlisted(
                v["argv"], cwd=sandbox, timeout=v.get("timeout_s", DEFAULT_CHECK_TIMEOUT_S),
            )
            expected = v.get("expect_exit_code", 0)
            passed = exit_code == expected
            results.append(CheckResult(
                f"custom:{v.get('name', v['argv'][0])}", passed,
                out.strip() or f"exit_code={exit_code}", time.monotonic() - t0, exit_code,
            ))
        except Exception as e:  # noqa: BLE001 — one malformed custom validator entry must not lose the rest
            results.append(CheckResult(
                f"custom:{v.get('name', 'malformed_entry')}", False,
                f"validator_error: {e!r}", time.monotonic() - t0, validator_error=True,
            ))
    return results


# ---- orchestration ----------------------------------------------------------

def validate(sandbox: str | Path, files_changed: dict[str, list[str]], config: dict | None = None) -> ValidationResult:
    sandbox = Path(sandbox)
    config = config or {}
    started_at = datetime.now(timezone.utc).isoformat()

    if not sandbox.exists():
        return ValidationResult(
            sandbox=str(sandbox), started_at=started_at, ended_at=started_at,
            passed=False, error=f"sandbox does not exist: {sandbox}",
        )

    checks: list[CheckResult] = []
    skipped: list[str] = []

    single_checks = [
        check_changed_file_inspection,
        check_expected_files,
        check_python_compile,
        check_json_syntax,
        check_json_schema,
        check_other_syntax,
        check_static_validation,
        check_tests,
    ]
    for fn in single_checks:
        # A check function raising is a bug/crash IN THE VALIDATOR, not a
        # real finding about the sandbox's code — it must never take down
        # the whole validation pass (and therefore the whole job) with an
        # unhandled exception. Recorded as a distinct, honest
        # validator_error=True CheckResult(passed=False) instead: still
        # correctly blocks promotion (a check that couldn't run is not
        # evidence of correctness), but is now visible and diagnosable
        # rather than an opaque crash.
        try:
            result = fn(sandbox, files_changed, config)
        except Exception as e:  # noqa: BLE001 — a validator crash must degrade to a recorded failure, never propagate
            t_name = fn.__name__.removeprefix("check_")
            checks.append(CheckResult(t_name, False, f"validator_error: {e!r}", 0.0, validator_error=True))
            continue
        if result is None:
            skipped.append(fn.__name__.removeprefix("check_"))
        else:
            checks.append(result)

    try:
        checks.extend(check_custom_validators(sandbox, files_changed, config))
    except Exception as e:  # noqa: BLE001 — same crash-safety guarantee for the custom-validator batch
        checks.append(CheckResult("custom_validators", False, f"validator_error: {e!r}", 0.0, validator_error=True))

    ended_at = datetime.now(timezone.utc).isoformat()
    passed = all(c.passed for c in checks)  # vacuously true if nothing ran

    return ValidationResult(
        sandbox=str(sandbox), started_at=started_at, ended_at=ended_at,
        passed=passed, checks=[asdict(c) for c in checks], skipped=skipped,
    )


# ---------------------------------------------------------------------------
# OMNI_COMPLETION_CONTRACT_V1
#
# validation.py historically answered:
#   "Are the changes that exist safe/valid?"
#
# It did NOT answer:
#   "Did the engineering job actually make the changes it was required to
#    make?"
#
# An explicit caller-supplied completion_contract closes that gap without
# changing read-only/no-op semantics for callers that do not request one.
#
# Supported config:
#
#   {
#       "completion_contract": {
#           "require_changes": True,
#           "required_modified_files": ["existing.py"],
#           "required_added_files": ["test_regression.py"],
#       }
#   }
#
# The contract is supplied by trusted orchestration, never by the model.
# The model cannot self-declare completion.
# ---------------------------------------------------------------------------

_OMNI_COMPLETION_CONTRACT_BASE_VALIDATE_V1 = validate


def _completion_contract_paths_v1(value, *, key: str) -> tuple[list[str], list[str]]:
    problems: list[str] = []

    if value is None:
        return [], problems

    if not isinstance(value, list):
        return [], [f"{key} must be a list of relative path strings"]

    normalized: list[str] = []

    for item in value:
        if not isinstance(item, str) or not item:
            problems.append(f"{key} contains a non-string or empty path: {item!r}")
            continue

        rel = Path(item)

        if rel.is_absolute() or ".." in rel.parts:
            problems.append(f"{key} contains unsafe/non-relative path: {item!r}")
            continue

        normalized.append(str(rel))

    return normalized, problems


def validate(
    sandbox: Path,
    files_changed: dict[str, list[str]],
    config: dict | None = None,
) -> ValidationResult:
    """Run normal validation plus an optional explicit completion contract."""

    result = _OMNI_COMPLETION_CONTRACT_BASE_VALIDATE_V1(
        sandbox,
        files_changed,
        config=config,
    )

    cfg = config or {}
    contract = cfg.get("completion_contract")

    if contract is None:
        return result

    t0 = time.monotonic()
    problems: list[str] = []

    if not isinstance(contract, dict):
        problems.append("completion_contract must be an object")
        contract = {}

    require_changes = contract.get("require_changes", False)

    if not isinstance(require_changes, bool):
        problems.append("completion_contract.require_changes must be boolean")
        require_changes = False

    required_modified, p1 = _completion_contract_paths_v1(
        contract.get("required_modified_files"),
        key="required_modified_files",
    )

    required_added, p2 = _completion_contract_paths_v1(
        contract.get("required_added_files"),
        key="required_added_files",
    )

    problems.extend(p1)
    problems.extend(p2)

    added = set(files_changed.get("added", []) or [])
    modified = set(files_changed.get("modified", []) or [])
    removed = set(files_changed.get("removed", []) or [])

    has_changes = bool(added or modified or removed)

    if require_changes and not has_changes:
        problems.append(
            "completion contract requires a real sandbox change, but files_changed is empty"
        )

    missing_modified = [
        rel
        for rel in required_modified
        if rel not in modified
    ]

    missing_added = [
        rel
        for rel in required_added
        if rel not in added
    ]

    if missing_modified:
        problems.append(
            f"required files were not actually modified: {missing_modified}"
        )

    if missing_added:
        problems.append(
            f"required files were not actually added: {missing_added}"
        )

    for rel in required_modified:
        if rel in modified and not (sandbox / rel).is_file():
            problems.append(
                f"required modified file is not present in sandbox: {rel!r}"
            )

    for rel in required_added:
        if rel in added and not (sandbox / rel).is_file():
            problems.append(
                f"required added file is not present in sandbox: {rel!r}"
            )

    passed = not problems

    detail = (
        "explicit completion contract satisfied by the actual sandbox change report"
        if passed
        else "; ".join(problems)
    )

    result.checks.append(
        asdict(
            CheckResult(
                name="completion_contract",
                passed=passed,
                detail=detail,
                duration_s=time.monotonic() - t0,
            )
        )
    )

    if not passed:
        result.passed = False

    return result

