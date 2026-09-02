#!/usr/bin/env python3
"""Scenario branch generator -- DETERMINISTIC (rewritten 2026-09-02, see
divisions/omnioracle/api/deterministic_forecast_core.py). No unseeded randomness:
each branch's confidence is a heuristic score over its own real objective/outcome
text, refined by explicit seeded Monte Carlo dispersion. Default seed is derived
from the branch's own content (content_seed), so re-running with the same
objectives/outcomes always reproduces identical output unless a caller passes an
explicit seed to deliberately explore an alternate ensemble."""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence, derive_risk

ROOT=Path("/opt/pulse5-core")
SCEN=ROOT/"divisions/omnioracle/scenario_branch_generator"
BRANCHES=SCEN/"branches"
STATE=SCEN/"state"
RECEIPTS=SCEN/"receipts"

BRANCHES.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
RECEIPTS.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

strategic_targets=[
 {
   "objective":"runtime_autonomy",
   "possible_outcomes":[
      "stable_growth",
      "partial_instability",
      "cross_division_failure",
      "self_healing_success"
   ]
 },
 {
   "objective":"omnioracle_prediction_accuracy",
   "possible_outcomes":[
      "accuracy_improves",
      "signal_noise_growth",
      "worker_specialization_success",
      "forecast_conflict_detected"
   ]
 },
 {
   "objective":"swarm_coordination",
   "possible_outcomes":[
      "worker_scaling_success",
      "queue_bottleneck",
      "resource_contention",
      "successful_parallel_repairs"
   ]
 }
]


def generate_branches(targets=None, seed_override=None):
    """Pure-ish function (still writes artifacts to disk, matching this pipeline's
    existing convention) so tests can call it directly and assert on the return
    value. seed_override: if given, used for every branch instead of each branch's
    own content-derived seed -- lets a caller deliberately explore an alternate
    seeded ensemble while staying fully reproducible."""
    targets = targets if targets is not None else strategic_targets
    generated=[]
    all_branch_sets=[]

    for target in targets:
        branch_set=[]
        for idx, outcome in enumerate(target["possible_outcomes"], start=1):
            text = f"{target['objective']} {outcome}"
            base_score = heuristic_base_score(text)
            seed = seed_override if seed_override is not None else content_seed(target["objective"], outcome)
            mc = seeded_monte_carlo(base_score, seed=seed, n_samples=200, spread=0.12)
            confidence = derive_confidence(mc, evidence_count=1)  # each branch is its own single evidence unit
            risk = derive_risk(mc)

            branch={
              "branch_id":f"{target['objective']}_BRANCH_{idx}",
              "objective":target["objective"],
              "simulated_outcome":outcome,
              "confidence":confidence,
              "risk_score":risk,
              "risk_level":"medium" if confidence < 0.7 else "low",
              "recommended_response":
                 "monitor_and_learn"
                 if confidence < 0.75
                 else "prepare_action_packet",
              "base_heuristic_score": base_score,
              "reproducibility": {"deterministic": True, "seed": mc.seed, "n_samples": mc.n_samples},
              "provenance": "synthetic",
              "generated_at":now()
            }

            branch_set.append(branch)

        digest_source = json.dumps([{k: v for k, v in b.items() if k != "generated_at"} for b in branch_set], sort_keys=True)
        import hashlib
        digest=hashlib.sha256(digest_source.encode()).hexdigest()[:16]

        out=BRANCHES/f"{target['objective']}_{digest}.json"
        out.write_text(json.dumps(branch_set,indent=2))
        generated.append(str(out))
        all_branch_sets.append(branch_set)

    state={
      "updated_at":now(),
      "engine":"omnioracle_scenario_branch_generator_v1",
      "scenario_sets_generated":len(generated),
      "branch_files":generated,
      "simulation_mode":"deterministic_heuristic_plus_seeded_monte_carlo",
      "live_execution_performed":False,
      "production_overwrite_performed":False,
      "status":"scenario_branches_generated"
    }

    import hashlib
    receipt_digest=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()[:16]
    receipt=RECEIPTS/f"scenario_branch_generator_v1_{receipt_digest}.json"
    receipt.write_text(json.dumps(state,indent=2))
    (STATE/"omnioracle_scenario_branch_generator_v1_state.json").write_text(json.dumps(state,indent=2))

    return state, all_branch_sets


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    state, _ = generate_branches(seed_override=int(_seed_env) if _seed_env else None)
    print(json.dumps(state,indent=2))
