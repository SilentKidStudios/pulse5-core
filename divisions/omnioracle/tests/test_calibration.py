"""Tests for forecast_outcome_verifier/calibration.py -- the real-outcome recording
hook (campaign requirement 7). Does not fabricate any real outcome; runs entirely
against an isolated tmp_path ledger, never against the real
forecast_ledger/entries/ files, so a test run can never mark a real seed prediction
as outcome_verified."""
import sys
import json
from pathlib import Path

VERIFIER_DIR = Path(__file__).resolve().parent.parent / "forecast_outcome_verifier"
sys.path.insert(0, str(VERIFIER_DIR))

import pytest
import calibration


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "entries"
    ledger_dir.mkdir()
    state_path = tmp_path / "calibration_state_v1.json"
    monkeypatch.setattr(calibration, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(calibration, "CALIBRATION_STATE", state_path)

    entry = {
        "created_at": "2026-09-02T00:00:00+00:00",
        "prediction_id": "PRED_TEST_ONLY_001",
        "domain": "test_domain",
        "prediction": "unit-test-only prediction, never real",
        "confidence": 0.7,
        "status": "tracking",
        "outcome_verified": False,
        "accuracy_score": None,
        "learning_feedback": [],
    }
    (ledger_dir / "PRED_TEST_ONLY_001_deadbeef.json").write_text(json.dumps(entry, indent=2))
    return ledger_dir, state_path


def test_unknown_prediction_id_rejected(isolated_ledger):
    with pytest.raises(calibration.UnknownPredictionError):
        calibration.record_real_outcome(
            "PRED_DOES_NOT_EXIST_XYZ", actual_outcome="n/a", correct=True,
        )


def test_record_real_outcome_updates_ledger_entry_and_calibration_state(isolated_ledger):
    before_state = calibration.read_calibration_state()
    assert before_state["real_outcome_count"] == 0

    entry = calibration.record_real_outcome(
        "PRED_TEST_ONLY_001",
        actual_outcome="isolated test-only confirmation",
        correct=True,
        notes="unit test exercising the mechanism only, isolated ledger",
    )

    assert entry["outcome_verified"] is True
    assert entry["accuracy_score"] == 1.0
    assert entry["learning_feedback"][-1]["source"] == "calibration.record_real_outcome (real, not proxy)"

    after_state = calibration.read_calibration_state()
    assert after_state["real_outcome_count"] == 1
    assert after_state["real_accuracy_rate"] == 1.0


def test_incorrect_outcome_scores_zero_by_default(isolated_ledger):
    entry = calibration.record_real_outcome(
        "PRED_TEST_ONLY_001", actual_outcome="did not happen", correct=False,
    )
    assert entry["accuracy_score"] == 0.0


def test_calibration_state_never_claims_calibrated_with_few_outcomes(isolated_ledger):
    calibration.record_real_outcome("PRED_TEST_ONLY_001", actual_outcome="x", correct=True)
    state = calibration.read_calibration_state()
    assert state["real_outcome_count"] == 1
    assert state["calibration_status"] == "insufficient_real_outcomes_for_calibration"


def test_real_forecast_ledger_untouched_by_tests():
    # Sanity check that the real ledger directory (not the isolated tmp_path one)
    # still has zero real_outcome recordings from this test run -- i.e. this test
    # module never wrote to production data.
    real_state_path = Path("/opt/pulse5-core/divisions/omnioracle/forecast_outcome_verifier/state/calibration_state_v1.json")
    if real_state_path.exists():
        state = json.loads(real_state_path.read_text())
        # Not asserting count == 0 (a human may have recorded real outcomes since),
        # just confirming this test module didn't write PRED_TEST_ONLY_001 into it.
        assert "PRED_TEST_ONLY_001" not in json.dumps(state)
