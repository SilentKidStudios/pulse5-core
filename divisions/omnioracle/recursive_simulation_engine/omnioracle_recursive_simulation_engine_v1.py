#!/usr/bin/env python3
"""Recursive simulation engine -- DETERMINISTIC (rewritten 2026-09-02, see
divisions/omnioracle/api/deterministic_forecast_core.py). Branch confidence at each
recursion depth is a deterministic function of the parent objective's heuristic score
plus depth-based uncertainty growth (deeper recursion = wider seeded Monte Carlo
spread, not arbitrary random.choice). future_outcome/risk_level are derived from the
resulting confidence band against real label vocabularies, not drawn independently
at random from an unrelated list."""

import json
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence, derive_risk

ROOT=Path("/opt/pulse5-core")

ORACLE=ROOT/"divisions/omnioracle"

SIM=ORACLE/"recursive_simulation_engine"
SCEN=ORACLE/"scenario_branch_generator/branches"
FORECAST=ORACLE/"forecast_ledger/entries"

RUNS=SIM/"runs"
STATE=SIM/"state"
BRANCHES=SIM/"branches"

RUNS.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
BRANCHES.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

OUTCOME_BY_BAND = {
    "high": "stable_growth",
    "medium": "controlled_risk",
    "low": "worker_divergence",
}
RISK_BY_BAND = {
    "high": "low",
    "medium": "medium_controlled",
    "low": "medium",
}


def _band(confidence):
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.55:
        return "medium"
    return "low"


def make_branch(parent_name, depth, parent_text, seed_override=None):
    # Deeper recursion carries genuinely more uncertainty -- modeled as a wider
    # Monte Carlo spread with depth, not an unrelated random.choice.
    base = heuristic_base_score(f"{parent_text} depth_{depth}")
    seed = seed_override if seed_override is not None else content_seed(parent_name, depth)
    spread = min(0.30, 0.10 + depth * 0.04)
    mc = seeded_monte_carlo(base, seed=seed, n_samples=150, spread=spread)
    confidence = derive_confidence(mc, evidence_count=1)
    band = _band(confidence)

    return {
        "branch_id": hashlib.sha256(f"{parent_name}_{depth}_{confidence}".encode()).hexdigest()[:16],
        "parent": parent_name,
        "depth": depth,
        "confidence": confidence,
        "future_outcome": OUTCOME_BY_BAND[band],
        "risk_level": RISK_BY_BAND[band],
        "reproducibility": {"deterministic": True, "seed": mc.seed, "n_samples": mc.n_samples, "spread": spread},
        "provenance": "synthetic",
    }


def run_recursive_simulation(seed_override=None):
    all_recursive=[]

    scenario_files=list(SCEN.glob("*.json"))
    forecast_files=list(FORECAST.glob("*.json"))

    for f in scenario_files:
        try:
            raw=json.loads(f.read_text())
        except Exception:
            continue

        branches = raw if isinstance(raw,list) else [raw]
        objective=f.stem
        recursive_tree=[]

        for depth in range(1,5):
            branch_count=min(depth+1,5)
            for idx in range(branch_count):
                parent_text = f"{objective}_{idx}"
                recursive_tree.append(make_branch(objective, depth, parent_text, seed_override))

        tree_doc={
            "generated_at": now(),
            "objective": objective,
            "source_file": str(f),
            "recursive_branch_count": len(recursive_tree),
            "max_depth": 4,
            "simulation_mode": "deterministic_heuristic_plus_seeded_monte_carlo",
            "branches": recursive_tree
        }

        digest=hashlib.sha256(
            json.dumps({k: v for k, v in tree_doc.items() if k != "generated_at"}, sort_keys=True).encode()
        ).hexdigest()[:16]

        out=BRANCHES/f"recursive_simulation_{objective}_{digest}.json"
        out.write_text(json.dumps(tree_doc,indent=2))
        all_recursive.append(str(out))

    summary={
        "updated_at": now(),
        "engine": "omnioracle_recursive_simulation_engine_v1",
        "scenario_files_processed": len(scenario_files),
        "forecast_files_available": len(forecast_files),
        "recursive_simulation_sets": len(all_recursive),
        "simulation_outputs": all_recursive,
        "simulation_depth": 4,
        "live_execution_performed": False,
        "production_overwrite_performed": False,
        "status": "recursive_simulation_completed"
    }

    state_file=STATE/"omnioracle_recursive_simulation_engine_v1_state.json"
    state_file.write_text(json.dumps(summary,indent=2))
    return summary


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    print(json.dumps(run_recursive_simulation(seed_override=int(_seed_env) if _seed_env else None),indent=2))
