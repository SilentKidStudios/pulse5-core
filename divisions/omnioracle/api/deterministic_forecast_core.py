"""Deterministic forecast core -- shared math for Omni Oracle forecast/simulation engines.

CONVERGENCE NOTE (OMNI_ORACLE_REAL-FORECAST_ENGINE_CONVERGENCE campaign, 2026-09-02)
--------------------------------------------------------------------------------------
Before this module existed, scenario_branch_generator, recursive_simulation_engine, and
probabilistic_forecast_engine each generated "confidence" and "risk" values with
Python's global, UNSEEDED `random.uniform()` / `random.choice()` -- numbers with no
relationship to any real input, not reproducible, and not derived from evidence.

This module replaces that with three ingredients, all deterministic:

  1. heuristic_base_score(text) -- a transparent, auditable keyword/weight score over
     REAL caller-supplied text (the scenario objective / outcome / question), in the
     same spirit as omnisim's decision_heuristic engine. Same text -> same score,
     always. This is not a trained or predictive model.

  2. seeded_monte_carlo(base_score, seed=...) -- EXPLICIT seeded sampling (a local
     random.Random(seed) instance, never the global `random` module) representing
     genuine parameter/model uncertainty around the base score. Reproducible: same
     (base_score, seed, n_samples, spread) always produces the identical aggregate.
     The seed is always recorded in the output so any run can be reproduced exactly.
     If no seed is supplied, callers are expected to derive one deterministically from
     their own real input content (see each engine's `_content_seed()`), NOT from time
     or process entropy -- so "no seed given" still means "fully reproducible", not
     "random".

  3. derive_confidence / derive_risk -- deterministic formulas combining the Monte
     Carlo aggregate's mean and dispersion, the real evidence-artifact count, and
     (optionally) cross-source/cross-branch agreement. A single random draw is never
     exposed as a confidence value.

HONESTY BOUNDARY -- this still does NOT make Oracle forecasts real-world-calibrated.
There is still no live external signal ingestion anywhere in this division
(real_signal_connector's connectors remain blueprint_ready/live_collection_enabled=
false), and the scenario objectives/outcomes fed into these engines are still
hardcoded bootstrap content, not live telemetry. What changes is that outputs are now
REPRODUCIBLE and DERIVED FROM REAL INPUTS/SEEDED SAMPLING instead of unconditioned
random draws -- necessary but not sufficient for real predictive accuracy, which still
requires (a) live evidence ingestion and (b) accumulated real-outcome history via
forecast_outcome_verifier/calibration.py. Both remain open; see calibration_status.
"""
from __future__ import annotations

import hashlib
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = "omnioracle_forecast_schema_v1"

DEFAULT_KEYWORD_WEIGHTS = {
    "stable": 0.12, "stability": 0.10, "success": 0.15, "successful": 0.15,
    "growth": 0.10, "improve": 0.10, "improves": 0.10, "healing": 0.08,
    "recovery": 0.08, "coordination": 0.06, "scaling": 0.05, "accuracy": 0.06,
    "fail": -0.18, "failure": -0.20, "instability": -0.15, "unstable": -0.15,
    "conflict": -0.12, "bottleneck": -0.10, "contention": -0.10, "risk": -0.08,
    "error": -0.15, "noise": -0.08, "degrade": -0.12, "degradation": -0.12,
}


def content_seed(*parts: str) -> int:
    """Deterministic seed derived from real text content -- never time/process entropy.
    Same content -> same seed, always, so 'no explicit seed given' still means
    fully reproducible rather than random."""
    joined = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(joined.encode()).hexdigest()[:8], 16)


def heuristic_base_score(text: str, weights: Optional[dict] = None, base: float = 0.5) -> float:
    """Transparent keyword-weighted score in [0,1]. Deterministic: same text -> same score."""
    weights = weights or DEFAULT_KEYWORD_WEIGHTS
    t = (text or "").lower()
    score = base
    for kw, w in weights.items():
        if kw in t:
            score += w
    return max(0.0, min(1.0, round(score, 4)))


@dataclass
class MonteCarloResult:
    seed: int
    n_samples: int
    mean: float
    stdev: float
    agreement_fraction: float
    min_sample: float
    max_sample: float


def seeded_monte_carlo(base_score: float, *, seed: int, n_samples: int = 200, spread: float = 0.12) -> MonteCarloResult:
    """Explicit seeded ensemble sampling around base_score using a LOCAL Random
    instance. Reproducible: identical inputs always produce an identical result.
    This is the only place `random` is used anywhere in this module, and it is
    always seeded -- never the global `random` module."""
    if n_samples < 1:
        raise ValueError("n_samples must be >= 1")
    if seed is None:
        seed = 0  # explicit deterministic default -- random.Random(None) would seed from OS entropy
    rng = random.Random(seed)
    samples = [max(0.0, min(1.0, rng.gauss(base_score, spread))) for _ in range(n_samples)]
    mean = round(statistics.fmean(samples), 4)
    stdev = round(statistics.pstdev(samples), 4) if len(samples) > 1 else 0.0
    within_band = sum(1 for s in samples if abs(s - mean) <= max(spread, 1e-9))
    agreement_fraction = round(within_band / len(samples), 4)
    return MonteCarloResult(
        seed=seed, n_samples=n_samples, mean=mean, stdev=stdev,
        agreement_fraction=agreement_fraction,
        min_sample=round(min(samples), 4), max_sample=round(max(samples), 4),
    )


def derive_confidence(mc: MonteCarloResult, evidence_count: int,
                       cross_source_agreement: Optional[float] = None) -> float:
    """Deterministic confidence: rises with evidence volume (capped, diminishing),
    falls with Monte Carlo dispersion, rises with cross-source agreement. Never a
    raw random draw."""
    evidence_term = min(0.15, 0.03 * max(0, evidence_count))
    dispersion_penalty = min(0.35, mc.stdev * 2.0)
    agreement_term = 0.0
    if cross_source_agreement is not None:
        agreement_term = (cross_source_agreement - 0.5) * 0.2
    confidence = mc.mean + evidence_term - dispersion_penalty + agreement_term
    return round(max(0.0, min(0.99, confidence)), 4)


def derive_risk(mc: MonteCarloResult, contradiction_flags: int = 0) -> float:
    """Deterministic risk: rises with Monte Carlo dispersion and contradictions,
    rises as the mean projection falls."""
    risk = mc.stdev * 1.5 + min(0.4, contradiction_flags * 0.1) + (1 - mc.mean) * 0.3
    return round(max(0.0, min(1.0, risk)), 4)


def probability_band(confidence: Optional[float]) -> str:
    if confidence is None:
        return "NO_BAND_INSUFFICIENT_DATA"
    if confidence >= 0.85:
        return "HIGH_CONFIDENCE"
    if confidence >= 0.70:
        return "MODERATE_CONFIDENCE"
    if confidence >= 0.50:
        return "SPECULATIVE"
    return "LOW_CONFIDENCE"


def _now():
    return datetime.now(timezone.utc).isoformat()


def canonical_forecast_result(*, forecast, evidence_inputs, scenario_assumptions,
                               model_or_engine_contributors, mc: MonteCarloResult,
                               evidence_count: int, min_evidence: int = 1,
                               confidence: Optional[float] = None, risk: Optional[float] = None,
                               cross_source_agreement: Optional[float] = None,
                               contradiction_flags: int = 0, synthetic_provenance: bool = True,
                               calibration_status: str = "uncalibrated_no_real_outcomes_yet",
                               timestamp: Optional[str] = None) -> dict:
    """Assemble the canonical Omni Oracle forecast result schema. Fails closed
    (status=INSUFFICIENT_DATA, confidence=None) when evidence_count < min_evidence,
    rather than inventing a plausible-looking number."""
    ts = timestamp or _now()
    reproducibility = {"deterministic": True, "seed": mc.seed, "n_samples": mc.n_samples}

    if evidence_count < min_evidence:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INSUFFICIENT_DATA",
            "forecast": None,
            "confidence": None,
            "probability_band": probability_band(None),
            "uncertainty": {
                "kind": "insufficient_evidence",
                "note": f"evidence_count={evidence_count} < min_evidence={min_evidence}; "
                        "refusing to fabricate a confidence value.",
            },
            "risk": None,
            "evidence_inputs": evidence_inputs,
            "scenario_assumptions": scenario_assumptions,
            "model_or_engine_contributors": model_or_engine_contributors,
            "provenance": "synthetic" if synthetic_provenance else "live",
            "calibration_status": calibration_status,
            "reproducibility": reproducibility,
            "schema": "canonical_forecast_result_v1",
            "timestamp": ts,
        }

    conf = confidence if confidence is not None else derive_confidence(mc, evidence_count, cross_source_agreement)
    rsk = risk if risk is not None else derive_risk(mc, contradiction_flags)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "forecast": forecast,
        "confidence": conf,
        "probability_band": probability_band(conf),
        "uncertainty": {
            "kind": "seeded_monte_carlo_dispersion",
            "stdev": mc.stdev,
            "agreement_fraction": mc.agreement_fraction,
            "sample_range": [mc.min_sample, mc.max_sample],
            "note": "confidence/risk are deterministic functions of a transparent keyword heuristic, "
                    "seeded Monte Carlo dispersion, and real evidence-artifact counts -- not unconditioned "
                    "random draws. Still not calibrated against real-world outcomes; see calibration_status.",
        },
        "risk": rsk,
        "evidence_inputs": evidence_inputs,
        "scenario_assumptions": scenario_assumptions,
        "model_or_engine_contributors": model_or_engine_contributors,
        "provenance": "synthetic" if synthetic_provenance else "live",
        "calibration_status": calibration_status,
        "reproducibility": reproducibility,
        "schema": "canonical_forecast_result_v1",
        "timestamp": ts,
    }
