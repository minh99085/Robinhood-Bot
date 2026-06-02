"""Institutional probability calibration tests (TDD, deterministic, no network).

Quant scope exercised here:
* Statistical & Probabilistic Modeling — fitted Platt / isotonic / temperature
  calibration plus a conservative shrink fallback when samples are insufficient.
* Backtesting & Simulation — Brier / log-loss / ECE before-vs-after calibration.
* Risk Management — calibration can only make probabilities MORE conservative
  (never more aggressive) when evidence is thin.
* Compliance/Operational Excellence — calibration artifacts round-trip for
  replay + training reports.

All datasets are hand-built and deterministic; there is no randomness, no
network, and no Grok call.
"""

from __future__ import annotations

import math

from engine.calibration_models import (
    InstitutionalCalibrator,
    brier,
    calibration_slope_intercept,
    ece,
    fit_isotonic,
    fit_platt,
    fit_temperature,
    log_loss,
    reliability_buckets,
)
from engine.markets import universe_manager as um
from engine.training.config import TrainingConfig
from engine.training.probability_stack import ProbabilityEstimate, ProbabilityStack

from tests._pmtrain_helpers import FakeResearch, market

_NOW = 1_000_000.0


def _overconfident_pairs(scale: int = 1) -> list[tuple[float, int]]:
    """Symmetric over-confident dataset: model says 0.90 / 0.10 but the realized
    frequency is only 0.70 / 0.30. A good calibrator pulls these toward centre."""
    pairs: list[tuple[float, int]] = []
    for _ in range(scale):
        pairs += [(0.90, 1)] * 7 + [(0.90, 0)] * 3      # p=0.9, freq 0.7
        pairs += [(0.70, 1)] * 6 + [(0.70, 0)] * 4      # p=0.7, freq 0.6
        pairs += [(0.30, 1)] * 4 + [(0.30, 0)] * 6      # p=0.3, freq 0.4
        pairs += [(0.10, 1)] * 3 + [(0.10, 0)] * 7      # p=0.1, freq 0.3
    return pairs


# --------------------------------------------------------------------------- #
# 1. backward-compatible dataclass
# --------------------------------------------------------------------------- #
def test_probability_estimate_new_fields_have_safe_defaults():
    est = ProbabilityEstimate(
        market_id="m", p_market_mid=0.5, p_model=0.5, p_research=0.5, p_raw=0.5,
        p_final=0.5, shrink=0.25, confidence=0.5, research_source="grok_cache",
        research_usable=True, model_has_edge=False, ambiguity_score=0.0,
        evidence_score=0.5, stale_score=0.0, spread=0.02, liquidity_usd=10_000.0,
        calibration_error=0.0, fresh_book=True, best_ask=0.51)
    # new optional fields exist with conservative defaults
    assert est.calibrated_probability == 0.5
    assert est.confidence_interval_low <= est.calibrated_probability <= est.confidence_interval_high
    assert est.uncertainty_components == {}
    assert est.effective_sample_size == 0.0
    assert est.calibration_method == "identity"
    assert est.chainlink_features == {}
    assert est.bregman_group_id == ""
    assert est.no_trade_probability_reason == ""
    d = est.to_dict()
    assert "calibrated_probability" in d and "uncertainty_components" in d


# --------------------------------------------------------------------------- #
# 2. fitted calibration methods improve calibration
# --------------------------------------------------------------------------- #
def test_platt_scaling_reduces_calibration_error():
    pairs = _overconfident_pairs(scale=4)
    model = fit_platt(pairs)
    cal = [(model.transform(p), y) for p, y in pairs]
    assert ece(cal) < ece(pairs)
    assert brier(cal) <= brier(pairs) + 1e-9
    # over-confident 0.9 prediction is pulled down toward the realized 0.7
    assert model.transform(0.9) < 0.9


def test_isotonic_calibration_is_monotone_and_reduces_ece():
    pairs = _overconfident_pairs(scale=4)
    model = fit_isotonic(pairs)
    grid = [i / 20 for i in range(21)]
    mapped = [model.transform(p) for p in grid]
    assert all(b >= a - 1e-9 for a, b in zip(mapped, mapped[1:]))  # non-decreasing
    cal = [(model.transform(p), y) for p, y in pairs]
    assert ece(cal) <= ece(pairs) + 1e-9


def test_temperature_scaling_softens_overconfidence():
    pairs = _overconfident_pairs(scale=4)
    model = fit_temperature(pairs)
    assert model.temperature > 1.0          # T>1 softens an over-confident model
    assert log_loss([(model.transform(p), y) for p, y in pairs]) <= log_loss(pairs) + 1e-9
    assert model.transform(0.9) < 0.9


def test_calibration_slope_intercept_detects_overconfidence():
    # over-confident model -> calibration slope < 1
    slope, intercept = calibration_slope_intercept(_overconfident_pairs(scale=4))
    assert slope < 1.0
    assert math.isfinite(intercept)


def test_reliability_buckets_sum_counts():
    pairs = _overconfident_pairs(scale=2)
    rows = reliability_buckets(pairs, bins=10)
    assert sum(r["count"] for r in rows) == len(pairs)
    for r in rows:
        if r["count"]:
            assert 0.0 <= r["avg_predicted"] <= 1.0
            assert 0.0 <= r["realized_frequency"] <= 1.0


# --------------------------------------------------------------------------- #
# 3. conservative shrink fallback when samples are insufficient
# --------------------------------------------------------------------------- #
def test_insufficient_samples_fall_back_to_conservative_shrink():
    pairs = [(0.9, 1), (0.8, 1), (0.2, 0)]          # far below min_samples
    cal = InstitutionalCalibrator(method="auto", min_samples=20).fit(pairs)
    assert cal.calibration_method == "conservative_shrink"
    # a 0.9 raw probability must be pulled toward 0.5 (less aggressive), never up
    assert 0.5 < cal.transform(0.9) < 0.9
    assert cal.transform(0.1) > 0.1
    assert cal.effective_sample_size == len(pairs)


def test_more_samples_means_less_shrink_in_fallback():
    few = InstitutionalCalibrator(method="conservative_shrink", min_samples=20).fit(
        [(0.9, 1)] * 2)
    more = InstitutionalCalibrator(method="conservative_shrink", min_samples=20).fit(
        [(0.9, 1)] * 18)
    # with more evidence the fallback shrinks less (closer to the raw 0.9)
    assert more.transform(0.9) >= few.transform(0.9) - 1e-9


def test_auto_uses_fitted_method_when_enough_samples():
    cal = InstitutionalCalibrator(method="auto", min_samples=20).fit(
        _overconfident_pairs(scale=4))
    assert cal.calibration_method in ("platt", "isotonic", "temperature")
    assert cal.effective_sample_size >= 20


# --------------------------------------------------------------------------- #
# 4. confidence intervals + artifact round-trip
# --------------------------------------------------------------------------- #
def test_transform_with_interval_is_ordered_and_bounded():
    cal = InstitutionalCalibrator(method="platt", min_samples=5).fit(
        _overconfident_pairs(scale=4))
    p_cal, lo, hi = cal.transform_with_interval(0.9)
    assert 0.0 <= lo <= p_cal <= hi <= 1.0


def test_calibration_artifact_round_trips():
    cal = InstitutionalCalibrator(method="platt", min_samples=5).fit(
        _overconfident_pairs(scale=4))
    art = cal.to_artifact()
    for key in ("method", "effective_sample_size", "slope", "intercept",
                "reliability_buckets", "metrics", "params"):
        assert key in art
    restored = InstitutionalCalibrator.from_artifact(art)
    assert abs(restored.transform(0.9) - cal.transform(0.9)) < 1e-9
    assert restored.calibration_method == cal.calibration_method


# --------------------------------------------------------------------------- #
# 5. ProbabilityStack integration (additive, backward compatible)
# --------------------------------------------------------------------------- #
def _rec():
    return um.MarketRecord.from_raw(
        market(0, bid=0.28, ask=0.30, liq=20_000, depth=1000, now=_NOW), now=_NOW)


def test_probability_stack_populates_calibration_fields():
    cfg = TrainingConfig()
    cal = InstitutionalCalibrator(method="platt", min_samples=5).fit(
        _overconfident_pairs(scale=4))
    stack = ProbabilityStack(cfg, calibrator=cal)
    est = stack.estimate(_rec(), FakeResearch(fair=0.80, conf=0.9), now=_NOW)

    assert est.calibration_method == "platt"
    assert 0.0 <= est.calibrated_probability <= 1.0
    assert est.confidence_interval_low <= est.calibrated_probability <= est.confidence_interval_high
    assert est.effective_sample_size == cal.effective_sample_size
    # uncertainty decomposition is always populated
    for k in ("market", "model", "research", "chainlink", "liquidity",
              "ambiguity", "stale", "total"):
        assert k in est.uncertainty_components


def test_probability_stack_calibrator_does_not_change_p_final():
    """The calibrator only annotates `calibrated_probability`; the executable
    fair value `p_final` (and the edge gate that depends on it) is unchanged."""
    cfg = TrainingConfig()
    rec = _rec()
    sig = FakeResearch(fair=0.80, conf=0.9)
    base = ProbabilityStack(cfg).estimate(rec, sig, now=_NOW)
    cal = InstitutionalCalibrator(method="platt", min_samples=5).fit(
        _overconfident_pairs(scale=4))
    withcal = ProbabilityStack(cfg, calibrator=cal).estimate(rec, sig, now=_NOW)
    assert abs(base.p_final - withcal.p_final) < 1e-12
