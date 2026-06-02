"""Bayesian shrinkage + calibrated probability distribution.

Quant scope — *Statistical & Probabilistic Modeling* + *Risk Management*: proves
the calibrated probability is a DISTRIBUTION (mean + credible interval +
uncertainty components + effective sample size + evidence quality + no-trade
reason), and that the Bayesian shrinkage pulls toward the market price when
evidence is weak, sample size is low, settlement ambiguity is high, Chainlink is
stale, or calibration is unstable. PAPER ONLY — analytics, never sizes/approves.
"""

from __future__ import annotations

import pytest

from engine.training.probability_stack import (
    bayesian_shrink, calibrated_distribution)


def test_strong_evidence_keeps_raw_probability():
    p = bayesian_shrink(0.80, 0.50, evidence_quality=1.0, effective_sample_size=500.0,
                        ambiguity=0.0, chainlink_stale=False, calibration_instability=0.0)
    assert p == pytest.approx(0.80, abs=0.03)


def test_weak_evidence_shrinks_to_market():
    p = bayesian_shrink(0.80, 0.50, evidence_quality=0.05, effective_sample_size=3.0,
                        ambiguity=0.5, chainlink_stale=False, calibration_instability=0.6)
    assert abs(p - 0.50) < abs(p - 0.80)


def test_stale_chainlink_pins_to_market():
    p = bayesian_shrink(0.80, 0.50, evidence_quality=1.0, effective_sample_size=500.0,
                        chainlink_stale=True)
    assert p == pytest.approx(0.50, abs=0.02)


@pytest.mark.parametrize("field", ["ambiguity", "calibration_instability"])
def test_more_of_each_risk_shrinks_more(field):
    base = bayesian_shrink(0.80, 0.50, **{field: 0.0})
    worse = bayesian_shrink(0.80, 0.50, **{field: 0.8})
    assert abs(worse - 0.50) <= abs(base - 0.50)


def test_low_sample_shrinks_more_than_high_sample():
    low = bayesian_shrink(0.80, 0.50, effective_sample_size=2.0, evidence_quality=0.6)
    high = bayesian_shrink(0.80, 0.50, effective_sample_size=500.0, evidence_quality=0.6)
    assert abs(high - 0.50) > abs(low - 0.50)


def test_calibrated_distribution_has_full_shape():
    d = calibrated_distribution(mean=0.62, ci_low=0.55, ci_high=0.69,
                                uncertainty_components={"total": 0.2, "research": 0.1},
                                effective_sample_size=120.0, evidence_quality=0.7)
    for k in ("mean", "ci_low", "ci_high", "interval_width", "uncertainty_components",
              "effective_sample_size", "evidence_quality", "no_trade_reason"):
        assert k in d
    assert d["interval_width"] == pytest.approx(0.69 - 0.55)
    assert d["uncertainty_components"]["total"] == 0.2
