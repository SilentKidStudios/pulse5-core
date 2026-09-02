#!/usr/bin/env python3
"""Autonomous signal fusion engine -- DETERMINISTIC (rewritten 2026-09-02, part of the
OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_LIVE-SIGNAL_CLOSURE campaign).

Before this rewrite: this is a LIVE daemon-timer engine (omnioracle-continuous-
daemon.timer runs it every ~15min) that counted real connector/forecast/risk/
opportunity/consensus file totals into `inputs`, then emitted a HARDCODED CONSTANT
signal_health="stable" and confidence_band="moderate_to_high" for every domain, every
cycle, regardless of what those counts actually were -- a fixed "everything is fine"
output masquerading as live fusion. Worse than random: it never varied at all, so a
real degradation would never show up here.

Now: signal_health/confidence_band are computed from the actual evidence volume via
the shared deterministic_forecast_core (transparent heuristic + evidence count +
seeded Monte Carlo). Zero evidence anywhere -> status=INSUFFICIENT_DATA, not a
default-happy "stable"."""

import json, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence, probability_band

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

CONNECTORS=ORACLE/"real_signal_connector/connectors"
FORECASTS=ORACLE/"probabilistic_forecast_engine/forecasts"
RISKS=ORACLE/"predictive_risk_sentinel/alerts"
OPPS=ORACLE/"predictive_opportunity_hunter/opportunities"
CONS=ORACLE/"consensus_intelligence_layer/consensus"

FUSION=ORACLE/"autonomous_signal_fusion_engine"

OUT=FUSION/"fused_signals"
MODELS=FUSION/"models"
STATE=FUSION/"state"

OUT.mkdir(parents=True, exist_ok=True)
MODELS.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

fusion_domains=[
    "runtime_stability",
    "swarm_intelligence",
    "strategic_growth",
    "predictive_accuracy",
    "cross_division_pressure"
]


def run_fusion(seed_override=None):
    connector_files=list(CONNECTORS.glob("*.json")) if CONNECTORS.exists() else []
    forecast_files=list(FORECASTS.glob("*.json")) if FORECASTS.exists() else []
    risk_files=list(RISKS.glob("*.json")) if RISKS.exists() else []
    opportunity_files=list(OPPS.glob("*.json")) if OPPS.exists() else []
    consensus_files=list(CONS.glob("*.json")) if CONS.exists() else []

    total_evidence = len(connector_files) + len(forecast_files) + len(risk_files) + len(opportunity_files) + len(consensus_files)

    fused_outputs=[]

    for domain in fusion_domains:
        base = heuristic_base_score(domain)
        seed = seed_override if seed_override is not None else content_seed(domain, total_evidence)
        mc = seeded_monte_carlo(base, seed=seed, n_samples=150, spread=0.10)

        if total_evidence == 0:
            fused={
                "generated_at":now(),
                "fusion_domain":domain,
                "status":"INSUFFICIENT_DATA",
                "connector_inputs":0, "forecast_inputs":0, "risk_inputs":0,
                "opportunity_inputs":0, "consensus_inputs":0,
                "fusion_mode":"deterministic_evidence_weighted_correlation",
                "signal_health":None,
                "confidence_band":probability_band(None),
                "recommended_next_action":"increase_signal_collection",
                "provenance":"synthetic",
                "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
                "live_execution_allowed":False
            }
        else:
            fusion_confidence = derive_confidence(mc, evidence_count=total_evidence)
            health = "stable" if fusion_confidence >= 0.6 else ("degraded" if fusion_confidence >= 0.4 else "unstable")
            fused={
                "generated_at":now(),
                "fusion_domain":domain,
                "status":"OK",
                "connector_inputs":len(connector_files),
                "forecast_inputs":len(forecast_files),
                "risk_inputs":len(risk_files),
                "opportunity_inputs":len(opportunity_files),
                "consensus_inputs":len(consensus_files),
                "fusion_mode":"deterministic_evidence_weighted_correlation",
                "fusion_confidence":fusion_confidence,
                "signal_health":health,
                "confidence_band":probability_band(fusion_confidence),
                "recommended_next_action":(
                    "increase_signal_collection"
                    if domain=="predictive_accuracy" or fusion_confidence < 0.5
                    else "continue_monitoring"
                ),
                "provenance":"synthetic",
                "calibration_status":"uncalibrated_no_real_outcomes_yet",
                "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
                "live_execution_allowed":False
            }

        hid=hashlib.sha256(
            json.dumps({k:v for k,v in fused.items() if k!="generated_at"},sort_keys=True).encode()
        ).hexdigest()[:16]

        fp=OUT/f"fused_signal_{domain}_{hid}.json"
        fp.write_text(json.dumps(fused,indent=2))

        fused_outputs.append(str(fp))

    model_state={
        "updated_at":now(),
        "engine":"omnioracle_autonomous_signal_fusion_engine_v1",
        "fusion_outputs_generated":len(fused_outputs),
        "fusion_outputs":fused_outputs,
        "fusion_strategy":"cross_source_signal_unification",
        "total_evidence_artifacts":total_evidence,
        "real_time_fusion_active":False,
        "live_execution_performed":False,
        "production_overwrite_performed":False,
        "status":"signal_fusion_engine_ready"
    }

    model_file=MODELS/"fusion_model_state_v1.json"
    model_file.write_text(json.dumps(model_state,indent=2))

    state_file=STATE/"omnioracle_autonomous_signal_fusion_engine_v1_state.json"
    state_file.write_text(json.dumps(model_state,indent=2))
    return model_state


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    print(json.dumps(run_fusion(seed_override=int(_seed_env) if _seed_env else None),indent=2))
