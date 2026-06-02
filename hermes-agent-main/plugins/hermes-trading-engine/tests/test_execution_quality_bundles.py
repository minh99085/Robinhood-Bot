"""Simulated CLOB execution-quality metrics (TDD, deterministic, offline).

Quant scope: Execution Engine CLOB v2 simulation. Queue-position approximation,
fill probability, slippage forecast, spread-blowout detection, partial-fill
risk, markout by horizon, and Bregman-bundle execution quality.
"""

from __future__ import annotations

from engine.training.execution_quality import (
    bundle_execution_quality,
    fill_probability,
    markout_by_horizon,
    partial_fill_risk,
    queue_position_approximation,
    slippage_forecast,
    spread_blowout,
)


def test_queue_position_front_and_back():
    assert queue_position_approximation(0.0, 10.0) == 0.0          # nobody ahead
    assert queue_position_approximation(90.0, 10.0) == 0.9         # mostly behind
    assert 0.0 <= queue_position_approximation(50.0, 10.0, refreshed_depth=40.0) <= 1.0


def test_fill_probability_falls_with_spread_and_size_and_stale():
    tight = fill_probability(0.01, depth_usd=1000.0, order_usd=10.0)
    wide = fill_probability(0.07, depth_usd=1000.0, order_usd=10.0)
    big = fill_probability(0.01, depth_usd=1000.0, order_usd=2000.0)
    assert 0.0 < wide < tight <= 1.0
    assert big < tight
    assert fill_probability(0.01, depth_usd=1000.0, order_usd=10.0, stale=True) == 0.0


def test_slippage_forecast_grows_with_size():
    small = slippage_forecast(10.0, depth_usd=1000.0, base_bps=25.0, impact_coeff=100.0)
    large = slippage_forecast(500.0, depth_usd=1000.0, base_bps=25.0, impact_coeff=100.0)
    assert large > small >= 25.0


def test_spread_blowout_detection():
    assert spread_blowout(0.30, 0.05, factor=3.0) is True
    assert spread_blowout(0.10, 0.05, factor=3.0) is False


def test_partial_fill_risk():
    assert partial_fill_risk(10.0, depth_usd=1000.0, max_depth_fraction=0.35) == 0.0
    risk = partial_fill_risk(1000.0, depth_usd=1000.0, max_depth_fraction=0.35)
    assert 0.0 < risk <= 1.0


def test_markout_by_horizon_signs():
    buy = markout_by_horizon(0.50, {"5s": 0.52, "60s": 0.48, "5m": None}, side="BUY")
    assert buy["5s"] == 0.02 and buy["60s"] == -0.02 and buy["5m"] is None
    sell = markout_by_horizon(0.50, {"5s": 0.48}, side="SELL")
    assert sell["5s"] == 0.02


def test_bundle_execution_quality_aggregates_legs():
    legs = [{"spread": 0.01, "depth_usd": 1000.0, "order_usd": 30.0, "baseline_spread": 0.01},
            {"spread": 0.02, "depth_usd": 1000.0, "order_usd": 30.0, "baseline_spread": 0.01},
            {"spread": 0.01, "depth_usd": 1000.0, "order_usd": 30.0, "baseline_spread": 0.01}]
    q = bundle_execution_quality(legs)
    assert 0.0 < q["all_leg_fill_probability"] <= 1.0
    assert q["worst_slippage_bps"] >= 25.0
    assert q["spread_blowout"] is False
    assert 0.0 < q["overall_quality"] <= 1.0
    assert q["leg_count"] == 3


def test_bundle_quality_zero_when_a_leg_is_stale_or_blown_out():
    stale_leg = [{"spread": 0.01, "depth_usd": 1000.0, "order_usd": 30.0, "stale": True},
                 {"spread": 0.01, "depth_usd": 1000.0, "order_usd": 30.0}]
    assert bundle_execution_quality(stale_leg)["all_leg_fill_probability"] == 0.0
    blown = [{"spread": 0.30, "depth_usd": 1000.0, "order_usd": 30.0, "baseline_spread": 0.02},
             {"spread": 0.01, "depth_usd": 1000.0, "order_usd": 30.0, "baseline_spread": 0.01}]
    q = bundle_execution_quality(blown)
    assert q["spread_blowout"] is True and q["overall_quality"] == 0.0


def test_bundle_quality_empty():
    q = bundle_execution_quality([])
    assert q["overall_quality"] == 0.0 and q["leg_count"] == 0
