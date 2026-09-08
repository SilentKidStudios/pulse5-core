"""OmniForge — governed capability construction/integration organ.

PHASE K (Founder-authorized 2026-08-17, with continuity requirements).
Historical reconciliation: no file, directory, or code implementation named
"OmniForge" exists anywhere on this machine — confirmed by a whole-filesystem
search, consistent with this project's own prior finding
(registry_data/capabilities.json's `studio_planned_roles_omniforge_
blackvault_omniguard_aed_omnicompetitio` entry: status
planned_not_implemented, "build_tools" role, never built). This module is
the first real implementation of that planned role, scoped to exactly what
the evidence showed was missing: a governed layer that turns a
black_vault-evaluated candidate into a Studio-native adapter — using
OmniEngineer for the actual code-writing, not a second execution engine.

integrate() does NOT run, install, import, or execute the external
candidate's own code at any point — it asks OmniEngineer to write a NEW,
small, sandboxed, tested, DELIBERATELY MINIMAL placeholder stub module (a
single describe() function identifying the capability, not a functional
reimplementation) — tightly scoped on purpose after a live test run showed
an open-ended "write a working wrapper" prompt tempted the model into
over-scoping (a full from-scratch reimplementation of nontrivial upstream
functionality), which is worse local-model reliability for no real safety
benefit. A stub is exactly the right size for what a governed FIRST
integration step should produce; expanding a stub into real functionality
is separate, later, human-directed work. Same bounded sandbox/validation/
canary pipeline every other OmniEngineer job in this project already goes
through. Nothing here grants the resulting adapter any authority beyond
what OmniEngineer jobs already have (sandbox-only, never self-modifying
mrsilent_bridge, never touching Scorpio/credentials/protected paths —
authority_policy.classify() still runs on
every job, unchanged).

integrate() never writes directly to registry_data/capabilities.json — like
every other capability record in this project, a new entry requires a
human/Claude-reviewed proposal, not an automatic merge. On a successful
OmniEngineer build, this creates exactly that: an evolution.proposal at
PROMOTION_CANDIDATE-eligible status once validated, never further.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import black_vault
import omniengineer_harness
import validation
from evolution import proposal as proposal_mod

OMNIFORGE_IMPLEMENT_TIMEOUT_S = 180  # same order as evolution/advance.py's OMNI_IMPLEMENT_TIMEOUT_S


@dataclass
class IntegrationResult:
    status: str  # "rejected_not_eligible" | "succeeded" | "failed" | "duplicate_in_flight"
    quarantine_id: str
    job_id: str | None = None
    proposal_id: str | None = None
    reason: str = ""


_INELIGIBLE_VERDICTS = frozenset({"rejected"})


def integrate(quarantine_id: str, *, requested_by: str) -> IntegrationResult:
    record = black_vault.load(quarantine_id)
    if record is None:
        return IntegrationResult(status="rejected_not_eligible", quarantine_id=quarantine_id,
                                  reason="no such quarantine record")
    if record.verdict in _INELIGIBLE_VERDICTS:
        return IntegrationResult(status="rejected_not_eligible", quarantine_id=quarantine_id,
                                  reason=f"quarantine verdict={record.verdict!r} is not forge-eligible")

    p = proposal_mod.create(
        observed_weakness=f"External capability candidate {record.name!r} ({record.source_identifier}) was "
                           f"quarantined and recommended for forge review (verdict={record.verdict!r}).",
        proposed_upgrade=f"Build a small, self-contained, sandboxed Studio adapter module wrapping "
                          f"{record.name!r}'s relevant functionality, with a unit test proving it works, "
                          f"consistent with this project's existing OmniEngineer sandbox conventions. Do not "
                          f"install, import, or execute {record.name!r}'s own upstream code — write a fresh, "
                          f"minimal, reviewed implementation informed by its documented interface.",
        risk_score="low",
        origin="manual",
    )

    module_name = _safe_module_name(record.name)
    task_text = (
        f"Create a small self-contained Python module `{module_name}_adapter.py` that provides EXACTLY ONE "
        f"function, `describe() -> dict`, returning a fixed dict with exactly these keys: "
        f"'capability_name' (value: {record.name!r}), 'origin' (value: {record.origin!r}), "
        f"'status' (value: 'stub_registered'), 'notes' (value: a one-sentence string describing that this "
        f"is a placeholder Studio adapter stub, not a reimplementation of {record.name!r}'s functionality). "
        f"This is deliberately a minimal placeholder stub, NOT a functional reimplementation — do not add any "
        f"other functions, classes, or logic. Add a matching unittest test file `test_{module_name}_adapter.py` "
        f"with exactly one test asserting describe() returns a dict containing those exact key/value pairs. Run "
        f"the tests with python3 -m unittest to confirm they pass, then call run_validator. Do not attempt to "
        f"download, install, or execute {record.name!r}'s own package."
    )

    job = omniengineer_harness.submit_job(
        task=task_text, requested_by=requested_by, timeout_s=OMNIFORGE_IMPLEMENT_TIMEOUT_S,
        founder_approved=False,
        on_job_created=lambda jid: proposal_mod.append_implementation_job(p.proposal_id, jid, engine="omni_engineer",
                                                                            note="omniforge integration attempt"),
    )

    def _fail(reason: str) -> IntegrationResult:
        proposal_mod.record_experience(
            p.proposal_id,
            incident_fingerprint={"symptom": f"omniforge integration failed for {record.name}",
                                   "error_signature": f"omniforge_integration_failure:{record.source_identifier}",
                                   "affected_organ": "omniforge", "environment_context": "external_evolution_pipeline"},
            root_cause={"explanation": reason, "confidence": "high", "evidence": str(job.policy_reasons or job.status)},
            remediation={"procedure": "not yet found", "authority_required": "n/a", "affected_files_or_services": []},
            outcome={"success": False, "performance": "n/a", "side_effects": "sandboxed job only, no Studio impact"},
            negative_knowledge=[f"attempting to forge-integrate {record.source_identifier} via a direct "
                                 f"OmniEngineer prompt failed ({reason}) — do not blindly retry identically "
                                 f"without new evidence"],
        )
        proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.REJECTED,
                              note=f"omniforge integration attempt failed: {reason}")
        return IntegrationResult(status="failed", quarantine_id=quarantine_id, job_id=job.job_id,
                                  proposal_id=p.proposal_id, reason=reason)

    # DOMAIN_D_TAXONOMY gap-closure (2026-09-08): real, live evidence found —
    # 2 real integrate() calls saw job.status=="duplicate_suppressed" and
    # both were routed through _fail() exactly like a genuine content
    # failure (deterministic-reject, "confidence": "high" root-cause,
    # permanent proposal REJECTED). That's factually wrong: omniengineer_
    # harness._duplicate_result()'s own docstring/behavior confirms
    # duplicate_suppressed means a DIFFERENT, STILL-ACTIVE job (job.job_id
    # here is that existing job's id, not a new one) is already in flight
    # for the identical task — nothing about THIS candidate or its content
    # failed; a concurrent dispatch was correctly suppressed. Writing a
    # high-confidence "integration failed" experience record and
    # permanently rejecting the proposal for this case fabricates a
    # negative finding about a candidate that may well succeed via the
    # job already running. Never retried automatically here (that remains
    # opportunistic re-discovery on a future scouting pass, unchanged) —
    # this only stops mislabeling a non-failure as one.
    if job.status == "duplicate_suppressed":
        return IntegrationResult(
            status="duplicate_in_flight", quarantine_id=quarantine_id, job_id=job.job_id,
            proposal_id=p.proposal_id,
            reason=f"job {job.job_id} for this exact task is already in flight — not a failure, not retried "
                   "here; proposal left open for a future pass",
        )

    if job.status != "succeeded":
        return _fail(f"omniengineer status={job.status!r}")

    # Mirror evolution/advance.py's exact IMPLEMENTED -> TESTED -> CANARY ->
    # PROMOTION_CANDIDATE progression — same terminal state, same meaning:
    # ready for a human to review and `cli.py promote --founder-approved`,
    # never promoted by this pipeline itself.
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.IMPLEMENTED,
                          note=f"omniforge: job {job.job_id} succeeded via omni_engineer")
    if not job.promotion_eligible:
        return _fail(f"automatic validation failed for job {job.job_id}")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.TESTED,
                          note=f"validation passed for job {job.job_id}")

    canary = validation.validate(Path(job.workdir), job.files_changed, config=None)
    if not canary.passed:
        return _fail("independent canary re-validation did not reproduce PASS")
    proposal_mod.advance(p.proposal_id, proposal_mod.ProposalStatus.CANARY, note="canary passed")

    proposal_mod.advance(
        p.proposal_id, proposal_mod.ProposalStatus.PROMOTION_CANDIDATE,
        note=f"ready for human-directed promotion: omniforge-integrated adapter for {record.name!r}, "
             f"sandbox at {job.workdir}, run `cli.py promote {job.job_id} --target <path> --founder-approved`",
    )
    proposal_mod.record_experience(
        p.proposal_id,
        incident_fingerprint={"symptom": f"needed a Studio adapter for {record.name}",
                               "error_signature": f"omniforge_integration_success:{record.source_identifier}",
                               "affected_organ": "omniforge", "environment_context": "external_evolution_pipeline"},
        root_cause={"explanation": "no internal capability covered this need (see black_vault duplicate-check)",
                    "confidence": "medium", "evidence": "black_vault evaluation + successful OmniEngineer build + canary"},
        remediation={"procedure": f"OmniEngineer built a sandboxed adapter for {record.name}", "authority_required": "none (sandbox)",
                     "affected_files_or_services": job.files_changed},
        validation_contract=["validation.validate() gate passed", "independent canary re-validation passed"],
        outcome={"success": True, "performance": "n/a", "side_effects": "sandbox only; not promoted"},
    )
    return IntegrationResult(status="succeeded", quarantine_id=quarantine_id, job_id=job.job_id,
                              proposal_id=p.proposal_id, reason="reached PROMOTION_CANDIDATE (not promoted)")


def _safe_module_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "candidate"
