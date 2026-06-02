"""Institutional replay-metric extension: deterministic, divide-by-zero safe."""

from __future__ import annotations

import math

from engine.replay import metrics as m


def test_calmar_positive_for_up_curve():
    eq = [100, 102, 101, 105, 110]
    assert m.calmar(eq) > 0


def test_omega_and_profit_factor():
    assert m.omega([0.02, -0.01, 0.03, -0.01]) > 1.0
    assert m.profit_factor([5, -2, 3, -1]) == round(8 / 3, 6)
    assert m.profit_factor([1, 2, 3]) == float("inf")
    assert m.profit_factor([]) == 0.0


def test_expectancy_and_hit_rate():
    assert m.expectancy([2, -1, 3]) == round(4 / 3, 6)
    assert m.hit_rate([1, -1, 2, -3]) == 0.5
    assert m.hit_rate([]) == 0.0


def test_drawdown_duration_counts_underwater_steps():
    # peak at idx0 (100), underwater for 3 steps, recovers at idx4
    assert m.drawdown_duration([100, 98, 95, 99, 101]) == 3
    assert m.drawdown_duration([100, 101, 102]) == 0


def test_turnover_slippage_fee_drag():
    assert m.turnover(200.0, 100.0) == 2.0
    assert m.slippage_drag(2.0, 100.0) == 0.02
    assert m.fee_drag(1.0, 100.0) == 0.01
    assert m.turnover(10.0, 0.0) == 0.0          # divide-by-zero safe


def test_realized_and_expected_vs_realized_edge():
    trades = [{"realized_pnl": 0.5, "cost": 5.0, "net_edge": 0.08},
              {"realized_pnl": -0.25, "cost": 5.0, "net_edge": 0.06}]
    assert m.realized_edge(trades) == round(((0.1) + (-0.05)) / 2, 6)
    evr = m.expected_vs_realized_edge(trades)
    assert evr["expected"] == 0.07 and "gap" in evr


def test_probability_metrics_reward_good_predictions():
    good_p = [0.9, 0.1, 0.8, 0.2]
    bad_p = [0.5, 0.5, 0.5, 0.5]
    ys = [1, 0, 1, 0]
    assert m.brier_score(good_p, ys) < m.brier_score(bad_p, ys)
    assert m.log_loss(good_p, ys) < m.log_loss(bad_p, ys)
    assert 0.0 <= m.ece(good_p, ys) <= 1.0
    assert m.calibration_error(good_p, ys) == m.ece(good_p, ys)


def test_strategy_attribution_groups_pnl():
    trades = [{"category": "crypto", "realized_pnl": 1.0},
              {"category": "crypto", "realized_pnl": -0.5},
              {"category": "fx", "realized_pnl": 0.25}]
    attr = m.strategy_attribution(trades, key="category")
    assert attr["crypto"]["trades"] == 2 and attr["crypto"]["pnl"] == 0.5
    assert attr["fx"]["trades"] == 1


def test_institutional_metrics_aggregate_keys_and_safety():
    out = m.institutional_metrics(
        equities=[100, 101, 100.5, 103], decisions=20, rejections=12, explorations=3,
        trades=[{"realized_pnl": 1.0, "cost": 5.0, "net_edge": 0.05, "category": "crypto"},
                {"realized_pnl": -0.5, "cost": 5.0, "net_edge": 0.04, "category": "crypto"},
                {"realized_pnl": 2.5, "cost": 5.0, "net_edge": 0.06, "category": "crypto"}],
        predictions=[0.6, 0.4, 0.7], outcomes=[1, 0, 1],
        fees=0.3, slippage_cost=0.2, notional_traded=15.0)
    for k in ("sharpe", "sortino", "calmar", "omega", "expectancy", "hit_rate",
              "profit_factor", "turnover", "slippage_drag", "fee_drag",
              "drawdown_duration", "realized_edge", "expected_vs_realized_edge",
              "brier_score", "log_loss", "ece", "calibration_error", "trade_count",
              "decision_count", "rejection_rate", "exploration_rate",
              "strategy_attribution", "max_drawdown", "volatility"):
        assert k in out, k
    assert out["trade_count"] == 3 and out["decision_count"] == 20
    assert out["rejection_rate"] == round(12 / 20, 6)
    assert out["exploration_rate"] == round(3 / 3, 6)


def test_institutional_metrics_empty_is_safe():
    out = m.institutional_metrics()
    assert out["trade_count"] == 0 and out["sharpe"] == 0.0 and out["rejection_rate"] == 0.0
