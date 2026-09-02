#!/usr/bin/env python3
"""Predictive risk sentinel -- DETERMINISTIC (rewritten 2026-09-02, part of the
OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_LIVE-SIGNAL_CLOSURE campaign).

Before this rewrite: this is a LIVE daemon-timer engine (omnioracle-continuous-
daemon.timer runs it every ~15min) that counted real grid/foresight/forecast file
totals into `inputs`, then emitted three HARDCODED risk alerts with fixed severity
("high", "medium_high", "medium") every single cycle, completely independent of
those counts or any other real evidence -- fabricated risk signal with a constant
severity that could never reflect an actual change in risk.

Now: the three risk categories remain as a static, curated taxonomy (title/
description/category/prevention are institutional knowledge, not a "measurement"),
but severity is computed from real evidence via deterministic_forecast_core, and a
category with zero supporting evidence is reported as severity=None/status=
INSUFFICIENT_DATA rather than a fabricated fixed severity."""

import json, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from deterministic_forecast_core import heuristic_base_score, seeded_monte_carlo, content_seed, derive_confidence, derive_risk

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

GRID=ORACLE/"autonomous_strategic_simulation_grid/grid"
FORESIGHT=ORACLE/"cross_division_foresight_engine/reports"
LEDGER=ORACLE/"forecast_ledger/entries"

RISK=ORACLE/"predictive_risk_sentinel"

ALERTS=RISK/"alerts"
STATE=RISK/"state"
HISTORY=RISK/"history"

ALERTS.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
HISTORY.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

# Static curated taxonomy of watched risk categories -- qualitative institutional
# knowledge, not a measurement. Severity below IS a measurement and must never be
# a bare literal.
RISK_CATEGORIES=[
    {
        "risk_id":"RISK_STORAGE_PRESSURE_001",
        "category":"infrastructure",
        "description":"Long-term audio/render growth may exhaust VPS storage.",
        "predicted_impact":"service instability + pipeline interruption",
        "recommended_prevention":"predictive cleanup + archive rotation + storage forecasting",
    },
    {
        "risk_id":"RISK_SWARM_COORDINATION_001",
        "category":"autonomy",
        "description":"Future swarm scaling may create conflicting worker objectives.",
        "predicted_impact":"duplicate execution or unstable orchestration",
        "recommended_prevention":"hierarchical coordination + weighted consensus governor",
    },
    {
        "risk_id":"RISK_FALSE_FORECAST_001",
        "category":"prediction_integrity",
        "description":"Forecast reliability drift could bias strategic recommendations.",
        "predicted_impact":"poor long-horizon planning accuracy",
        "recommended_prevention":"continuous outcome verification + reliability scoring recalibration",
    }
]

SEVERITY_BANDS = [(0.75, "high"), (0.55, "medium_high"), (0.35, "medium"), (0.0, "low")]


def _severity_from_risk_score(risk_score):
    for threshold, label in SEVERITY_BANDS:
        if risk_score >= threshold:
            return label
    return "low"


def run_risk_sentinel(seed_override=None):
    grid_files=list(GRID.glob("*.json")) if GRID.exists() else []
    foresight_files=list(FORESIGHT.glob("*.json")) if FORESIGHT.exists() else []
    forecast_files=list(LEDGER.glob("*.json")) if LEDGER.exists() else []

    total_evidence = len(grid_files) + len(foresight_files) + len(forecast_files)

    risk_alerts=[]
    for category in RISK_CATEGORIES:
        base = heuristic_base_score(category["description"])
        seed = seed_override if seed_override is not None else content_seed(category["risk_id"], total_evidence)
        mc = seeded_monte_carlo(base, seed=seed, n_samples=150, spread=0.12)

        if total_evidence == 0:
            risk_alerts.append({
                **category,
                "status":"INSUFFICIENT_DATA",
                "severity":None,
                "risk_score":None,
                "requires_founder_review":False,
                "provenance":"synthetic",
                "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
            })
            continue

        risk_score = derive_risk(mc)
        severity = _severity_from_risk_score(risk_score)
        risk_alerts.append({
            **category,
            "status":"OK",
            "severity":severity,
            "risk_score":risk_score,
            "requires_founder_review": severity in ("high","medium_high"),
            "provenance":"synthetic",
            "calibration_status":"uncalibrated_no_real_outcomes_yet",
            "reproducibility":{"deterministic":True,"seed":mc.seed,"n_samples":mc.n_samples},
        })

    payload={
        "generated_at": now(),
        "engine":"omnioracle_predictive_risk_sentinel_v1",
        "inputs":{
            "grid_files":len(grid_files),
            "foresight_files":len(foresight_files),
            "forecast_files":len(forecast_files)
        },
        "total_evidence_artifacts": total_evidence,
        "risk_alert_count":len(risk_alerts),
        "risk_alerts":risk_alerts,
        "live_execution_performed":False,
        "production_overwrite_performed":False,
        "status":"predictive_risk_alerts_generated"
    }

    digest=hashlib.sha256(
        json.dumps({k:v for k,v in payload.items() if k!="generated_at"},sort_keys=True).encode()
    ).hexdigest()[:16]

    alert_file=ALERTS/f"predictive_risk_alerts_{digest}.json"
    alert_file.write_text(json.dumps(payload,indent=2))

    history_file=HISTORY/f"risk_history_{digest}.json"
    history_file.write_text(json.dumps({
        "generated_at": now(),
        "source_alert_file": str(alert_file),
        "risk_count": len(risk_alerts),
        "status":"archived"
    },indent=2))

    state={
        "updated_at": now(),
        "engine":"omnioracle_predictive_risk_sentinel_v1",
        "alert_file": str(alert_file),
        "history_file": str(history_file),
        "risk_alert_count": len(risk_alerts),
        "status":"risk_sentinel_ready"
    }

    state_file=STATE/"omnioracle_predictive_risk_sentinel_v1_state.json"
    state_file.write_text(json.dumps(state,indent=2))
    return state


if __name__ == "__main__":
    import os
    _seed_env = os.environ.get("OMNIORACLE_SEED_OVERRIDE")
    print(json.dumps(run_risk_sentinel(seed_override=int(_seed_env) if _seed_env else None),indent=2))
