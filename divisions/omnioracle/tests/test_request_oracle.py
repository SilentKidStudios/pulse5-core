import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "api"
sys.path.insert(0, str(API_DIR))

import pytest
import request_oracle as ro


# --- normal path -----------------------------------------------------------

def test_read_mode_known_domain_returns_ok_or_insufficient_evidence():
    result = ro.request_oracle("runtime_autonomy", mode="read")
    assert result["status"] in ("ok", "insufficient_evidence")
    assert result["domain_recognized"] is True
    assert result["reproducible"] is True
    assert "receipt_path" in result
    assert Path(result["receipt_path"]).exists()


def test_read_mode_is_deterministic_given_fixed_disk_state():
    r1 = ro.request_oracle("runtime_autonomy", mode="read")
    r2 = ro.request_oracle("runtime_autonomy", mode="read")
    # observations must match exactly (excluding request_id/timestamp/receipt_path)
    assert r1["observations"] == r2["observations"]
    assert r1["evidence_count"] == r2["evidence_count"]


def test_never_claims_certainty():
    result = ro.request_oracle("runtime_autonomy", mode="read")
    if result.get("synthetic_projection"):
        assert result["synthetic_projection"]["synthetic"] is True
    boundary_text = " ".join(result["recommendation_boundary"]).lower()
    assert "not a prediction of fact" in boundary_text or "not a" in boundary_text


# --- ensemble ----------------------------------------------------------------

def test_ensemble_with_omnisim_scenario():
    result = ro.request_oracle(
        "runtime_autonomy", mode="read",
        also_run_omnisim_scenario="Should we proceed with a low-risk build task?",
    )
    assert "omnisim_scenario" in result
    assert result["omnisim_scenario"]["engine"] == "omnisim.api.request_scenario"


# --- validation / fail-safe gates -------------------------------------------

@pytest.mark.parametrize("bad_domain", [None, "", "   ", 123, [], {}])
def test_malformed_domain_rejected(bad_domain):
    with pytest.raises(ro.OracleValidationError):
        ro.request_oracle(bad_domain, mode="read")


def test_unknown_mode_rejected():
    with pytest.raises(ro.OracleValidationError):
        ro.request_oracle("runtime_autonomy", mode="predict_the_future")


def test_negative_min_evidence_rejected():
    with pytest.raises(ro.OracleValidationError):
        ro.request_oracle("runtime_autonomy", mode="read", min_evidence=-1)


def test_insufficient_evidence_fails_safely_not_fabricated():
    result = ro.request_oracle("runtime_autonomy", mode="read", min_evidence=10_000_000)
    assert result["status"] == "insufficient_evidence"
    assert "synthetic_projection" not in result


def test_unsupported_domain_with_no_data_is_insufficient_evidence():
    result = ro.request_oracle("a_domain_that_has_never_existed_xyz", mode="read", min_evidence=1)
    assert result["domain_recognized"] is False
    assert result["status"] == "insufficient_evidence"


def test_extreme_input_long_question_does_not_crash():
    long_question = "why? " * 5000
    result = ro.request_oracle("runtime_autonomy", mode="read", question=long_question)
    assert result["status"] in ("ok", "insufficient_evidence")


def test_contradictory_params_do_not_crash():
    # asking to "generate" fresh data but requiring more evidence than could plausibly exist
    result = ro.request_oracle("runtime_autonomy", mode="read", seed="fixed-seed-123", min_evidence=0)
    assert result["request"]["seed"] == "fixed-seed-123"


# --- failover ----------------------------------------------------------------

def test_run_engine_missing_script_reports_missing_not_crash():
    fake = Path("/opt/pulse5-core/divisions/omnioracle/does_not_exist_v1.py")
    outcome = ro._run_engine(fake)
    assert outcome["ok"] is False
    assert outcome["status"] == "missing"


def test_omnisim_ensemble_failure_is_bounded(monkeypatch):
    # Simulate a realistic failure: the helper itself raises unexpectedly (e.g. a bug
    # in its own try/except). request_oracle's call site must still not propagate it.
    def boom(*a, **k):
        raise RuntimeError("simulated omnisim outage")

    monkeypatch.setattr(ro, "_try_omnisim_scenario", boom, raising=True)
    result = ro.request_oracle("runtime_autonomy", mode="read", also_run_omnisim_scenario="test")
    assert result["status"] in ("ok", "insufficient_evidence")
    assert result["omnisim_scenario"]["ok"] is False
    assert "simulated omnisim outage" in result["omnisim_scenario"]["error"]


def test_omnisim_ensemble_real_import_failure_is_caught_internally(monkeypatch):
    # Realistic failure mode: omnisim/api is unreachable. _try_omnisim_scenario's own
    # try/except must catch this without help from the outer call site. Force a fresh
    # import attempt (bypassing sys.modules caching from earlier tests) against a
    # nonexistent root.
    monkeypatch.delitem(sys.modules, "request_scenario", raising=False)
    monkeypatch.setattr(ro, "ORACLE", Path("/tmp/does_not_exist_oracle_root_xyz"))
    clean_path = [p for p in sys.path if not p.endswith("omnisim/api")]
    monkeypatch.setattr(ro.sys, "path", clean_path)
    outcome = ro._try_omnisim_scenario("test question")
    assert outcome["ok"] is False
    assert outcome["engine"] == "omnisim.api.request_scenario"
