"""Part D integration test: forecast created -> immutable forecast identity ->
later real ground-truth outcome -> calibration comparison -> accuracy/error metrics
-> future calibration state. Uses isolated tmp_path directories throughout so this
test never writes into the real forecast_ledger or calibration_state.json."""
import sys
import json
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "api"
PFE_DIR = Path(__file__).resolve().parent.parent / "probabilistic_forecast_engine"
VERIFIER_DIR = Path(__file__).resolve().parent.parent / "forecast_outcome_verifier"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(VERIFIER_DIR))

import importlib.util


def _import_pfe():
    spec = importlib.util.spec_from_file_location(
        "pfe_calib_e2e", PFE_DIR / "omnioracle_probabilistic_forecast_engine_v1.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_full_forecast_to_calibration_loop(tmp_path, monkeypatch):
    import calibration

    pfe = _import_pfe()

    isolated_ledger = tmp_path / "entries"
    isolated_ledger.mkdir()
    isolated_calibration_state = tmp_path / "calibration_state_v1.json"

    monkeypatch.setattr(pfe, "LEDGER", isolated_ledger)
    monkeypatch.setattr(calibration, "LEDGER_DIR", isolated_ledger)
    monkeypatch.setattr(calibration, "CALIBRATION_STATE", isolated_calibration_state)

    # Step 1: forecast created, with real (if sparse) evidence via a fixed seed.
    forecast = pfe.forecast_domain("runtime_autonomy", seed_override=2026, min_evidence=0)
    assert forecast["status"] == "OK"
    fid = forecast["forecast_id"]
    assert fid.startswith("FCID_")

    # Step 2: immutable forecast identity -- a ledger entry now exists for this exact
    # forecast, distinct from the 3 hardcoded seed predictions.
    ledger_files = list(isolated_ledger.glob(f"{fid}_*.json"))
    assert len(ledger_files) == 1
    ledger_entry = json.loads(ledger_files[0].read_text())
    assert ledger_entry["prediction_id"] == fid
    assert ledger_entry["outcome_verified"] is False
    assert ledger_entry["confidence"] == forecast["confidence"]

    # Step 3: later, a real ground-truth outcome is recorded against that exact
    # forecast identity (not fabricated -- this is the mechanism test, the "real"
    # outcome here is a test-supplied fixture, and calibration_status must stay
    # honest about that -- see assertions below).
    updated_entry = calibration.record_real_outcome(
        fid, actual_outcome="test-fixture: recommended action was followed and succeeded",
        correct=True, notes="Part D pipeline integration test",
    )

    # Step 4: calibration comparison + accuracy/error metric recorded.
    assert updated_entry["outcome_verified"] is True
    assert updated_entry["accuracy_score"] == 1.0
    assert updated_entry["prediction_id"] == fid

    # Step 5: future calibration state reflects it.
    state = calibration.read_calibration_state()
    assert state["real_outcome_count"] == 1
    assert state["real_accuracy_rate"] == 1.0
    # Still honestly not "calibrated" off one data point.
    assert state["calibration_status"] == "insufficient_real_outcomes_for_calibration"


def test_two_forecasts_get_distinct_immutable_identities(tmp_path, monkeypatch):
    pfe = _import_pfe()
    isolated_ledger = tmp_path / "entries"
    isolated_ledger.mkdir()
    monkeypatch.setattr(pfe, "LEDGER", isolated_ledger)

    f1 = pfe.forecast_domain("runtime_autonomy", seed_override=1, min_evidence=0)
    f2 = pfe.forecast_domain("runtime_autonomy", seed_override=2, min_evidence=0)
    assert f1["forecast_id"] != f2["forecast_id"]

    ledger_files = list(isolated_ledger.glob("FCID_*.json"))
    assert len(ledger_files) == 2
