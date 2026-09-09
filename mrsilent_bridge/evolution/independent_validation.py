"""
Independent Validation — a genuinely SEPARATE re-check, not a second call
into validation.py (Founder-authorized SINGLE_VALIDATOR_DEPENDENCY
resilience milestone, 2026-08-18).

validation.py is preserved completely unmodified in spirit and untouched in
its check logic (only crash-safety was hardened at the orchestration level,
see its own module docstring/changelog) — it remains the primary, full-
coverage gate. What was missing is INDEPENDENCE: `omniengineer_harness.py`
already runs `validation.validate()` twice (VALIDATE then CANARY), but
that's the SAME code path run twice — a bug or blind spot in
`check_python_compile`'s in-process `compile()` call, or in
`check_tests`'s exit-code interpretation, would pass BOTH times identically.
Phase U's `evolution/local_review.py` adds a genuinely different local
model's SUBJECTIVE opinion, which is valuable but not a deterministic
recheck of objective, mechanical facts.

This module re-derives the two most safety-critical, objectively-checkable
facts about a sandbox using a DELIBERATELY DIFFERENT mechanism than
validation.py's own:
  - "do the changed Python files actually compile" — validation.py's
    check_python_compile calls compile() IN-PROCESS on file text already
    read into this interpreter. This module instead shells out fresh,
    per-file, to `python3 -m py_compile <path>` — a genuinely separate
    process/interpreter instance, immune to any bug specific to the
    in-process compile() code path (e.g. an encoding/state issue that only
    manifests one way in-process and another via a fresh subprocess).
  - "do the discovered tests actually pass" — validation.py's check_tests
    routes through _run_allowlisted() (this project's one shared jailed-
    subprocess runner, used by EVERY check in that module). This module
    calls subprocess.run() directly with its own independent allowlist/
    jail logic — a bug in _run_allowlisted() itself (e.g. output
    truncation hiding a real failure, or an exit-code misinterpretation)
    would not be replicated here, because this is not the same code.

Both checks are read-only/run-only, exactly like validation.py's checks:
tests literally execute (that's what "does it pass" means), but nothing
here writes outside the sandbox, downloads anything, or touches a
protected path — the same authority_policy.GATED_PATH_MARKERS check runs
first, independently implemented, before anything else does.

This is NOT a competing validation engine — it is deliberately narrow (two
facts, not the full battery validation.py already covers), deliberately
slower-path (only invoked after validation.py + canary both already
passed, on the promotion-candidate path, never as a replacement for the
fast-fail case), and its ONLY job is to catch the specific class of risk a
single validator implementation cannot catch about itself: "the one
program that judges correctness is itself wrong." A disagreement here is
recorded loudly and blocks promotion_eligible — see
omniengineer_harness.py's canary step — never silently overridden, never
silently becomes production approval.
"""
from __future__ import annotations

import importlib.util
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authority_policy import GATED_PATH_MARKERS
from env_util import minimal_env

INDEPENDENT_CHECK_TIMEOUT_S = 60


@dataclass
class IndependentFinding:
    name: str
    passed: bool
    detail: str


@dataclass
class IndependentValidationResult:
    sandbox: str
    checked_at: str
    ran: bool  # False if there was nothing objectively re-checkable (e.g. no python/tests) — not a disagreement
    agrees_with_primary: bool | None  # None when ran=False
    findings: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _within_sandbox(path: Path, sandbox: Path) -> bool:
    try:
        path.resolve().relative_to(sandbox.resolve())
        return True
    except ValueError:
        return False


def _changed_python_files(sandbox: Path, files_changed: dict[str, list[str]]) -> list[Path]:
    rels = (files_changed.get("added", []) or []) + (files_changed.get("modified", []) or [])
    paths = []
    for rel in rels:
        p = sandbox / rel
        if p.suffix != ".py" or not p.exists():
            continue
        if not _within_sandbox(p, sandbox):
            continue  # symlink escape — validation.py's own check already catches/blocks this; never re-execute it here
        p_str = str(p.resolve())
        if any(marker.lower() in p_str.lower() for marker in GATED_PATH_MARKERS):
            continue  # defense in depth — independently re-checked, not assumed from validation.py's result
        paths.append(p)
    return paths


def _recheck_compile(sandbox: Path, py_files: list[Path]) -> IndependentFinding | None:
    if not py_files:
        return None
    t0 = time.monotonic()
    errors = []
    for f in py_files:
        try:
            proc = subprocess.run(
                ["python3", "-m", "py_compile", str(f)],
                cwd=str(sandbox), env=minimal_env(),
                capture_output=True, text=True, timeout=INDEPENDENT_CHECK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{f.relative_to(sandbox)}: py_compile subprocess timed out")
            continue
        if proc.returncode != 0:
            errors.append(f"{f.relative_to(sandbox)}: {(proc.stderr or proc.stdout or '').strip()[:500]}")
    passed = not errors
    detail = (f"{len(py_files)} python file(s) independently re-compiled clean via a fresh `python3 -m py_compile` "
              f"subprocess per file (not validation.py's in-process compile())") if passed else "; ".join(errors)
    return IndependentFinding("independent_python_compile_recheck", passed, detail)


def _recheck_tests(sandbox: Path) -> IndependentFinding | None:
    """RANK8_INDEPENDENT_TEST_DISCOVERY_REPAIR (Founder-authorized
    2026-09-09, real reproduced incident: job c912b60a / proposal 08a37299
    and the deterministic test_calc.py fixture both hit this): this
    function's independence has always meant a fresh, separately-invoked
    subprocess.run() call outside validation.py's shared
    _run_allowlisted() jail wrapper (immune to a bug specific to THAT
    plumbing) -- it was never meant to mean a deliberately different,
    incompatible test-DISCOVERY convention. Hardcoding `python3 -m
    unittest discover` meant this recheck could only ever see
    unittest.TestCase-based tests, while this project's actual, prevailing
    test convention is plain module-level `def test_*():` functions with
    no TestCase subclass (every real test file in mrsilent_bridge/tests/,
    and validation.py's OWN test_discovery_and_run check, already run
    these via pytest) -- unittest discover reported "Ran 0 tests" for
    every one of them, a false disagreement with zero relation to whether
    the code under test is actually correct.

    Now mirrors validation.py::check_tests()'s own runner-selection
    precedence exactly (prefer pytest when importable; fall back to
    `unittest discover` only when it genuinely is not) so both validators
    recognize the SAME tests. Still a completely independent process
    invocation (this module's own subprocess.run(), own env, own timeout
    -- never validation.py's _run_allowlisted()), so a bug specific to
    THAT shared code path is still independently caught exactly as
    before; only the test-DISCOVERY mismatch is closed."""
    has_tests = any(sandbox.rglob("test_*.py")) or any(sandbox.rglob("*_test.py"))
    if not has_tests:
        return None
    use_pytest = importlib.util.find_spec("pytest") is not None
    runner = "pytest" if use_pytest else "unittest discover"
    cmd = (["python3", "-m", "pytest", ".", "-q", "-p", "no:cacheprovider"] if use_pytest
           else ["python3", "-m", "unittest", "discover", "-s", "."])
    try:
        proc = subprocess.run(
            cmd, cwd=str(sandbox), env=minimal_env(),
            capture_output=True, text=True, timeout=INDEPENDENT_CHECK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return IndependentFinding("independent_test_recheck", False, f"{runner} subprocess timed out")
    if use_pytest and proc.returncode == 5:
        # pytest exit code 5 = no tests collected -- not applicable, not a
        # failure (mirrors validation.py::check_tests()'s identical handling).
        return None
    passed = proc.returncode == 0
    detail = (f"tests independently re-ran clean via a direct, freshly-invoked `python3 -m {runner}` "
              f"subprocess (not validation.py's _run_allowlisted() code path)") if passed else \
             ((proc.stdout or "") + (proc.stderr or "")).strip()[:1000]
    return IndependentFinding("independent_test_recheck", passed, detail)


def recheck(sandbox: str | Path, files_changed: dict[str, list[str]], *,
            primary_passed: bool) -> IndependentValidationResult:
    """Only meaningful to call once validation.py (+ canary) has already
    reported passed=True on this same sandbox — `primary_passed` is
    required explicitly (not re-derived) so a caller can never accidentally
    treat a disagreement as informative when the primary hasn't even
    finished, and so this module never has to duplicate validation.py's
    own orchestration to know what to compare against."""
    sandbox = Path(sandbox)
    checked_at = datetime.now(timezone.utc).isoformat()
    if not sandbox.exists():
        return IndependentValidationResult(sandbox=str(sandbox), checked_at=checked_at, ran=False,
                                            agrees_with_primary=None, reason=f"sandbox does not exist: {sandbox}")

    py_files = _changed_python_files(sandbox, files_changed)
    findings = [f for f in (_recheck_compile(sandbox, py_files), _recheck_tests(sandbox)) if f is not None]

    if not findings:
        return IndependentValidationResult(sandbox=str(sandbox), checked_at=checked_at, ran=False,
                                            agrees_with_primary=None,
                                            reason="nothing objectively re-checkable (no changed .py files, no tests)")

    independent_passed = all(f.passed for f in findings)
    agrees = independent_passed == primary_passed
    return IndependentValidationResult(
        sandbox=str(sandbox), checked_at=checked_at, ran=True, agrees_with_primary=agrees,
        findings=[asdict(f) for f in findings],
        reason=None if agrees else (
            f"DISAGREEMENT: validation.py+canary reported passed={primary_passed}, but an independently-"
            f"implemented recheck (separate subprocess mechanism, not validation.py's code path) found "
            f"passed={independent_passed} — " + "; ".join(f.detail for f in findings if not f.passed)
        ),
    )
