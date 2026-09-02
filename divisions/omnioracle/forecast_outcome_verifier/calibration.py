"""Calibration hook for forecast_outcome_verifier -- lets a REAL observed outcome be
recorded against a forecast_ledger prediction, and rolls it into an aggregate
calibration_state.json distinct from the proxy_internal verification records that
omnioracle_forecast_outcome_verifier_v1.py already writes.

CRITERION_17 NOTE (OMNI_ORACLE_REAL-FORECAST_ENGINE_CONVERGENCE campaign, 2026-09-02)
----------------------------------------------------------------------------------------
This file does NOT close criterion 17 (BACKTEST_OR_HISTORICAL_VALIDATION). It builds
the mechanism so real outcomes CAN be recorded and accumulated honestly over time,
without requiring a pipeline redesign later. As of this writing zero real outcomes
have been recorded -- calibration_state.json's real_outcome_count is 0 until a human
(or an authorized process with real ground truth) actually calls record_real_outcome()
with a genuine observed result. This module fabricates nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/opt/pulse5-core")
ORACLE = ROOT / "divisions/omnioracle"
LEDGER_DIR = ORACLE / "forecast_ledger/entries"
VERIFIER_DIR = ORACLE / "forecast_outcome_verifier"
CALIBRATION_STATE = VERIFIER_DIR / "state" / "calibration_state_v1.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


class UnknownPredictionError(ValueError):
    pass


def _find_ledger_file(prediction_id: str) -> Path:
    if not LEDGER_DIR.exists():
        raise UnknownPredictionError(f"forecast_ledger entries directory does not exist: {LEDGER_DIR}")
    matches = [f for f in LEDGER_DIR.glob(f"{prediction_id}_*.json")]
    if not matches:
        raise UnknownPredictionError(f"no forecast_ledger entry found for prediction_id={prediction_id!r}")
    return matches[0]


def record_real_outcome(prediction_id: str, *, actual_outcome: str, correct: bool,
                         observed_at: str | None = None, notes: str | None = None,
                         accuracy_score: float | None = None) -> dict:
    """Record a genuine, human/process-confirmed real-world outcome against a
    forecast_ledger prediction. Raises UnknownPredictionError if prediction_id
    doesn't exist -- never silently invents a matching record.

    accuracy_score: if not given, defaults to 1.0 if correct else 0.0 (simple
    binary scoring). Callers with a probabilistic prediction may supply a proper
    Brier-style score instead.
    """
    ledger_file = _find_ledger_file(prediction_id)
    entry = json.loads(ledger_file.read_text())

    score = accuracy_score if accuracy_score is not None else (1.0 if correct else 0.0)
    entry["outcome_verified"] = True
    entry["accuracy_score"] = round(float(score), 4)
    entry.setdefault("learning_feedback", []).append({
        "recorded_at": observed_at or _now(),
        "actual_outcome": actual_outcome,
        "correct": bool(correct),
        "accuracy_score": round(float(score), 4),
        "notes": notes,
        "source": "calibration.record_real_outcome (real, not proxy)",
    })
    ledger_file.write_text(json.dumps(entry, indent=2))

    _update_calibration_state(entry)
    return entry


def _update_calibration_state(entry: dict) -> dict:
    CALIBRATION_STATE.parent.mkdir(parents=True, exist_ok=True)
    if CALIBRATION_STATE.exists():
        try:
            state = json.loads(CALIBRATION_STATE.read_text())
        except Exception:
            state = _empty_calibration_state()
    else:
        state = _empty_calibration_state()

    state["real_outcome_count"] += 1
    state["accuracy_scores"].append(entry["accuracy_score"])
    state["real_accuracy_rate"] = round(sum(state["accuracy_scores"]) / len(state["accuracy_scores"]), 4)
    state["updated_at"] = _now()
    state["calibration_status"] = (
        "insufficient_real_outcomes_for_calibration" if state["real_outcome_count"] < 30
        else "provisionally_calibrated_see_real_accuracy_rate"
    )

    CALIBRATION_STATE.write_text(json.dumps(state, indent=2))
    return state


def _empty_calibration_state() -> dict:
    return {
        "engine": "forecast_outcome_verifier.calibration_v1",
        "real_outcome_count": 0,
        "accuracy_scores": [],
        "real_accuracy_rate": None,
        "calibration_status": "insufficient_real_outcomes_for_calibration",
        "note": "Distinct from proxy_internal verification records in forecast_outcome_verifier_v1_state.json. "
                "Populated only by genuine calls to record_real_outcome() with real observed results -- never "
                "auto-filled or estimated.",
        "updated_at": _now(),
    }


def read_calibration_state() -> dict:
    if not CALIBRATION_STATE.exists():
        return _empty_calibration_state()
    return json.loads(CALIBRATION_STATE.read_text())
