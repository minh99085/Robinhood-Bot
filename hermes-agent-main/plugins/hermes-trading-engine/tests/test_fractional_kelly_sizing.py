"""Fractional-Kelly directional sizing (TDD, deterministic, offline).

Quant scope: Risk Management & Portfolio Optimization. Fractional Kelly on a
calibrated directional probability edge, hard-clamped by the paper per-order
ceiling — never negative, never above the cap.
"""

from __future__ import annotations

from engine.training.portfolio import (
    chainlink_freshness_penalty,
    fractional_kelly,
    kelly_size_usd,
    liquidity_adjusted_size,
    settlement_ambiguity_penalty,
)


def test_no_edge_yields_zero_fraction():
    assert fractional_kelly(0.50, 0.50) == 0.0       # p == price -> no edge
    assert fractional_kelly(0.40, 0.50) == 0.0       # p < price -> no bet
    assert fractional_kelly(0.7, 0.0) == 0.0         # degenerate price
    assert fractional_kelly(0.7, 1.0) == 0.0


def test_positive_edge_yields_positive_capped_fraction():
    f = fractional_kelly(0.70, 0.50, kelly_fraction=0.5, max_fraction=0.05)
    assert 0.0 < f <= 0.05


def test_fraction_is_monotonic_in_probability():
    lo = fractional_kelly(0.60, 0.50, kelly_fraction=1.0, max_fraction=1.0)
    hi = fractional_kelly(0.80, 0.50, kelly_fraction=1.0, max_fraction=1.0)
    assert hi > lo


def test_kelly_fraction_scales_size():
    full = fractional_kelly(0.80, 0.50, kelly_fraction=1.0, max_fraction=1.0)
    half = fractional_kelly(0.80, 0.50, kelly_fraction=0.5, max_fraction=1.0)
    assert abs(half - 0.5 * full) < 1e-9


def test_kelly_size_usd_capped_by_paper_ceiling():
    # large bankroll, but the hard per-order ceiling clamps the size
    size = kelly_size_usd(0.90, 0.50, bankroll=10_000.0, kelly_fraction=0.5,
                          max_fraction=0.5, max_size_usd=5.0)
    assert size == 5.0


def test_kelly_size_respects_max_fraction_before_ceiling():
    size = kelly_size_usd(0.90, 0.50, bankroll=100.0, kelly_fraction=1.0,
                          max_fraction=0.02, max_size_usd=50.0)
    assert size <= 0.02 * 100.0 + 1e-9          # fraction cap bites before USD cap


def test_liquidity_adjusted_size_caps_to_depth_fraction():
    assert liquidity_adjusted_size(100.0, 200.0, max_depth_fraction=0.35) == 70.0
    assert liquidity_adjusted_size(10.0, 200.0, max_depth_fraction=0.35) == 10.0


def test_chainlink_freshness_penalty_shrinks_size():
    assert chainlink_freshness_penalty(10.0, chainlink_no_trade=True) == 0.0
    fresh = chainlink_freshness_penalty(10.0, chainlink_confidence=1.0, weight=0.5)
    stale = chainlink_freshness_penalty(10.0, chainlink_confidence=0.2, weight=0.5)
    assert fresh == 10.0 and stale < fresh


def test_settlement_ambiguity_penalty_shrinks_size():
    clean = settlement_ambiguity_penalty(10.0, ambiguity=0.0, max_ambiguity=0.35)
    amb = settlement_ambiguity_penalty(10.0, ambiguity=0.3, max_ambiguity=0.35)
    assert clean == 10.0 and 0.0 <= amb < clean
