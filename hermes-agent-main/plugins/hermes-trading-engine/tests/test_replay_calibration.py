"""Replay calibration: Brier, log-loss clamp, ECE, unresolved exclusion."""

from __future__ import annotations

import math

from engine.replay import calibration as cal


def test_calibration_brier_score():
    pairs = [(0.9, 1), (0.2, 0), (0.6, 1), (0.3, 0)]
    expected = ((0.9 - 1) ** 2 + (0.2) ** 2 + (0.6 - 1) ** 2 + (0.3) ** 2) / 4
    assert abs(cal.brier_score(pairs) - expected) < 1e-12
    assert cal.brier_score([]) is None


def test_calibration_log_loss_clamps_extreme_probabilities():
    # p exactly 0 with y=1, and p exactly 1 with y=0 must not be infinite
    ll = cal.log_loss([(0.0, 1), (1.0, 0)])
    assert ll is not None
    assert math.isfinite(ll)


def test_calibration_ece_buckets():
    # perfectly calibrated bucket: predict 0.5, realized frequency 0.5 -> ECE 0
    perfect = [(0.5, 1), (0.5, 0)]
    assert cal.expected_calibration_error(perfect, buckets=10) == 0.0
    # miscalibrated: predict 0.9 but never happens -> ECE > 0
    bad = [(0.9, 0), (0.9, 0)]
    assert cal.expected_calibration_error(bad, buckets=10) > 0.0


def test_unresolved_outcomes_excluded_from_realized_calibration():
    preds = [
        {"venue": "polymarket", "market_id": "m1", "asset_id": "a1", "outcome": "YES",
         "predicted_probability": 0.8},
        {"venue": "polymarket", "market_id": "m2", "asset_id": "a2", "outcome": "YES",
         "predicted_probability": 0.6},  # no outcome -> unresolved
    ]
    outcomes = [{"venue": "polymarket", "market_id": "m1", "asset_id": "a1",
                 "outcome": "YES", "realized_outcome": 1}]
    summary = cal.summarize_calibration(preds, outcomes)
    assert summary["resolved_count"] == 1
    assert summary["unresolved_count"] == 1
    assert summary["brier_score"] is not None  # computed only over the 1 resolved pair
