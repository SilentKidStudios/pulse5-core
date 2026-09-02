#!/usr/bin/env python3
"""Consensus intelligence layer -- DETERMINISTIC aggregation (no randomness ever used
here). Updated 2026-09-02 to compute risk/uncertainty from the REAL dispersion of the
branch confidences it already reads, via the shared deterministic_forecast_core (a
genuine ensemble-statistics aggregate, not sampled noise), and to expose the
canonical forecast schema fields (see deterministic_forecast_core.canonical_forecast_result).

Refactored into compute_consensus_for_branches()/run_consensus() (was a top-level
script that executed on import) so tests can call it directly with controlled branch
sets rather than only via subprocess."""

import json
import hashlib
import sys
import statistics
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import MonteCarloResult, derive_risk, probability_band

ROOT=Path("/opt/pulse5-core")

ORACLE=ROOT/"divisions/omnioracle"

CONS=ORACLE/"consensus_intelligence_layer"
SIM=ORACLE/"recursive_simulation_engine/branches"

STATE=CONS/"state"
OUT=CONS/"consensus"


def now():
    return datetime.now(timezone.utc).isoformat()


def compute_consensus_for_branches(branches, objective="unknown", source_label="unknown"):
    """Pure(ish) computation over an explicit branch list -- no disk I/O. Deterministic:
    same branches -> identical consensus dict (excluding generated_at)."""
    if not branches:
        return None

    outcomes=[b.get("future_outcome","unknown") for b in branches]
    risks=[b.get("risk_level","unknown") for b in branches]
    confidences=[float(b.get("confidence",0.5)) for b in branches]

    outcome_counter = Counter(outcomes)
    outcome_vote=outcome_counter.most_common(1)[0][0]
    risk_vote=Counter(risks).most_common(1)[0][0]

    avg_conf=round(sum(confidences)/max(len(confidences),1),4)
    # Real ensemble dispersion across actual branch confidences -- not sampled noise.
    conf_stdev = round(statistics.pstdev(confidences), 4) if len(confidences) > 1 else 0.0
    agreement_fraction = round(outcome_counter.most_common(1)[0][1] / max(len(outcomes), 1), 4)
    real_mc = MonteCarloResult(seed="n/a_real_ensemble_not_sampled", n_samples=len(confidences),
                                mean=avg_conf, stdev=conf_stdev, agreement_fraction=agreement_fraction,
                                min_sample=round(min(confidences), 4), max_sample=round(max(confidences), 4))
    # More distinct outcomes among branches = more contradiction among evidence sources.
    contradiction_flags = max(0, len(set(outcomes)) - 1)
    consensus_risk_score = derive_risk(real_mc, contradiction_flags=contradiction_flags)

    confidence_band=(
        "high"
        if avg_conf >= 0.80 else
        "medium"
        if avg_conf >= 0.60 else
        "low"
    )

    consensus={
        "generated_at": now(),
        "source_simulation": source_label,
        "objective": objective,
        "branch_count": len(branches),
        "consensus_outcome": outcome_vote,
        "consensus_risk": risk_vote,
        "consensus_risk_score": consensus_risk_score,
        "average_confidence": avg_conf,
        "confidence_stdev": conf_stdev,
        "agreement_fraction": agreement_fraction,
        "contradiction_flags": contradiction_flags,
        "confidence_band": confidence_band,
        "probability_band": probability_band(avg_conf),
        "strategic_recommendation": [],
        "requires_founder_review": True,
        "live_execution_allowed": False,
        "provenance": "synthetic",
        "calibration_status": "uncalibrated_no_real_outcomes_yet",
        "reproducibility": {"deterministic": True, "note": "real ensemble aggregate over already-deterministic branch inputs; no sampling performed here"},
    }

    lower=outcome_vote.lower()

    if "growth" in lower:
        consensus["strategic_recommendation"]=[
            "expand validation scope",
            "increase controlled experimentation",
            "prepare staged promotion candidates"
        ]

    elif "risk" in lower:
        consensus["strategic_recommendation"]=[
            "increase monitoring density",
            "tighten rollback enforcement",
            "expand prevention learning"
        ]

    elif "divergence" in lower:
        consensus["strategic_recommendation"]=[
            "rebalance swarm workers",
            "increase consensus weighting",
            "inspect prediction conflicts"
        ]

    else:
        consensus["strategic_recommendation"]=[
            "collect more signals",
            "expand recursive simulation depth",
            "increase reliability verification"
        ]

    return consensus


def run_consensus():
    STATE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    consensus_outputs=[]

    for sim_file in sorted(SIM.glob("*.json")):
        try:
            data=json.loads(sim_file.read_text())
        except Exception:
            continue

        branches=data.get("branches",[])
        consensus = compute_consensus_for_branches(branches, objective=data.get("objective","unknown"), source_label=str(sim_file))
        if consensus is None:
            continue

        digest=hashlib.sha256(
            json.dumps({k: v for k, v in consensus.items() if k != "generated_at"},sort_keys=True).encode()
        ).hexdigest()[:16]

        out=OUT/f"consensus_{digest}.json"
        out.write_text(json.dumps(consensus,indent=2))

        consensus_outputs.append(str(out))

    summary={
        "updated_at": now(),
        "engine": "omnioracle_consensus_intelligence_layer_v1",
        "simulations_processed": len(list(SIM.glob('*.json'))),
        "consensus_outputs_generated": len(consensus_outputs),
        "consensus_outputs": consensus_outputs,
        "collective_reasoning_mode": "weighted_swarm_consensus",
        "live_execution_performed": False,
        "production_overwrite_performed": False,
        "status": "consensus_intelligence_completed"
    }

    state_file=STATE/"omnioracle_consensus_intelligence_layer_v1_state.json"
    state_file.write_text(json.dumps(summary,indent=2))
    return summary


if __name__ == "__main__":
    print(json.dumps(run_consensus(),indent=2))
