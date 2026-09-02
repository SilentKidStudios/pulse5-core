#!/usr/bin/env python3
"""Predictive opportunity hunter -- DETERMINISTIC (rewritten 2026-09-02, part of the
OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_LIVE-SIGNAL_CLOSURE campaign).

Before this rewrite: this is a LIVE daemon-timer engine (omnioracle-continuous-
daemon.timer runs it every ~15min) that collected real grid/consensus/recommendation
file lists into `inputs`, then emitted three HARDCODED "opportunities" with fixed
opportunity_score/risk_score literals (0.86/0.32, 0.91/0.25, 0.88/0.40) every single
cycle, completely independent of those inputs -- fabricated precision with no
relationship to any real evidence.

Now: the three opportunity concepts remain as static curated content (title/source/
recommended_next -- these are institutional ideas, not measurements), but
opportunity_score/risk_score are computed from real evidence via
deterministic_forecast_core, and an opportunity with zero supporting evidence is
reported as status=INSUFFICIENT_DATA with no fabricated score."""

import json, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence, derive_risk

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

GRID=ORACLE/"autonomous_strategic_simulation_grid/grid"
CONS=ORACLE/"consensus_intelligence_layer/consensus"
RECS=ORACLE/"recommendation_bridge/recommendations"
HUNTER=ORACLE/"predictive_opportunity_hunter"

OUT=HUNTER/"opportunities"
STATE=HUNTER/"state"
OUTBOX=HUNTER/"outbox"

OUT.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
OUTBOX.mkdir(parents=True, exist_ok=True)

def now(): return datetime.now(timezone.utc).isoformat()

# Static curated concepts -- qualitative institutional ideas, not measurements.
# opportunity_score/risk_score below ARE measurements and must never be bare literals.
OPPORTUNITY_CONCEPTS = [
    {
        "opportunity_id": "OPP_RUNTIME_MARKET_001",
        "title": "Turn runtime safety stack into reusable studio infrastructure product",
        "source": "container_runtime_execution + monitor/heal loop",
        "recommended_next": "Package as internal OmniForge reusable module before any external product idea.",
    },
    {
        "opportunity_id": "OPP_ORACLE_SIGNAL_001",
        "title": "Use OmniOracle to predict storage, queue, and service failures before they happen",
        "source": "prevention learning + cross-division foresight",
        "recommended_next": "Connect storage guard, service health, and approval backlog signals into Oracle intake.",
    },
    {
        "opportunity_id": "OPP_SWARM_BUILD_001",
        "title": "Create 100-worker MiroFish-style swarm using reliability-weighted specialist workers",
        "source": "worker reliability + consensus intelligence",
        "recommended_next": "Build worker spawning simulator before real worker scaling.",
    }
]


def run_opportunity_hunter(seed_override=None):
    grid_files = sorted(GRID.glob("*.json")) if GRID.exists() else []
    consensus_files = sorted(CONS.glob("*.json")) if CONS.exists() else []
    recommendation_files = sorted(RECS.glob("*.json")) if RECS.exists() else []

    inputs = {
        "grid_files": [str(p) for p in grid_files],
        "consensus_files": [str(p) for p in consensus_files],
        "recommendation_files": [str(p) for p in recommendation_files],
    }
    total_evidence = len(grid_files) + len(consensus_files) + len(recommendation_files)

    opportunities=[]
    for concept in OPPORTUNITY_CONCEPTS:
        base = heuristic_base_score(concept["title"])
        seed = seed_override if seed_override is not None else content_seed(concept["opportunity_id"], total_evidence)
        mc = seeded_monte_carlo(base, seed=seed, n_samples=150, spread=0.10)

        if total_evidence == 0:
            opportunities.append({
                **concept,
                "status":"INSUFFICIENT_DATA",
                "opportunity_score":None,
                "risk_score":None,
                "requires_founder_review":True,
                "provenance":"synthetic",
                "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
            })
            continue

        opportunity_score = derive_confidence(mc, evidence_count=total_evidence)
        risk_score = derive_risk(mc)
        opportunities.append({
            **concept,
            "status":"OK",
            "opportunity_score": opportunity_score,
            "risk_score": risk_score,
            "requires_founder_review": opportunity_score < 0.6 or risk_score > 0.4,
            "provenance":"synthetic",
            "calibration_status":"uncalibrated_no_real_outcomes_yet",
            "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
        })

    state = {
        "updated_at": now(),
        "engine": "omnioracle_predictive_opportunity_hunter_v1",
        "inputs": inputs,
        "total_evidence_artifacts": total_evidence,
        "opportunity_count": len(opportunities),
        "opportunities": opportunities,
        "live_execution_performed": False,
        "production_overwrite_performed": False,
        "status": "predictive_opportunities_generated"
    }

    digest=hashlib.sha256(
        json.dumps({k:v for k,v in state.items() if k!="updated_at"},sort_keys=True).encode()
    ).hexdigest()[:16]

    opp_file=OUT/f"predictive_opportunities_{digest}.json"
    opp_file.write_text(json.dumps(state,indent=2))

    outbox_file=OUTBOX/f"opportunity_outbox_{digest}.json"
    outbox_file.write_text(json.dumps({
        "queued_at": now(),
        "opportunity_file": str(opp_file),
        "status": "queued_for_mrsilent_review",
        "live_execution_allowed": False
    },indent=2))

    state_file=STATE/"omnioracle_predictive_opportunity_hunter_v1_state.json"
    state_file.write_text(json.dumps(state,indent=2))

    return {
        "updated_at": state["updated_at"],
        "engine": state["engine"],
        "opportunity_count": state["opportunity_count"],
        "opportunity_file": str(opp_file),
        "outbox_file": str(outbox_file),
        "status": state["status"]
    }


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    print(json.dumps(run_opportunity_hunter(seed_override=int(_seed_env) if _seed_env else None),indent=2))
