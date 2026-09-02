"""Omni Oracle request_oracle API -- governed forecast/consensus capability facade.

CANONICAL IDENTITY (OMNISIM_OMNI_ORACLE_GOD_MODE_V1 campaign, 2026-09-01)
--------------------------------------------------------------------------
This module is the single, documented, testable, invocable entry point for
"Omni Oracle" -- the forecasting / consensus layer that sits alongside OmniSim
(omnisim/api/request_scenario.py, decision-support scenario simulation).

Before this file existed, Oracle was 27 independent scripts under
divisions/omnioracle/<subsystem>/ that read each other's JSON outputs off
disk and run on a live systemd timer (omnioracle-continuous-daemon.timer),
with no function-callable, validated, single request/response surface. This
file does not replace or duplicate that pipeline -- it is a read/aggregate
and (optionally) trigger facade in front of the real, existing engines.

HONESTY NOTE -- READ BEFORE TRUSTING ANY confidence/probability FIELD BELOW
--------------------------------------------------------------------------
The underlying engines this facade reads from (probabilistic_forecast_engine,
consensus_intelligence_layer, recursive_simulation_engine) generate their
confidence/risk/outcome values with Python's unseeded `random` module -- not
a trained model, not a live external signal feed, and not calibrated against
real outcomes. divisions/omnioracle/real_signal_connector's connectors are
all explicitly "blueprint_ready" / "live_collection_enabled": false -- no
live external data is ingested anywhere in this division as of this writing.
forecast_outcome_verifier explicitly refuses to update reliability scores
until real outcomes are confirmed ("real_outcome_required": true, "scoring_
policy": "do_not_update_reliability_until_real_outcome_confirmed"). See also
divisions/omnioracle/final_godmode_audit/reports/omnioracle_final_godmode_gap_audit_v1.json
("true_operational_god_mode_declared": false).

Therefore every "confidence"/"probability_band" value this facade surfaces is
a SYNTHETIC PLACEHOLDER (result["synthetic_projection"]["synthetic"] is
always True), not a calibrated statistical estimate and not evidence of
real-world predictive accuracy. This facade separates real, counted
`observations` (how many artifacts actually exist on disk right now) from
that synthetic projection so a caller can never confuse the two. Treat the
synthetic projection like a labeled mock in a UI prototype: useful for
testing the pipeline shape, not a real forecast, until real_signal_connector
goes live and forecast_outcome_verifier has accumulated real outcome history.

Never represent output from this module as a guaranteed or calibrated
prediction. Oracle has no authority to execute, dispatch, or approve
anything itself. Founder approval is still required for any protected-gate
action regardless of this output.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

ORACLE = Path(__file__).resolve().parent.parent  # divisions/omnioracle
RECEIPTS = Path(__file__).resolve().parent / "receipts"

KNOWN_DOMAINS = [
    "runtime_autonomy",
    "swarm_coordination",
    "prediction_accuracy",
    "cross_division_growth",
    "strategic_expansion",
]

# Real, existing engine scripts this facade reads from / can trigger.
# It does not reimplement their logic -- it shells out to the same scripts
# the live omnioracle-continuous-daemon.timer already runs.
ENGINE_SCRIPTS = {
    "probabilistic_forecast": ORACLE / "probabilistic_forecast_engine/omnioracle_probabilistic_forecast_engine_v1.py",
    "consensus": ORACLE / "consensus_intelligence_layer/omnioracle_consensus_intelligence_layer_v1.py",
}

FORECAST_DIR = ORACLE / "probabilistic_forecast_engine/forecasts"
CONSENSUS_DIR = ORACLE / "consensus_intelligence_layer/consensus"
LEDGER_DIR = ORACLE / "forecast_ledger/entries"
VERIFICATIONS_DIR = ORACLE / "forecast_outcome_verifier/verifications"

RECOMMENDATION_BOUNDARIES = [
    "This output is decision-support / pipeline-shape evidence, not a prediction of fact or a "
    "guarantee of outcome. Confidence and probability fields are synthetic placeholders (see module "
    "docstring) until real_signal_connector is live and forecast_outcome_verifier has real outcome history.",
    "Founder approval is still required for any protected-gate action (credentials, paid-resource "
    "activation, production promotion, model deletion/replacement, destructive/irreversible actions) "
    "regardless of this output.",
    "Omni Oracle has no authority to execute, dispatch, or approve any action itself.",
]


class OracleValidationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_id(domain, question, mode, seed) -> str:
    h = hashlib.sha256(f"{domain}|{question}|{mode}|{seed}|{_now()}".encode()).hexdigest()[:16]
    return f"ORC_{h}"


def _validate(domain, mode, min_evidence):
    if not isinstance(domain, str) or not domain.strip():
        raise OracleValidationError("domain must be a non-empty string")
    if mode not in ("read", "generate"):
        raise OracleValidationError(f"mode must be 'read' or 'generate', got {mode!r}")
    if not isinstance(min_evidence, int) or min_evidence < 0:
        raise OracleValidationError("min_evidence must be a non-negative int")


def _domain_evidence(domain):
    """Deterministic aggregation over whatever is ALREADY on disk for this domain.

    This is real: it counts and reads actual files written by the live pipeline
    (systemd timer omnioracle-continuous-daemon.timer, and any earlier requests
    through this facade). No randomness is introduced here.
    """
    forecast_files = sorted(FORECAST_DIR.glob(f"*{domain}*.json")) if FORECAST_DIR.exists() else []
    ledger_files = sorted(LEDGER_DIR.glob("*.json")) if LEDGER_DIR.exists() else []
    ledger_for_domain = []
    for f in ledger_files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("domain") == domain:
            ledger_for_domain.append((f, data))

    latest_forecast = None
    if forecast_files:
        latest = max(forecast_files, key=lambda p: p.stat().st_mtime)
        try:
            latest_forecast = json.loads(latest.read_text())
        except Exception:
            latest_forecast = None

    consensus_files = sorted(CONSENSUS_DIR.glob("*.json")) if CONSENSUS_DIR.exists() else []

    verification_count = 0
    if VERIFICATIONS_DIR.exists():
        for f in VERIFICATIONS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("domain") == domain:
                verification_count += 1

    return {
        "forecast_artifact_count": len(forecast_files),
        "latest_forecast_path": str(max(forecast_files, key=lambda p: p.stat().st_mtime)) if forecast_files else None,
        "latest_forecast": latest_forecast,
        "ledger_entries_for_domain": [
            {"path": str(f), "prediction_id": d.get("prediction_id"), "confidence": d.get("confidence"),
             "status": d.get("status"), "outcome_verified": d.get("outcome_verified")}
            for f, d in ledger_for_domain
        ],
        "consensus_artifact_count_total": len(consensus_files),
        "verification_count_for_domain": verification_count,
    }


def _run_engine(script_path: Path, timeout=90):
    if not script_path.exists():
        return {"engine": str(script_path), "ok": False, "status": "missing", "exit_code": None}
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "engine": str(script_path),
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stderr_sample": proc.stderr[-500:] if proc.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"engine": str(script_path), "ok": False, "status": "timeout", "exit_code": None}
    except Exception as exc:  # bounded failover: one engine's crash must not take down the request
        return {"engine": str(script_path), "ok": False, "status": f"error: {exc}", "exit_code": None}


def request_oracle(domain, question=None, *, mode="read", seed=None, min_evidence=0,
                    also_run_omnisim_scenario=None):
    """Request an Omni Oracle forecast/consensus read (or, opt-in, a fresh generate cycle).

    mode="read" (default): purely aggregates whatever real evidence already exists on
        disk for `domain` (written by the live continuous_oracle_daemon timer or prior
        calls to this facade). Fully deterministic given fixed on-disk state --
        result["reproducible"] is True.

    mode="generate": triggers the real probabilistic_forecast_engine and
        consensus_intelligence_layer scripts fresh (the same scripts the live systemd
        timer runs), then re-aggregates. These engines use unseeded `random` internally,
        so result["reproducible"] is honestly reported False in this mode -- this facade
        does not fabricate determinism it cannot provide.

    min_evidence: if the domain has fewer than this many forecast/ledger/consensus
        artifacts after aggregation, the request fails safely with
        status="insufficient_evidence" instead of fabricating a result.

    also_run_omnisim_scenario: if a question string is given, also calls OmniSim's own
        governed request_scenario() (omnisim/api/request_scenario.py) and includes it
        under result["omnisim_scenario"] -- demonstrating the OmniSim/Oracle ensemble
        with clear responsibility boundaries: OmniSim = decision-support scenario
        heuristics, Oracle = forecast/consensus aggregation. Failure of the OmniSim call
        does not fail the Oracle request (bounded failover); it is reported inline.
    """
    _validate(domain, mode, min_evidence)

    request_id = _request_id(domain, question, mode, seed)
    timestamp = _now()

    engine_runs = []
    reproducible = True
    if mode == "generate":
        reproducible = False
        for key in ("probabilistic_forecast", "consensus"):
            engine_runs.append(_run_engine(ENGINE_SCRIPTS[key]))

    evidence = _domain_evidence(domain)
    evidence_count = (
        evidence["forecast_artifact_count"]
        + len(evidence["ledger_entries_for_domain"])
        + evidence["verification_count_for_domain"]
    )

    domain_recognized = domain in KNOWN_DOMAINS

    if evidence_count < min_evidence:
        result = {
            "request_id": request_id,
            "timestamp": timestamp,
            "status": "insufficient_evidence",
            "request": {"domain": domain, "question": question, "mode": mode, "seed": seed,
                        "min_evidence": min_evidence},
            "domain_recognized": domain_recognized,
            "observations": evidence,
            "evidence_count": evidence_count,
            "engine_runs": engine_runs,
            "reproducible": reproducible,
            "recommendation_boundary": RECOMMENDATION_BOUNDARIES,
        }
        _write_receipt(request_id, result)
        return result

    latest = evidence.get("latest_forecast")
    synthetic_projection = None
    if latest:
        synthetic_projection = {
            "synthetic": True,
            "source_artifact": evidence["latest_forecast_path"],
            "confidence_score": latest.get("confidence_score"),
            "probability_band": latest.get("probability_band"),
            "risk_factor": latest.get("risk_factor"),
            "estimated_success_probability": latest.get("estimated_success_probability"),
            "recommended_action": latest.get("recommended_action"),
        }

    result = {
        "request_id": request_id,
        "timestamp": timestamp,
        "status": "ok",
        "request": {"domain": domain, "question": question, "mode": mode, "seed": seed,
                    "min_evidence": min_evidence},
        "domain_recognized": domain_recognized,
        "observations": evidence,
        "evidence_count": evidence_count,
        "synthetic_projection": synthetic_projection,
        "engine_runs": engine_runs,
        "reproducible": reproducible,
        "uncertainty": {
            "kind": "synthetic_placeholder_pending_real_signal_ingestion",
            "note": "See module docstring: confidence/probability values above are generated by unseeded "
                    "random sampling in the underlying engines, not a calibrated model or live data. "
                    "Do not treat them as real predictive accuracy.",
        },
        "limitations": [
            "No live external signal ingestion exists yet (real_signal_connector connectors are all "
            "blueprint_ready, live_collection_enabled=false).",
            "forecast_outcome_verifier will not update reliability scores until real outcomes are "
            "confirmed (real_outcome_required=true).",
        ],
        "recommendation_boundary": RECOMMENDATION_BOUNDARIES,
    }

    if also_run_omnisim_scenario:
        try:
            result["omnisim_scenario"] = _try_omnisim_scenario(also_run_omnisim_scenario)
        except Exception as exc:  # defense in depth: even a bug inside the helper must not fail the request
            result["omnisim_scenario"] = {"ok": False, "engine": "omnisim.api.request_scenario", "error": str(exc)}

    _write_receipt(request_id, result)
    return result


def _try_omnisim_scenario(question):
    try:
        omnisim_api = ORACLE.parent.parent / "omnisim" / "api"
        if str(omnisim_api) not in sys.path:
            sys.path.insert(0, str(omnisim_api))
        import request_scenario as _omnisim  # local import: optional ensemble dependency
        scenario = _omnisim.request_scenario(question, scenario_type="decision_heuristic")
        return {"ok": True, "engine": "omnisim.api.request_scenario", "result": scenario}
    except Exception as exc:  # bounded failover: OmniSim unavailable must not fail the Oracle request
        return {"ok": False, "engine": "omnisim.api.request_scenario", "error": str(exc)}


def _write_receipt(request_id, result):
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / f"{request_id}_receipt.json").write_text(json.dumps(result, indent=2, default=str))
    result["receipt_path"] = str(RECEIPTS / f"{request_id}_receipt.json")


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Omni Oracle request_oracle CLI")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--question", default=None)
    ap.add_argument("--mode", default="read", choices=["read", "generate"])
    ap.add_argument("--seed", default=None)
    ap.add_argument("--min-evidence", type=int, default=0)
    ap.add_argument("--also-run-omnisim-scenario", default=None)
    args = ap.parse_args()
    result = request_oracle(
        args.domain, args.question, mode=args.mode, seed=args.seed,
        min_evidence=args.min_evidence, also_run_omnisim_scenario=args.also_run_omnisim_scenario,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _cli()
