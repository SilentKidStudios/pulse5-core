#!/usr/bin/env python3
"""Tests for RANK8_INDEPENDENT_TEST_DISCOVERY_REPAIR in
evolution/independent_validation.py.

WHAT THIS CLOSES: read-only investigation (2026-09-09, campaign blocker
"Rank 8: independent-validation disagreement") root-caused a real,
reproduced disagreement -- _recheck_tests() hardcoded `python3 -m unittest
discover`, which only ever discovers unittest.TestCase-based tests. This
project's actual, prevailing test convention (every real file in this very
tests/ directory, and the deterministic test_calc.py fixture in
test_omniengineer.py) is plain module-level `def test_*():` functions with
no TestCase subclass -- invisible to unittest discover, which reported
"Ran 0 tests" for every one of them regardless of whether the code under
test was correct. That is a validator-authoring mismatch (stale/wrong
independent-validation semantics), not a real implementation defect: job
c912b60a / proposal 08a37299 had validation.py+canary both genuinely pass,
yet ended up "succeeded_validator_disagreement" purely from this mismatch.

WHAT THIS DELIBERATELY DOES NOT DO: it never changes validation.py itself,
never routes independent_validation.py through validation.py's shared
_run_allowlisted() code path (the actual "independence" property this
module's own docstring describes -- a fresh, separately-invoked
subprocess.run() call, immune to a bug specific to THAT shared plumbing),
and never weakens disagreement reporting -- a real disagreement (this
file's negative tests) must still be recorded and still block promotion.

Run: python3 tests/test_independent_validation_rank8_discovery.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution import independent_validation as iv  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _sandbox_with(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def _files_changed(*rels: str) -> dict:
    return {"added": list(rels), "modified": []}


# --------------------------------------------------------------------- #
# Positive: this project's real, plain pytest-style tests are now seen
# --------------------------------------------------------------------- #
def test_pytest_style_passing_test_now_recognized_and_agrees() -> None:
    sandbox = _sandbox_with({
        "calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n",
    })
    result = iv.recheck(sandbox, _files_changed("calc.py", "test_calc.py"), primary_passed=True)
    check("ran=True (real .py files + real tests were found and executed)", result.ran is True, result.to_json())
    check("agrees_with_primary=True (real test genuinely passes, no more false 'NO TESTS RAN')",
          result.agrees_with_primary is True, result.to_json())
    test_finding = next((f for f in result.findings if f["name"] == "independent_test_recheck"), None)
    check("independent_test_recheck itself passed", test_finding is not None and test_finding["passed"] is True, test_finding)
    check("'NO TESTS RAN' no longer appears for a real, plain pytest-style test file",
          test_finding is not None and "NO TESTS RAN" not in test_finding["detail"], test_finding)


def test_pytest_style_failing_test_still_correctly_detected() -> None:
    """A genuine test failure must still be caught -- the fix closes a
    false disagreement, it must never mask a real one."""
    sandbox = _sandbox_with({
        "calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 5\n",
    })
    result = iv.recheck(sandbox, _files_changed("calc.py", "test_calc.py"), primary_passed=False)
    check("ran=True", result.ran is True, result.to_json())
    check("independent recheck genuinely fails on a genuinely wrong assertion",
          any(f["name"] == "independent_test_recheck" and f["passed"] is False for f in result.findings),
          result.findings)
    check("agrees_with_primary=True (both primary and independent correctly failed)",
          result.agrees_with_primary is True, result.to_json())


def test_real_disagreement_still_reported_when_primary_and_independent_actually_differ() -> None:
    """If primary claims passed=True but the independent recheck's own
    compile step genuinely fails, that is a REAL disagreement and must
    still be reported loudly -- this fix must never suppress a genuine one."""
    sandbox = _sandbox_with({"broken.py": "def f(:\n    pass\n"})  # syntax error
    result = iv.recheck(sandbox, _files_changed("broken.py"), primary_passed=True)
    check("ran=True", result.ran is True, result.to_json())
    check("agrees_with_primary=False (primary said True, independent compile genuinely failed)",
          result.agrees_with_primary is False, result.to_json())
    check("reason names it as a DISAGREEMENT", result.reason is not None and "DISAGREEMENT" in result.reason, result.reason)


# --------------------------------------------------------------------- #
# Fallback path unaffected: unittest discover still used, and still
# correctly recognizes real unittest.TestCase-style tests, when pytest is
# genuinely unavailable.
# --------------------------------------------------------------------- #
def test_unittest_fallback_still_works_when_pytest_unavailable() -> None:
    sandbox = _sandbox_with({
        "calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": (
            "import unittest\nfrom calc import add\n\n\n"
            "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n"
        ),
    })
    with mock.patch.object(iv.importlib.util, "find_spec", return_value=None):
        result = iv.recheck(sandbox, _files_changed("calc.py", "test_calc.py"), primary_passed=True)
    check("falls back to unittest discover and still finds/passes a real TestCase-based test",
          result.ran is True and result.agrees_with_primary is True, result.to_json())
    test_finding = next((f for f in result.findings if f["name"] == "independent_test_recheck"), None)
    check("detail names unittest discover as the runner used in the fallback path",
          test_finding is not None and "unittest discover" in test_finding["detail"], test_finding)


# --------------------------------------------------------------------- #
# pytest exit code 5 (no tests collected) is a skip, not a failure --
# mirrors validation.py::check_tests()'s identical handling.
# --------------------------------------------------------------------- #
def test_pytest_no_tests_collected_is_skip_not_failure() -> None:
    # A file matching the test_*.py glob but with no actual test items in
    # it -- pytest collects zero tests and exits 5.
    sandbox = _sandbox_with({"test_empty.py": "# no tests here\n"})
    finding = iv._recheck_tests(sandbox)
    check("pytest exit code 5 (no tests collected) yields no finding at all (skip, not a failure)",
          finding is None, finding)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"raised {e!r}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nALL {len(tests)} TESTS PASSED")
