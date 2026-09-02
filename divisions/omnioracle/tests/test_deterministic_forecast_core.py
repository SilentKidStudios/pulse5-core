"""Tests for the OMNI_ORACLE_REAL-FORECAST_ENGINE_CONVERGENCE rewrite: deterministic
core math, and the four rewritten engines (scenario_branch_generator,
recursive_simulation_engine, consensus_intelligence_layer, probabilistic_forecast_engine).

Covers (per campaign requirement 6): same inputs => same outputs; explicit seeded
Monte Carlo => reproducible distribution; missing evidence => fail closed;
contradictory evidence => uncertainty rises; stronger agreeing evidence => confidence
can rise; no arbitrary random confidence values; no hidden synthetic-to-live
promotion; malformed inputs rejected safely; consensus calculation deterministic;
risk calculation deterministic.
"""
import re
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "api"
sys.path.insert(0, str(API_DIR))

import pytest
import deterministic_forecast_core as core


# --- heuristic_base_score ----------------------------------------------------

def test_heuristic_base_score_deterministic():
    a = core.heuristic_base_score("runtime_autonomy stable_growth")
    b = core.heuristic_base_score("runtime_autonomy stable_growth")
    assert a == b


def test_heuristic_base_score_reflects_real_text_content():
    positive = core.heuristic_base_score("stable success growth recovery")
    negative = core.heuristic_base_score("failure instability conflict error")
    assert positive > negative


def test_heuristic_base_score_bounded():
    assert 0.0 <= core.heuristic_base_score("") <= 1.0
    assert 0.0 <= core.heuristic_base_score("success " * 100) <= 1.0


# --- content_seed --------------------------------------------------------------

def test_content_seed_deterministic():
    assert core.content_seed("a", "b") == core.content_seed("a", "b")


def test_content_seed_varies_with_content():
    assert core.content_seed("a", "b") != core.content_seed("a", "c")


# --- seeded_monte_carlo: same inputs => same outputs, reproducible distribution --

def test_seeded_monte_carlo_reproducible():
    mc1 = core.seeded_monte_carlo(0.6, seed=42, n_samples=500, spread=0.1)
    mc2 = core.seeded_monte_carlo(0.6, seed=42, n_samples=500, spread=0.1)
    assert mc1 == mc2


def test_seeded_monte_carlo_records_seed_and_config():
    mc = core.seeded_monte_carlo(0.5, seed=7, n_samples=100, spread=0.2)
    assert mc.seed == 7
    assert mc.n_samples == 100


def test_seeded_monte_carlo_rejects_zero_samples():
    with pytest.raises(ValueError):
        core.seeded_monte_carlo(0.5, seed=1, n_samples=0)


def test_seeded_monte_carlo_defaults_seed_when_none_but_stays_reproducible():
    mc1 = core.seeded_monte_carlo(0.5, seed=None, n_samples=50)
    mc2 = core.seeded_monte_carlo(0.5, seed=None, n_samples=50)
    assert mc1 == mc2  # seed=None resolves to a fixed explicit default (0), not entropy


# --- derive_confidence / derive_risk: deterministic, evidence-derived -----------

def test_derive_confidence_is_pure_function_of_inputs():
    mc = core.seeded_monte_carlo(0.7, seed=1, n_samples=100)
    c1 = core.derive_confidence(mc, evidence_count=3)
    c2 = core.derive_confidence(mc, evidence_count=3)
    assert c1 == c2  # no hidden randomness sneaking into the formula


def test_stronger_agreeing_evidence_raises_confidence():
    mc = core.seeded_monte_carlo(0.7, seed=1, n_samples=100)
    low_agreement = core.derive_confidence(mc, evidence_count=3, cross_source_agreement=0.4)
    high_agreement = core.derive_confidence(mc, evidence_count=3, cross_source_agreement=0.95)
    assert high_agreement > low_agreement


def test_more_evidence_raises_confidence_holding_mc_fixed():
    mc = core.seeded_monte_carlo(0.7, seed=1, n_samples=100)
    few = core.derive_confidence(mc, evidence_count=0)
    many = core.derive_confidence(mc, evidence_count=5)
    assert many > few


def test_higher_dispersion_raises_risk():
    tight = core.seeded_monte_carlo(0.6, seed=1, n_samples=500, spread=0.02)
    wide = core.seeded_monte_carlo(0.6, seed=1, n_samples=500, spread=0.30)
    assert core.derive_risk(wide) > core.derive_risk(tight)


def test_contradictions_raise_risk():
    mc = core.seeded_monte_carlo(0.6, seed=1, n_samples=200)
    assert core.derive_risk(mc, contradiction_flags=3) > core.derive_risk(mc, contradiction_flags=0)


def test_no_arbitrary_random_confidence_values():
    # derive_confidence never touches the global random module; verify by checking
    # its result is fully determined by (mc, evidence_count, cross_source_agreement).
    mc = core.seeded_monte_carlo(0.55, seed=99, n_samples=300)
    results = {core.derive_confidence(mc, evidence_count=2, cross_source_agreement=0.7) for _ in range(10)}
    assert len(results) == 1


# --- canonical_forecast_result: fail-closed + no hidden promotion ---------------

def test_fails_closed_below_min_evidence():
    mc = core.seeded_monte_carlo(0.8, seed=1, n_samples=50)
    result = core.canonical_forecast_result(
        forecast={"x": 1}, evidence_inputs={}, scenario_assumptions={},
        model_or_engine_contributors=[], mc=mc, evidence_count=0, min_evidence=1,
    )
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["confidence"] is None
    assert result["forecast"] is None


def test_ok_status_above_min_evidence():
    mc = core.seeded_monte_carlo(0.8, seed=1, n_samples=50)
    result = core.canonical_forecast_result(
        forecast={"x": 1}, evidence_inputs={}, scenario_assumptions={},
        model_or_engine_contributors=[], mc=mc, evidence_count=2, min_evidence=1,
    )
    assert result["status"] == "OK"
    assert result["confidence"] is not None
    assert 0.0 <= result["confidence"] <= 0.99


def test_no_hidden_synthetic_to_live_promotion():
    mc = core.seeded_monte_carlo(0.8, seed=1, n_samples=50)
    result = core.canonical_forecast_result(
        forecast={"x": 1}, evidence_inputs={}, scenario_assumptions={},
        model_or_engine_contributors=[], mc=mc, evidence_count=2, min_evidence=1,
        synthetic_provenance=True,
    )
    assert result["provenance"] == "synthetic"
    # calibration_status must never silently claim "calibrated" without real outcomes
    assert "uncalibrated" in result["calibration_status"] or "insufficient" in result["calibration_status"]


def test_probability_band_none_when_confidence_none():
    assert core.probability_band(None) == "NO_BAND_INSUFFICIENT_DATA"


# --- static check: no unseeded randomness left in the rewritten engines --------

REWRITTEN_ENGINE_FILES = [
    Path(__file__).resolve().parent.parent / "scenario_branch_generator/omnioracle_scenario_branch_generator_v1.py",
    Path(__file__).resolve().parent.parent / "recursive_simulation_engine/omnioracle_recursive_simulation_engine_v1.py",
    Path(__file__).resolve().parent.parent / "consensus_intelligence_layer/omnioracle_consensus_intelligence_layer_v1.py",
    Path(__file__).resolve().parent.parent / "probabilistic_forecast_engine/omnioracle_probabilistic_forecast_engine_v1.py",
]


def _strip_module_docstring(text):
    # These files document their OWN prior random.uniform()/random.choice() usage in
    # their module docstrings for context -- strip the docstring before checking for
    # actual (code) usage, not prose describing history.
    match = re.match(r'^(#![^\n]*\n)?("""|\'\'\')', text)
    if not match:
        return text
    quote = match.group(2)
    start = match.end()
    end = text.find(quote, start)
    return text[end + 3:] if end != -1 else text


@pytest.mark.parametrize("engine_file", REWRITTEN_ENGINE_FILES)
def test_no_unseeded_random_calls_in_rewritten_engines(engine_file):
    text = engine_file.read_text()
    assert re.search(r"^\s*import random\b", text, re.MULTILINE) is None, "must not import the global random module"
    code_only = _strip_module_docstring(text)
    assert "random.uniform(" not in code_only
    assert "random.choice(" not in code_only


# --- engine-level: scenario_branch_generator ------------------------------------

def _import_engine(subdir, filename, modname):
    import importlib.util
    path = Path(__file__).resolve().parent.parent / subdir / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scenario_branch_generator_deterministic():
    sbg = _import_engine("scenario_branch_generator", "omnioracle_scenario_branch_generator_v1.py", "sbg_test")
    _, sets1 = sbg.generate_branches()
    _, sets2 = sbg.generate_branches()

    def strip_ts(sets):
        return [[{k: v for k, v in b.items() if k != "generated_at"} for b in s] for s in sets]

    assert strip_ts(sets1) == strip_ts(sets2)


def test_scenario_branch_generator_explicit_seed_override_reproducible():
    sbg = _import_engine("scenario_branch_generator", "omnioracle_scenario_branch_generator_v1.py", "sbg_test2")
    _, sets_a = sbg.generate_branches(seed_override=123)
    _, sets_b = sbg.generate_branches(seed_override=123)

    def strip_ts(sets):
        return [[{k: v for k, v in b.items() if k != "generated_at"} for b in s] for s in sets]

    assert strip_ts(sets_a) == strip_ts(sets_b)


# --- engine-level: recursive_simulation_engine ----------------------------------

def test_recursive_simulation_engine_deterministic():
    rse = _import_engine("recursive_simulation_engine", "omnioracle_recursive_simulation_engine_v1.py", "rse_test")
    summary1 = rse.run_recursive_simulation()
    summary2 = rse.run_recursive_simulation()
    assert summary1["recursive_simulation_sets"] == summary2["recursive_simulation_sets"]
    assert summary1["simulation_outputs"] == summary2["simulation_outputs"]


# --- engine-level: consensus_intelligence_layer ---------------------------------

def test_consensus_deterministic_given_fixed_branches():
    cons = _import_engine("consensus_intelligence_layer", "omnioracle_consensus_intelligence_layer_v1.py", "cons_test")
    branches = [
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.8},
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.82},
        {"future_outcome": "stable_growth", "risk_level": "medium_controlled", "confidence": 0.79},
    ]
    r1 = cons.compute_consensus_for_branches(branches, objective="test_obj", source_label="test")
    r2 = cons.compute_consensus_for_branches(branches, objective="test_obj", source_label="test")
    r1.pop("generated_at"); r2.pop("generated_at")
    assert r1 == r2


def test_consensus_none_for_empty_branches():
    cons = _import_engine("consensus_intelligence_layer", "omnioracle_consensus_intelligence_layer_v1.py", "cons_test_empty")
    assert cons.compute_consensus_for_branches([], objective="x", source_label="y") is None


def test_contradictory_evidence_raises_consensus_risk():
    cons = _import_engine("consensus_intelligence_layer", "omnioracle_consensus_intelligence_layer_v1.py", "cons_test_contra")
    agreeing = [
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.8},
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.81},
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.79},
    ]
    contradictory = [
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.8},
        {"future_outcome": "worker_divergence", "risk_level": "medium", "confidence": 0.3},
        {"future_outcome": "controlled_risk", "risk_level": "medium_controlled", "confidence": 0.55},
    ]
    r_agree = cons.compute_consensus_for_branches(agreeing, objective="x", source_label="y")
    r_contra = cons.compute_consensus_for_branches(contradictory, objective="x", source_label="y")
    assert r_contra["consensus_risk_score"] > r_agree["consensus_risk_score"]
    assert r_contra["agreement_fraction"] < r_agree["agreement_fraction"]


def test_unanimous_agreement_yields_higher_agreement_fraction_than_split():
    cons = _import_engine("consensus_intelligence_layer", "omnioracle_consensus_intelligence_layer_v1.py", "cons_test_unan")
    unanimous = [{"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.75} for _ in range(4)]
    split = [
        {"future_outcome": "stable_growth", "risk_level": "low", "confidence": 0.75},
        {"future_outcome": "controlled_risk", "risk_level": "medium_controlled", "confidence": 0.6},
    ]
    r_unanimous = cons.compute_consensus_for_branches(unanimous, objective="x", source_label="y")
    r_split = cons.compute_consensus_for_branches(split, objective="x", source_label="y")
    assert r_unanimous["agreement_fraction"] == 1.0
    assert r_split["agreement_fraction"] < 1.0


# --- engine-level: probabilistic_forecast_engine --------------------------------

def test_probabilistic_forecast_fails_closed_for_unknown_domain_no_evidence():
    pfe = _import_engine("probabilistic_forecast_engine", "omnioracle_probabilistic_forecast_engine_v1.py", "pfe_test")
    result = pfe.forecast_domain("a_domain_that_will_never_have_evidence_xyz123", min_evidence=1)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["confidence"] is None


def test_probabilistic_forecast_deterministic_with_explicit_seed():
    pfe = _import_engine("probabilistic_forecast_engine", "omnioracle_probabilistic_forecast_engine_v1.py", "pfe_test2")
    r1 = pfe.forecast_domain("runtime_autonomy", seed_override=555, min_evidence=0)
    r2 = pfe.forecast_domain("runtime_autonomy", seed_override=555, min_evidence=0)
    r1.pop("timestamp"); r2.pop("timestamp")
    assert r1 == r2


def test_probabilistic_forecast_never_returns_raw_random_confidence():
    pfe = _import_engine("probabilistic_forecast_engine", "omnioracle_probabilistic_forecast_engine_v1.py", "pfe_test3")
    result = pfe.forecast_domain("runtime_autonomy", seed_override=1, min_evidence=0)
    if result["status"] == "OK":
        assert result["reproducibility"]["deterministic"] is True
        assert result["provenance"] == "synthetic"  # never silently promoted to "live"


def test_probabilistic_forecast_malformed_domain_rejected_by_facade():
    # forecast_domain itself trusts its caller (internal function); the public
    # request_oracle facade is what performs input validation -- covered in
    # test_request_oracle.py's malformed_domain tests. Here we just confirm
    # forecast_domain doesn't crash on an empty-but-valid string.
    pfe = _import_engine("probabilistic_forecast_engine", "omnioracle_probabilistic_forecast_engine_v1.py", "pfe_test4")
    result = pfe.forecast_domain("", min_evidence=1)
    assert result["status"] == "INSUFFICIENT_DATA"
