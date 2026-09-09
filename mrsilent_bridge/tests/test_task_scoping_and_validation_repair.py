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
              cfg_no_plan == {"require_tests": False, "documentation_only_scope": False}, cfg_no_plan)

        p2 = proposal_mod.refine(p.proposal_id, validation_plan="unit tests must prove X")
        cfg_with_plan = advance._validation_config_for_proposal(p2)
        check("a proposal that explicitly declared a validation_plan gets require_tests=True",
              cfg_with_plan == {"require_tests": True, "documentation_only_scope": False}, cfg_with_plan)
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


# ---- PART F: APPROVED-SCOPE FIDELITY REPAIR (2026-09-09) ---------------
#
# Real, live incident: 7 Founder-approved founder_gated proposals (ranks
# 3/4/5/7/8/9/10, 2026-09-08/09) were dispatched using ONLY their original
# generic observed_weakness/proposed_upgrade placeholder text --
# implementation_scope/non_file_scope/validation_plan were silently
# ignored by _build_task_text(), producing 5 real, meaningless engine
# attempts that deterministically failed validation and were auto-
# REJECTED, while the approved scope was never once attempted.

def test_21_approved_implementation_scope_is_included_verbatim() -> None:
    p = _p(observed_weakness="Founder Top-10 rank X has no actionable proposal",
           proposed_upgrade="Founder/human review required -- this proposal deliberately does not scope itself")
    p = proposal_mod.refine(p.proposal_id,
                             implementation_scope="Wire provider_b_bridge.py to record job outcomes to job_ledger.")
    try:
        txt = advance._build_task_text(p)
        check("task text contains the exact approved implementation_scope text",
              "Wire provider_b_bridge.py to record job outcomes to job_ledger." in txt, txt[:300])
        check("task text marks the approved scope as authoritative",
              "APPROVED IMPLEMENTATION SCOPE" in txt and "authoritative" in txt, txt[:300])
    finally:
        _cleanup(p.proposal_id, "test 21 cleanup")


def test_22_non_file_scope_is_preserved_in_task_text() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y")
    p = proposal_mod.refine(p.proposal_id,
                             implementation_scope="Do the bounded thing.",
                             non_file_scope="organ_discovery.py (DUTY_CHECKED_ENGINES), no other files")
    try:
        txt = advance._build_task_text(p)
        check("task text preserves the declared non_file_scope",
              "organ_discovery.py (DUTY_CHECKED_ENGINES), no other files" in txt, txt[:300])
    finally:
        _cleanup(p.proposal_id, "test 22 cleanup")


def test_23_validation_plan_is_included_in_task_text() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y")
    p = proposal_mod.refine(p.proposal_id,
                             implementation_scope="Do the bounded thing.",
                             validation_plan="A real job_ledger record with selected_engine='provider_b' exists.")
    try:
        txt = advance._build_task_text(p)
        check("task text includes the declared validation_plan",
              "A real job_ledger record with selected_engine='provider_b' exists." in txt, txt[:300])
    finally:
        _cleanup(p.proposal_id, "test 23 cleanup")


def test_24_generic_proposed_upgrade_cannot_broaden_approved_scope() -> None:
    """A deliberately broader/contradictory proposed_upgrade must remain
    background context, never the operative instruction, once
    implementation_scope is declared."""
    p = _p(observed_weakness="the whole subsystem is bad",
           proposed_upgrade="rewrite the entire module and everything that touches it")
    p = proposal_mod.refine(p.proposal_id,
                             implementation_scope="Add exactly one optional field with a safe default.")
    try:
        txt = advance._build_task_text(p)
        approved_idx = txt.index("APPROVED IMPLEMENTATION SCOPE")
        background_idx = txt.index("Background context only")
        broad_idx = txt.index("rewrite the entire module and everything that touches it")
        check("approved scope block appears before the generic background text",
              approved_idx < background_idx < broad_idx, txt[:400])
        check("task text tells the engine to implement EXACTLY AND ONLY the approved scope",
              "EXACTLY AND ONLY" in txt, txt[:400])
        check("task text explicitly forbids scope expansion without a new Founder decision",
              "requires a new, separate Founder decision" in txt, txt[:400])
    finally:
        _cleanup(p.proposal_id, "test 24 cleanup")


def test_25_repair_intent_also_gets_approved_scope_block() -> None:
    """Approved-scope fidelity applies to the REPAIR branch too, not just
    the new-component branch."""
    p = _p(observed_weakness="module.py::helper() has a bug", proposed_upgrade="fix the off-by-one error",
           source_paths=["some/module.py"])
    p = proposal_mod.refine(p.proposal_id, implementation_scope="Only fix the off-by-one in helper(); nothing else.")
    try:
        txt = advance._build_task_text(p)
        check("REPAIR branch still fires for a source_paths+repair-verb proposal",
              "REPAIRING existing canonical source" in txt, txt[:300])
        check("REPAIR branch also includes the approved implementation_scope",
              "Only fix the off-by-one in helper(); nothing else." in txt, txt[:300])
    finally:
        _cleanup(p.proposal_id, "test 25 cleanup")


def test_26_protected_actions_still_named_forbidden_in_task_text() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y")
    p = proposal_mod.refine(p.proposal_id, implementation_scope="Do the bounded thing.")
    try:
        txt = advance._build_task_text(p)
        for phrase in ("no paid resources", "no credential/secret changes", "no production promotion",
                       "no destructive operations", "no model changes", "no isolation changes"):
            check(f"task text still names protected-action prohibition: {phrase!r}", phrase in txt, txt[:400])
    finally:
        _cleanup(p.proposal_id, "test 26 cleanup")


def test_27_no_implementation_scope_is_byte_for_byte_backward_compatible() -> None:
    """A proposal that never went through refine() (the overwhelming
    majority, including every existing low-risk proposal) must get
    EXACTLY the same task text as before this repair -- no approved-scope
    block, no background-context relabeling."""
    p = _p(observed_weakness="no reference implementation exists yet", proposed_upgrade="build one")
    try:
        txt = advance._build_task_text(p)
        check("no implementation_scope -> no APPROVED IMPLEMENTATION SCOPE block appears",
              "APPROVED IMPLEMENTATION SCOPE" not in txt, txt[:300])
        check("no implementation_scope -> no relabeling of observed_weakness/proposed_upgrade as background",
              "Background context only" not in txt, txt[:300])
        check("legacy unconditional BUILD template wording is unchanged",
              "Create one small, correct, self-contained file" in txt, txt[:300])
    finally:
        _cleanup(p.proposal_id, "test 27 cleanup")


# ---- DOCUMENTATION_VALIDATION_SEMANTICS (Founder-authorized 2026-09-09) ----
#
# Real, live incident: jobs d35ef27b (rank 8, ASSESSMENT.md) and 3597f88b
# (rank 10, pulseworld_survey_report.md) genuinely produced correct,
# real, non-empty documentation deliverables, but were rejected purely
# because check_tests() demanded a test_*.py/*_test.py file that makes no
# sense for a non-executable deliverable. documentation_only_scope is a
# CLAIM on the proposal (never independently sufficient); validation.py's
# _documentation_only_evidence() only ever honors it once the job's REAL
# changed sandbox files are evidence-confirmed to be pure documentation.

def test_28_pure_markdown_doc_change_does_not_require_test_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "ASSESSMENT.md").write_text("# Real assessment content\n\nReal findings here.\n")
        result = validation.validate(
            sandbox, {"added": ["ASSESSMENT.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("pure Markdown documentation change passes without a behavioral test file",
              result.passed, result.to_json()["checks"])
        names = [c["name"] for c in result.to_json()["checks"]]
        check("the deterministic documentation_only_validation check actually ran",
              "documentation_only_validation" in names, names)
        check("test_discovery_and_run never ran for confirmed doc-only scope",
              "test_discovery_and_run" not in names, names)


def test_29_pure_text_report_artifact_requires_declared_validation_plan_evidence() -> None:
    """The 'approved validation_plan still required' bar is enforced
    upstream by proposal_completeness()/is_decision_ready() (a founder_
    gated proposal with documentation_only_scope=True but no
    validation_plan is never decision-ready in the first place); at the
    validate() layer, this proves the doc-only evidence check itself
    still applies deterministic, real evidence (non-empty, real file)."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "census_report.txt").write_text("Real census counts: 2209 discovered, 211 evaluated.\n")
        result = validation.validate(
            sandbox, {"added": ["census_report.txt"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("pure text report artifact passes without a behavioral test file", result.passed, result.to_json()["checks"])

    p = proposal_mod.create(observed_weakness="x", proposed_upgrade="y", risk_score="founder_gated", origin="test")
    try:
        p = proposal_mod.refine(p.proposal_id, documentation_only_scope=True)
        check("a founder_gated proposal declaring documentation_only_scope=True but NO validation_plan "
              "(and none of the other completeness-gate fields) is still NOT decision-ready -- documentation_only_scope "
              "is additive, never a substitute for the existing completeness gate",
              not proposal_mod.is_decision_ready(p), proposal_mod.proposal_completeness(p))
    finally:
        _cleanup(p.proposal_id, "test 29 cleanup")


def test_30_python_plus_readme_still_requires_tests() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "helper.py").write_text("def add(a, b):\n    return a + b\n")
        (sandbox / "README.md").write_text("# helper\n\nAdds two numbers.\n")
        result = validation.validate(
            sandbox, {"added": ["helper.py", "README.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REGRESSION: Python + README mix is NEVER treated as documentation-only", not result.passed, result.to_json()["checks"])
        test_check = next((c for c in result.to_json()["checks"] if c["name"] == "test_discovery_and_run"), None)
        check("require_tests=True still fires for the real, non-doc-only mixed change",
              test_check is not None and test_check["passed"] is False, test_check)


def test_31_bash_script_change_still_requires_tests() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "deploy.sh").write_text("#!/bin/bash\necho hi\n")
        result = validation.validate(
            sandbox, {"added": ["deploy.sh"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REGRESSION: a .sh change is never treated as documentation-only even when declared", not result.passed, result.to_json()["checks"])


def test_32_runtime_config_change_still_requires_tests() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "service.yaml").write_text("restart: always\n")
        result = validation.validate(
            sandbox, {"added": ["service.yaml"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REGRESSION: a runtime config (.yaml) change is never treated as documentation-only even when declared",
              not result.passed, result.to_json()["checks"])


def test_33_self_label_bypass_prevented_when_targeting_executable_source() -> None:
    """DOC_ONLY_SELF_LABEL_BYPASS_PREVENTED: a proposal that merely SAYS
    'documentation' in free text but whose real changes are executable
    source must never bypass tests -- classification is evidence-based,
    never text-based."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "evil.py").write_text("import os\nos.system('echo pwned')\n")
        result = validation.validate(
            sandbox, {"added": ["evil.py"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("a declared documentation_only_scope=True is IGNORED once real evidence shows a .py file",
              not result.passed, result.to_json()["checks"])
        names = [c["name"] for c in result.to_json()["checks"]]
        check("documentation_only_validation never even reports a pass for this real .py change",
              not any(c["name"] == "documentation_only_validation" and c["passed"] for c in result.to_json()["checks"]), names)


def test_34_documentation_only_proposal_with_no_real_changes_cannot_silently_pass() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        result = validation.validate(
            sandbox, {"added": [], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REGRESSION: zero real changes + documentation_only_scope=True + require_tests=True still fails "
              "(falls through to the normal missing-test-file failure, never a silent pass)",
              not result.passed, result.to_json()["checks"])


def test_35_missing_required_documentation_artifact_fails() -> None:
    """An empty 'deliverable' must never satisfy documentation-only scope."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "ASSESSMENT.md").write_text("")
        result = validation.validate(
            sandbox, {"added": ["ASSESSMENT.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("an empty documentation artifact fails, never silently passes", not result.passed, result.to_json()["checks"])
        doc_check = next((c for c in result.to_json()["checks"] if c["name"] == "documentation_only_validation"), None)
        check("the failure is attributed to the documentation_only_validation check, naming the empty file",
              doc_check is not None and doc_check["passed"] is False and "empty" in doc_check["detail"], doc_check)


def test_36_unrelated_file_mutation_still_enforced_by_existing_scope_checks() -> None:
    """changed_file_inspection's existing protected-marker/sandbox-escape
    enforcement is completely untouched by this repair."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "ASSESSMENT.md").write_text("real content")
        result = validation.validate(
            sandbox, {"added": ["ASSESSMENT.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("existing changed_file_inspection check still runs and passes for an in-sandbox doc file",
              any(c["name"] == "changed_file_inspection" and c["passed"] for c in result.to_json()["checks"]),
              result.to_json()["checks"])


def test_37_existing_behavioral_validation_semantics_unchanged_for_non_doc_proposals() -> None:
    """A proposal that never sets documentation_only_scope at all (the
    overwhelming majority, including every historical proposal) must get
    byte-for-byte the same require_tests behavior as before this repair."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        result_no_tests_required = validation.validate(
            sandbox, {"added": [], "modified": [], "removed": []}, config={"require_tests": False},
        )
        check("require_tests=False with no documentation_only_scope key at all: unchanged legacy vacuous pass",
              result_no_tests_required.passed, result_no_tests_required.to_json())

        result_tests_required = validation.validate(
            sandbox, {"added": [], "modified": [], "removed": []}, config={"require_tests": True},
        )
        check("require_tests=True with no documentation_only_scope key at all: unchanged legacy failure on no test file",
              not result_tests_required.passed, result_tests_required.to_json())


def test_38_real_rank8_and_rank10_historical_sandboxes_now_validate_correctly() -> None:
    """Direct regression proof against the two REAL historical sandboxes
    this policy was authorized to fix -- not synthetic fixtures."""
    rank8_sandbox = Path("jobs/d35ef27b-a26c-4eac-9f4b-a0813d72de03/workdir")
    rank10_sandbox = Path("jobs/3597f88b-772a-4a8d-8edd-01f09cdc1b61/workdir")
    if rank8_sandbox.exists():
        result8 = validation.validate(
            rank8_sandbox, {"added": ["ASSESSMENT.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REAL rank 8 historical sandbox (ASSESSMENT.md) now validates correctly", result8.passed, result8.to_json()["checks"])
    if rank10_sandbox.exists():
        result10 = validation.validate(
            rank10_sandbox, {"added": ["pulseworld_survey_report.md"], "modified": [], "removed": []},
            config={"require_tests": True, "documentation_only_scope": True},
        )
        check("REAL rank 10 historical sandbox (pulseworld_survey_report.md) now validates correctly",
              result10.passed, result10.to_json()["checks"])


def test_39_proposal_field_roundtrips_and_defaults_to_none() -> None:
    p = _p(observed_weakness="x", proposed_upgrade="y")
    try:
        check("documentation_only_scope defaults to None (unanswered, never assumed False-as-code, never True)",
              p.documentation_only_scope is None, p.documentation_only_scope)
        p2 = proposal_mod.refine(p.proposal_id, documentation_only_scope=True)
        check("refine() can explicitly set documentation_only_scope=True", p2.documentation_only_scope is True, p2.documentation_only_scope)
        reloaded = proposal_mod.load(p.proposal_id)
        check("the declaration persists across a fresh disk load", reloaded.documentation_only_scope is True, reloaded.documentation_only_scope)
    finally:
        _cleanup(p.proposal_id, "test 39 cleanup")


def test_40_ambiguous_scope_with_no_documentation_only_declaration_fails_closed() -> None:
    """A proposal that never declares documentation_only_scope at all
    (the ambiguous/default case) must fail closed -- normal require_tests
    behavior applies, exactly as before this repair existed."""
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        (sandbox / "ASSESSMENT.md").write_text("real content")
        result = validation.validate(
            sandbox, {"added": ["ASSESSMENT.md"], "modified": [], "removed": []},
            config={"require_tests": True},  # documentation_only_scope NOT set at all
        )
        check("REGRESSION: with no documentation_only_scope declared, a pure .md change still fails require_tests=True "
              "(fail-closed default -- the doc-only lane is opt-in only, never inferred)",
              not result.passed, result.to_json()["checks"])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")
