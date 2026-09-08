#!/usr/bin/env python3
"""
Tests for the TASK-SCOPING + VALIDATION/CANARY SEMANTICS REPAIR (2026-09-07).

Real, live, mechanically-verified incident this closes: proposal
9e67d645-69de-40f9-824d-a7506b570774 / implementation job
9746cea9-449a-4f12-b358-8bfcd2818fca reached promotion_candidate even though
it never repaired its target (evolution/advance.py::_build_task_text()) —
it only produced a disconnected advance_prototype.py, because (a)
_build_task_text() unconditionally asked for a "standalone prototype"
regardless of proposal intent, and (b) validation.check_tests() silently
SKIPPED (not failed) when no test file existed, so validate()'s vacuous
`all(c.passed for c in checks)` and canary's identical shallow re-check both
"passed" without ever proving anything behavioral.

This file is TESTS/FIXTURES ONLY — it never dispatches a real live
proposal, never triggers the timer, never calls run_cycle(), and old job
9746cea9's own historical records are never touched or reinterpreted.

Same plain-script style as tests/test_evolution_advance.py.

Run: python3 tests/test_task_scoping_and_validation_repair.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validation
from evolution import advance
from evolution import proposal as proposal_mod

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    assert condition, f"{name}" + (f" — {detail}" if detail else "")


def _cleanup(proposal_id: str, note: str) -> None:
    p = proposal_mod.load(proposal_id)
    if p.status not in proposal_mod.CLOSED_STATUSES:
        proposal_mod.advance(proposal_id, proposal_mod.ProposalStatus.REJECTED, note=note)


def _p(**kwargs) -> proposal_mod.Proposal:
    defaults = dict(observed_weakness="x", proposed_upgrade="y", risk_score="low", origin="test")
    defaults.update(kwargs)
    return proposal_mod.create(**defaults)


# ---- PART A: intent classification -----------------------------------

def test_1_no_source_paths_is_always_new_component() -> None:
    p = _p(observed_weakness="fix repair correct patch resolve", proposed_upgrade="anything")
    try:
        check("no source_paths -> always new_component regardless of wording",
              advance._classify_task_intent(p) == advance._INTENT_NEW_COMPONENT)
    finally:
        _cleanup(p.proposal_id, "test 1 cleanup")


def test_2_repair_verb_with_source_paths_classifies_repair() -> None:
    p = _p(observed_weakness="module.py::helper() has a bug", proposed_upgrade="fix the off-by-one error",
           source_paths=["some/module.py"])
    try:
        check("repair verb + source_paths -> repair_existing",
              advance._classify_task_intent(p) == advance._INTENT_REPAIR)
    finally:
        _cleanup(p.proposal_id, "test 2 cleanup")


def test_3_target_symbol_alone_classifies_repair() -> None:
    p = _p(observed_weakness="module.py::helper() always returns None", proposed_upgrade="return the real value",
           source_paths=["some/module.py"])
    try:
        check("exact target-symbol reference alone -> repair_existing even with no repair verb",
              advance._classify_task_intent(p) == advance._INTENT_REPAIR)
    finally:
        _cleanup(p.proposal_id, "test 3 cleanup")


def test_4_explicit_standalone_prototype_request_classifies_new_component() -> None:
    p = _p(observed_weakness="no reference implementation exists yet",
           proposed_upgrade="write a standalone prototype demonstrating the approach",
           source_paths=["some/module.py"])
    try:
        check("explicit 'standalone prototype' phrase, no repair verb/symbol -> new_component",
              advance._classify_task_intent(p) == advance._INTENT_NEW_COMPONENT)
    finally:
        _cleanup(p.proposal_id, "test 4 cleanup")


def test_5_no_signal_at_all_with_source_paths_is_ambiguous() -> None:
    p = _p(observed_weakness="something about the module", proposed_upgrade="make it better",
           source_paths=["some/module.py"])
    try:
        check("source_paths present, zero repair/new signal -> ambiguous (fail closed, never guess)",
              advance._classify_task_intent(p) == advance._INTENT_AMBIGUOUS)
    finally:
        _cleanup(p.proposal_id, "test 5 cleanup")


def test_6_contradictory_signal_is_ambiguous() -> None:
    p = _p(observed_weakness="module.py::helper() is broken",
           proposed_upgrade="fix it by writing a standalone prototype instead of touching the real file",
           source_paths=["some/module.py"])
    try:
        check("repair verb/symbol AND explicit standalone-prototype phrase together -> ambiguous",
              advance._classify_task_intent(p) == advance._INTENT_AMBIGUOUS)
    finally:
        _cleanup(p.proposal_id, "test 6 cleanup")


# ---- PART A: real fixtures that must NOT regress -----------------------

def test_7_real_9266a13c_shaped_fixture_still_classifies_and_does_not_raise() -> None:
    """Frozen 2026-09-06 fixture (test_authority_policy_negation_aware.py)
    — real proposal text, source_paths present, uses 'Add'/'Replace'
    wording. Must not raise TaskScopingAmbiguous."""
    p = _p(
        observed_weakness=(
            "omnisim/loop/omnisim_loop.py line 34 hardcodes the final report's status field to PASS "
            "regardless of whether any subprocess steps actually succeeded -- a prior sandboxed attempt "
            "implemented a materially correct fix and passed its own validation.py+canary (6/6 pytest "
            "tests), but was never promoted"
        ),
        proposed_upgrade=(
            "Add a small, pure function compute_status(steps) to omnisim/loop/omnisim_loop.py. "
            "Replace the hardcoded status PASS in the payload dict with compute_status(steps)'s result."
        ),
        source_paths=["/opt/pulse5-core/omnisim/loop/omnisim_loop.py"],
    )
    try:
        try:
            txt = advance._build_task_text(p)
            raised = False
        except advance.TaskScopingAmbiguous:
            raised = True
            txt = ""
        check("real 9266a13c-shaped fixture does not raise TaskScopingAmbiguous", raised is False)
        check("classifies as repair_existing (real fix language present)",
              advance._classify_task_intent(p) == advance._INTENT_REPAIR)
        check("task text instructs in-place modification, not a standalone prototype",
              "REPAIRING existing canonical source" in txt
              and "demonstrates/implements this upgrade as a standalone prototype" not in txt, txt[:200])
    finally:
        _cleanup(p.proposal_id, "test 7 cleanup")


def test_8_th3_probe_fixture_unchanged_no_source_paths() -> None:
    """Frozen fixture from test_th3_omni_router_integration.py — no
    source_paths set at all; must get byte-for-byte the old BUILD template."""
    p = proposal_mod.Proposal(
        proposal_id="probe-8", created_at="x",
        observed_weakness="PHASE 1: inspect calc.py -- calc.py has no discount() method.",
        proposed_upgrade="PHASE 3: implement discount() in calc.py. PHASE 4: test it, then repair and validate.",
        risk_score="low", origin="probe",
    )
    txt = advance._build_task_text(p)
    check("no source_paths -> unchanged legacy BUILD template (standalone prototype wording present)",
          "standalone prototype" in txt, txt[:200])


# ---- PART A: fail-closed integration through advance_one() -------------

def test_9_ambiguous_proposal_fails_closed_through_advance_one_not_rejected() -> None:
    p = _p(observed_weakness="something about the module", proposed_upgrade="make it better",
           source_paths=["some/module.py"])
    try:
        result = advance.advance_one(p.proposal_id, requested_by="test")
        check("ambiguous scoping blocks advancement (not eligible for implementation)",
              result.final_status != "promotion_candidate", result.final_status)
        check("blocked_reason names the ambiguous-scoping condition",
              result.blocked_reason == "ambiguous_task_scoping", result.blocked_reason)
        reloaded = proposal_mod.load(p.proposal_id)
        check("proposal is NOT rejected — stays eligible for a later attempt once refined",
              reloaded.status not in proposal_mod.CLOSED_STATUSES, reloaded.status)
    finally:
        _cleanup(p.proposal_id, "test 9 cleanup")


# ---- PART B: validation.check_tests() required-behavioral-test gate ----

def test_10_require_tests_false_and_no_tests_skips_as_before() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "x.py").write_text("x = 1\n")
        result = validation.check_tests(sandbox, {}, {})
        check("require_tests not set, no test files -> None (skip), unchanged legacy behavior", result is None)


def test_11_require_tests_true_and_no_tests_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "advance_prototype.py").write_text("def foo():\n    return 1\n")
        result = validation.check_tests(sandbox, {}, {"require_tests": True})
        check("require_tests=True, no test files -> a FAILING CheckResult, never a silent skip",
              result is not None and result.passed is False, result)


def test_12_require_tests_true_and_real_passing_test_passes() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "impl.py").write_text("def add(a, b):\n    return a + b\n")
        (sandbox / "test_impl.py").write_text(
            "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
            "from impl import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
        result = validation.check_tests(sandbox, {}, {"require_tests": True})
        check("require_tests=True with a real, passing test file -> passes", result is not None and result.passed is True, result)


def test_13_require_tests_true_and_failing_test_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
        result = validation.check_tests(sandbox, {}, {"require_tests": True})
        check("a genuinely failing required test fails the check", result is not None and result.passed is False, result)


def test_14_full_validate_vacuous_pass_closed_when_required() -> None:
    """The exact vacuous-pass mechanism that let job 9746cea9 through:
    validate()'s `passed = all(c.passed for c in checks)` is vacuously True
    when check_tests() is skipped. Proves it's no longer vacuous when a
    proposal's contract requires tests."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "advance_prototype.py").write_text("def helper():\n    return True\n")
        files_changed = {"added": ["advance_prototype.py"], "modified": [], "removed": []}

        legacy = validation.validate(sandbox, files_changed, config=None)
        check("legacy config=None still vacuously passes (unchanged old behavior, zero regression)",
              legacy.passed is True, legacy.to_json())

        required = validation.validate(sandbox, files_changed, config={"require_tests": True})
        check("require_tests=True closes the vacuous pass — overall validate() now fails",
              required.passed is False, required.to_json())


def test_15_syntax_only_cannot_fake_semantic_pass() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "advance_prototype.py").write_text("def helper():\n    return True  # syntactically perfect\n")
        result = validation.validate(sandbox, {"added": ["advance_prototype.py"], "modified": [], "removed": []},
                                      config={"require_tests": True})
        check("syntactically valid code alone cannot satisfy a required behavioral test contract",
              result.passed is False)


def test_16_arbitrary_test_skipping_not_possible_via_config_content() -> None:
    """require_tests is derived ONLY from the proposal's own governed
    validation_plan field (see _validation_config_for_proposal()), never
    from anything inside the sandbox/job content the model controls."""
    p = _p(observed_weakness="x", proposed_upgrade="y", risk_score="low", origin="test")
    try:
        cfg_no_plan = advance._validation_config_for_proposal(p)
        check("a proposal with no validation_plan gets require_tests=False (unchanged legacy default)",
              cfg_no_plan == {"require_tests": False}, cfg_no_plan)

        p2 = proposal_mod.refine(p.proposal_id, validation_plan="unit tests must prove X")
        cfg_with_plan = advance._validation_config_for_proposal(p2)
        check("a proposal that explicitly declared a validation_plan gets require_tests=True",
              cfg_with_plan == {"require_tests": True}, cfg_with_plan)
    finally:
        _cleanup(p.proposal_id, "test 16 cleanup")


# ---- PART C: canary NOT_REQUIRED / REQUIRED_AND_PASS / REQUIRED_AND_FAIL

def _run_fake_engine_to_canary(p: proposal_mod.Proposal, workdir: Path, files_changed: dict):
    """Drives the REAL advance_one() canary stage by faking only the engine
    runner's return value (job.workdir/files_changed/promotion_eligible) —
    everything from the canary call onward is the real, unmodified code
    path in evolution/advance.py."""
    from dataclasses import dataclass

    @dataclass
    class _FakeJob:
        job_id: str
        workdir: str
        files_changed: dict
        promotion_eligible: bool
        status: str = "succeeded"

    def fake_runner(task_text, requested_by, proposal_id):
        job = _FakeJob(job_id="fake-canary-job", workdir=str(workdir), files_changed=files_changed,
                       promotion_eligible=True)
        proposal_mod.append_implementation_job(proposal_id, job.job_id, engine="claude_code", note="fake")
        return advance.EngineAttempt("claude_code", "ran_cleanly", "fake", job.job_id, job.status, True), job

    original = advance._ENGINE_RUNNERS["claude_code"]
    advance._ENGINE_RUNNERS["claude_code"] = fake_runner
    try:
        return advance.advance_one(p.proposal_id, requested_by="test")
    finally:
        advance._ENGINE_RUNNERS["claude_code"] = original


def test_17_canary_not_required_when_no_canary_plan_declared() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y", risk_score="low", origin="test")
    try:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            (workdir / "x.py").write_text("x = 1\n")
            result = _run_fake_engine_to_canary(p, workdir, {"added": ["x.py"], "modified": [], "removed": []})
        check("no canary_plan declared -> reaches promotion_candidate (legacy behavior preserved)",
              result.final_status == "promotion_candidate", result.final_status)
        reloaded = proposal_mod.load(p.proposal_id)
        canary_note = next((h["note"] for h in reversed(reloaded.history) if h.get("status") == "canary"), "")
        check("proposal history records CANARY_NOT_REQUIRED", "CANARY_NOT_REQUIRED" in canary_note, canary_note)
    finally:
        _cleanup(p.proposal_id, "test 17 cleanup")


def test_18_canary_required_and_passes_with_real_test() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y", risk_score="low", origin="test")
    p = proposal_mod.refine(p.proposal_id, canary_plan="independent behavioral re-check")
    try:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            (workdir / "impl.py").write_text("def add(a, b):\n    return a + b\n")
            (workdir / "test_impl.py").write_text(
                "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
                "from impl import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            )
            files_changed = {"added": ["impl.py", "test_impl.py"], "modified": [], "removed": []}
            result = _run_fake_engine_to_canary(p, workdir, files_changed)
        check("declared canary_plan + real passing test -> reaches promotion_candidate",
              result.final_status == "promotion_candidate", result.final_status)
        reloaded = proposal_mod.load(p.proposal_id)
        canary_note = next((h["note"] for h in reversed(reloaded.history) if h.get("status") == "canary"), "")
        check("proposal history records CANARY_REQUIRED_AND_PASS", "CANARY_REQUIRED_AND_PASS" in canary_note, canary_note)
    finally:
        _cleanup(p.proposal_id, "test 18 cleanup")


def test_19_canary_required_and_fails_reproduces_9746cea9_failure_mode() -> None:
    """PART E: reproduces the exact semantic structure of the failed
    Item-4 job using tests/fixtures only — a hypothetical implementation
    that creates only advance_prototype.py (no test file), against a
    proposal that DID declare a canary_plan, must fail here rather than
    reach promotion_candidate."""
    p = _p(observed_weakness="evolution/advance.py::_build_task_text() needs repair",
           proposed_upgrade="fix the intent branching", source_paths=["mrsilent_bridge/evolution/advance.py"])
    p = proposal_mod.refine(p.proposal_id, canary_plan="independent behavioral re-check of the real fix")
    try:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            (workdir / "advance_prototype.py").write_text("def helper():\n    return True\n")
            files_changed = {"added": ["advance_prototype.py"], "modified": [], "removed": []}
            result = _run_fake_engine_to_canary(p, workdir, files_changed)
        check("PROTOTYPE_SUBSTITUTION_PREVENTED: standalone-prototype-only output with a declared "
              "canary_plan does NOT reach promotion_candidate", result.final_status != "promotion_candidate",
              result.final_status)
        check("blocked for the right reason (validation or canary failure, not something unrelated)",
              result.blocked_reason in ("validation failed", "canary failed"), result.blocked_reason)
    finally:
        _cleanup(p.proposal_id, "test 19 cleanup")


# ---- PART E: end-to-end semantic proof (task text + validation contract)

def test_20_post_repair_task_targets_existing_source_and_symbol() -> None:
    p = _p(
        observed_weakness="evolution/advance.py::_build_task_text() unconditionally asks for a prototype",
        proposed_upgrade="fix _build_task_text() to branch on intent instead",
        source_paths=["mrsilent_bridge/evolution/advance.py"],
    )
    try:
        txt = advance._build_task_text(p)
        check("TASK_TARGETS_EXISTING_SOURCE: task text names the real target file",
              "mrsilent_bridge/evolution/advance.py" in txt, txt[:300])
        check("TASK_TARGETS_BUILD_TASK_TEXT: task text preserves the exact target symbol",
              "_build_task_text()" in txt, txt[:300])
        check("PROTOTYPE_SUBSTITUTION_PREVENTED: task text never actually asks for a standalone prototype",
              "demonstrates/implements this upgrade as a standalone prototype" not in txt, txt[:300])
        check("task text explicitly forbids creating a separate demonstration file",
              "Do NOT create a new, separate, standalone file" in txt, txt)
    finally:
        _cleanup(p.proposal_id, "test 20 cleanup")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")
