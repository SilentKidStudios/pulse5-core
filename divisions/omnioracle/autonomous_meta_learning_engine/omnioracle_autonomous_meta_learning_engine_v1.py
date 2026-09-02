#!/usr/bin/env python3
"""Autonomous meta learning engine -- DETERMINISTIC (rewritten 2026-09-02, part of
the OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_LIVE-SIGNAL_CLOSURE campaign).

Before this rewrite: this is a LIVE daemon-timer engine (omnioracle-continuous-
daemon.timer runs it every ~15min) that counted real fusion/verification/worker/
evolution-memory file totals into `inputs`, then emitted four HARDCODED "learning
rules" with fixed confidence literals (0.88, 0.94, 0.91, 0.82) every single cycle,
independent of those counts -- fabricated precision unrelated to any real evidence.

Now: the four lesson/action statements remain as static curated content (they are
institutional design principles, not measurements), but confidence is computed from
real supporting evidence via deterministic_forecast_core, and a rule with zero
supporting evidence is reported as status=INSUFFICIENT_DATA with no fabricated
confidence."""

import json, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

FUSION=ORACLE/"autonomous_signal_fusion_engine/fused_signals"
VERIFY=ORACLE/"forecast_outcome_verifier/verifications"
RELIABILITY=ORACLE/"learning/worker_reliability/workers"
EVOLVE=ORACLE/"autonomous_strategic_evolution_loop/evolution_memory"

META=ORACLE/"autonomous_meta_learning_engine"
MEM=META/"meta_memory"
UPDATES=META/"model_updates"
STATE=META/"state"

MEM.mkdir(parents=True, exist_ok=True)
UPDATES.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

# Static curated design principles -- qualitative institutional lessons, not
# measurements. `confidence` below IS a measurement and must never be a bare literal.
LEARNING_RULE_CONCEPTS=[
    {
        "rule_id":"META_SIGNAL_DIVERSITY_001",
        "lesson":"More signal types improve forecast context.",
        "action":"prioritize connecting real runtime + approval + studio activity signals",
        "supporting_evidence_key":"fusion_files",
    },
    {
        "rule_id":"META_OUTCOME_VERIFICATION_001",
        "lesson":"Forecast reliability cannot truly improve without real outcome verification.",
        "action":"connect verifier to real future outcome events",
        "supporting_evidence_key":"verification_files",
    },
    {
        "rule_id":"META_WORKER_WEIGHTING_001",
        "lesson":"Workers should gain influence only after verified accurate predictions.",
        "action":"route high-impact forecasts through reliability-weighted consensus",
        "supporting_evidence_key":"worker_files",
    },
    {
        "rule_id":"META_RECURSIVE_DEPTH_001",
        "lesson":"Deeper scenario trees improve strategic coverage but need pruning.",
        "action":"add branch pruning and contradiction detection",
        "supporting_evidence_key":"evolution_memory_files",
    }
]


def run_meta_learning(seed_override=None):
    fusion_files=list(FUSION.glob("*.json")) if FUSION.exists() else []
    verification_files=list(VERIFY.glob("*.json")) if VERIFY.exists() else []
    worker_files=list(RELIABILITY.glob("*.json")) if RELIABILITY.exists() else []
    evolution_files=list(EVOLVE.glob("*.json")) if EVOLVE.exists() else []

    evidence_by_key = {
        "fusion_files": len(fusion_files),
        "verification_files": len(verification_files),
        "worker_files": len(worker_files),
        "evolution_memory_files": len(evolution_files),
    }
    total_evidence = sum(evidence_by_key.values())

    learning_rules=[]
    for concept in LEARNING_RULE_CONCEPTS:
        own_evidence = evidence_by_key.get(concept["supporting_evidence_key"], 0)
        base = heuristic_base_score(concept["lesson"])
        seed = seed_override if seed_override is not None else content_seed(concept["rule_id"], own_evidence)
        mc = seeded_monte_carlo(base, seed=seed, n_samples=150, spread=0.10)

        if own_evidence == 0:
            learning_rules.append({
                **concept,
                "status":"INSUFFICIENT_DATA",
                "confidence":None,
                "supporting_evidence_count":0,
                "provenance":"synthetic",
                "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
            })
            continue

        confidence = derive_confidence(mc, evidence_count=own_evidence)
        learning_rules.append({
            **concept,
            "status":"OK",
            "confidence": confidence,
            "supporting_evidence_count": own_evidence,
            "provenance":"synthetic",
            "calibration_status":"uncalibrated_no_real_outcomes_yet",
            "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
        })

    model_update={
        "generated_at":now(),
        "engine":"omnioracle_autonomous_meta_learning_engine_v1",
        "inputs":evidence_by_key,
        "total_evidence_artifacts": total_evidence,
        "learning_rules":learning_rules,
        "recommended_next_upgrades":[
            "real_outcome_event_collector",
            "branch_pruning_engine",
            "forecast_contradiction_detector",
            "reliability_weighted_worker_router",
            "continuous_oracle_daemon"
        ],
        "live_execution_performed":False,
        "production_overwrite_performed":False,
        "status":"meta_learning_completed"
    }

    digest=hashlib.sha256(
        json.dumps({k:v for k,v in model_update.items() if k!="generated_at"},sort_keys=True).encode()
    ).hexdigest()[:16]

    mem_file=MEM/f"meta_learning_memory_{digest}.json"
    mem_file.write_text(json.dumps(model_update,indent=2))

    update_file=UPDATES/f"oracle_model_update_recommendations_{digest}.json"
    update_file.write_text(json.dumps({
        "generated_at":now(),
        "source_memory":str(mem_file),
        "updates":model_update["recommended_next_upgrades"],
        "requires_founder_review_for_live_execution":True,
        "status":"model_update_recommendations_ready"
    },indent=2))

    state={
        "updated_at":now(),
        "engine":"omnioracle_autonomous_meta_learning_engine_v1",
        "meta_memory":str(mem_file),
        "model_update_file":str(update_file),
        "rules_learned":len(learning_rules),
        "status":"meta_learning_engine_ready"
    }

    (STATE/"omnioracle_autonomous_meta_learning_engine_v1_state.json").write_text(json.dumps(state,indent=2))
    return state


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    print(json.dumps(run_meta_learning(seed_override=int(_seed_env) if _seed_env else None),indent=2))
