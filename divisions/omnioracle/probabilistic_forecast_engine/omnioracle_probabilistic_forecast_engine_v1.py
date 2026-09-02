#!/usr/bin/env python3
"""Probabilistic forecast engine -- DETERMINISTIC (rewritten 2026-09-02, see
divisions/omnioracle/api/deterministic_forecast_core.py).

Before this rewrite, this engine ignored the actual content of consensus/simulation/
ledger files it counted and drew confidence/risk independently from
random.uniform() -- numbers structurally unrelated to the evidence they were
labeled as summarizing.

Now: for each domain, it reads the REAL consensus_intelligence_layer outputs and
forecast_ledger entries for that domain, and derives a base heuristic score from
consensus_intelligence_layer's own (now-deterministic) average_confidence values when
present, refined with explicit seeded Monte Carlo dispersion. If a domain has zero
matching evidence artifacts, it fails closed with status=INSUFFICIENT_DATA rather
than fabricating a projection -- it no longer unconditionally emits one number per
domain from the fixed domain list."""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import (
    heuristic_base_score, seeded_monte_carlo, content_seed,
    canonical_forecast_result,
)

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

LEDGER=ORACLE/"forecast_ledger/entries"
CONS=ORACLE/"consensus_intelligence_layer/consensus"
SIM=ORACLE/"recursive_simulation_engine/branches"
COUNCIL=ORACLE/"autonomous_strategy_council/recommendations"

OUT=ORACLE/"probabilistic_forecast_engine/forecasts"
STATE=ORACLE/"probabilistic_forecast_engine/state"

OUT.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

domains=[
    "runtime_autonomy",
    "swarm_coordination",
    "prediction_accuracy",
    "cross_division_growth",
    "strategic_expansion",
]


def _domain_consensus(domain):
    """Real consensus files whose objective/source mentions this domain."""
    if not CONS.exists():
        return []
    matches = []
    for f in CONS.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if domain in (data.get("objective") or "") or domain in (data.get("source_simulation") or ""):
            matches.append(data)
    return matches


def _domain_ledger(domain):
    if not LEDGER.exists():
        return []
    out = []
    for f in LEDGER.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("domain") == domain:
            out.append(data)
    return out


def forecast_domain(domain, seed_override=None, min_evidence=1):
    # Defense in depth: an empty/blank domain must never trivially "match" every
    # consensus/ledger file via substring containment -- fail closed immediately.
    if not domain or not str(domain).strip():
        seed = seed_override if seed_override is not None else content_seed("empty_domain")
        mc = seeded_monte_carlo(0.5, seed=seed, n_samples=1)
        result = canonical_forecast_result(
            forecast=None, evidence_inputs={"reason": "empty or blank domain"},
            scenario_assumptions={"domain": domain}, model_or_engine_contributors=[],
            mc=mc, evidence_count=0, min_evidence=max(min_evidence, 1),
        )
        result["domain"] = domain
        return result

    consensus_matches = _domain_consensus(domain)
    ledger_matches = _domain_ledger(domain)
    simulation_files = list(SIM.glob("*.json")) if SIM.exists() else []
    council_files = list(COUNCIL.glob("*.json")) if COUNCIL.exists() else []

    evidence_count = len(consensus_matches) + len(ledger_matches)

    # Base heuristic: prefer real upstream consensus confidence when available
    # (already a deterministic ensemble aggregate); otherwise fall back to a
    # transparent text heuristic over the domain name and any ledger predictions,
    # which is still real input, just weaker evidence.
    if consensus_matches:
        base_score = round(sum(c.get("average_confidence", 0.5) for c in consensus_matches) / len(consensus_matches), 4)
        contributors = ["consensus_intelligence_layer (average_confidence over matching consensus files)"]
    elif ledger_matches:
        text = " ".join(m.get("prediction", "") for m in ledger_matches)
        base_score = heuristic_base_score(f"{domain} {text}")
        contributors = ["forecast_ledger (heuristic score over recorded prediction text)"]
    else:
        base_score = heuristic_base_score(domain)
        contributors = ["heuristic_base_score(domain) -- no consensus/ledger evidence found"]

    seed = seed_override if seed_override is not None else content_seed(domain, len(consensus_matches), len(ledger_matches))
    mc = seeded_monte_carlo(base_score, seed=seed, n_samples=200, spread=0.10)

    cross_source_agreement = None
    if consensus_matches:
        cross_source_agreement = round(
            sum(c.get("agreement_fraction", 0.5) for c in consensus_matches) / len(consensus_matches), 4
        )

    result = canonical_forecast_result(
        forecast={
            "domain": domain,
            "recommended_action": "continue_expansion" if base_score >= 0.6 else "increase_validation",
        },
        evidence_inputs={
            "consensus_files_matched": len(consensus_matches),
            "ledger_entries_matched": len(ledger_matches),
            "simulation_files_total_on_disk": len(simulation_files),
            "council_files_total_on_disk": len(council_files),
        },
        scenario_assumptions={"future_window_days": 30, "domain": domain},
        model_or_engine_contributors=contributors,
        mc=mc,
        evidence_count=evidence_count,
        min_evidence=min_evidence,
        cross_source_agreement=cross_source_agreement,
        id_parts=(domain,),
    )
    result["domain"] = domain

    if result["status"] == "OK":
        _register_ledger_entry(result, domain)

    return result


def _register_ledger_entry(result, domain):
    """Give every OK forecast an immutable forecast_ledger entry so
    forecast_outcome_verifier/calibration.py:record_real_outcome() can later be
    called against it -- closes the forecast-created -> real-outcome-recorded loop
    for forecasts made through this engine (not just the 3 hardcoded seed
    predictions). Each call gets its own distinct forecast_id (timestamp is part of
    the identity, matching this codebase's existing forecast_ledger/
    forecast_outcome_verifier convention of one file per event, not an
    upsert-by-content-key) -- a real outcome attaches to the specific forecast
    instance it confirms, not to "whatever the latest matching one happens to be"."""
    LEDGER.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": result["timestamp"],
        "ledger_engine": "omnioracle_probabilistic_forecast_engine_v1",
        "prediction_id": result["forecast_id"],
        "domain": domain,
        "prediction": result["forecast"].get("recommended_action") if result.get("forecast") else None,
        "confidence": result["confidence"],
        "signals": result["model_or_engine_contributors"],
        "expected_outcome": result["forecast"],
        "status": "tracking",
        "ledger_digest": result["forecast_id"].replace("FCID_", ""),
        "outcome_verified": False,
        "accuracy_score": None,
        "learning_feedback": [],
    }
    # calibration.py:_find_ledger_file globs "{prediction_id}_*.json" (matching the
    # original forecast_ledger seed-entry naming convention), so this filename must
    # carry a trailing suffix after the prediction_id, not be exactly "{id}.json".
    (LEDGER / f"{result['forecast_id']}_entry.json").write_text(json.dumps(entry, indent=2))


def run_all_domains(min_evidence=1, seed_override=None):
    forecast_models=[]
    results = {}

    for domain in domains:
        result = forecast_domain(domain, seed_override=seed_override, min_evidence=min_evidence)
        results[domain] = result

        hid_source = json.dumps({k: v for k, v in result.items() if k != "timestamp"}, sort_keys=True, default=str)
        import hashlib
        hid = hashlib.sha256(hid_source.encode()).hexdigest()[:16]
        fp=OUT/f"probabilistic_forecast_{domain}_{hid}.json"
        fp.write_text(json.dumps(result,indent=2))
        forecast_models.append(str(fp))

    state={
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "omnioracle_probabilistic_forecast_engine_v1",
        "forecast_models_generated": len(forecast_models),
        "forecast_models": forecast_models,
        "probabilistic_mode": "deterministic_evidence_weighted_projection",
        "domains_with_insufficient_data": [d for d, r in results.items() if r["status"] == "INSUFFICIENT_DATA"],
        "live_execution_performed": False,
        "production_overwrite_performed": False,
        "status": "probabilistic_forecasts_generated"
    }

    state_path=STATE/"omnioracle_probabilistic_forecast_engine_v1_state.json"
    state_path.write_text(json.dumps(state,indent=2))
    return state, results


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    state, _ = run_all_domains(seed_override=int(_seed_env) if _seed_env else None)
    print(json.dumps(state,indent=2))
