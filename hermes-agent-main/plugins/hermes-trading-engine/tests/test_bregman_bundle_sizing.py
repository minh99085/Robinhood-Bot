"""Bregman bundle capital allocation (TDD, deterministic, offline).

Quant scope: Bregman arbitrage capital allocation + Risk Management. Sizes a
certified bundle from its worst-case PnL, all-leg depth, fill feasibility,
capital lock, and a leg-failure haircut. Un-certified opportunities get zero.
"""

from __future__ import annotations

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.training.portfolio import bregman_bundle_size


def _opp(asks=(0.30, 0.30, 0.30), depth=10_000.0):
    legs = [SimplexLeg(market_id="m", outcome=f"O{i}", token_id=f"t{i}", ask=a,
                       depth_usd=depth, fresh_book=True) for i, a in enumerate(asks)]
    grp = SimplexGroup(group_id="event:e", group_type="exhaustive_event", legs=legs,
                       mutually_exclusive=True, exhaustive=True)
    return BregmanArbitrageEngine(slippage_bps=25.0).certify(grp)


def test_certified_bundle_sizes_within_budget():
    s = bregman_bundle_size(_opp(), bankroll=500.0, max_bundle_usd=10.0)
    assert s["tradable"] is True
    assert s["sets"] > 0.0
    assert s["required_capital"] <= 10.0 + 1e-9
    assert s["capital_locked"] == s["required_capital"]
    assert s["expected_profit"] > 0.0
    assert s["worst_case_pnl"] > 0.0
    assert len(s["per_leg_notional"]) == 3


def test_leg_failure_haircut_is_applied():
    s = bregman_bundle_size(_opp(), bankroll=500.0, max_bundle_usd=10.0,
                            leg_failure_haircut=0.5)
    worst_leg = max(s["per_leg_notional"])
    assert abs(s["worst_case_leg_failure"] - 0.5 * worst_leg) < 1e-6
    assert s["worst_case_leg_failure"] > 0.0


def test_smaller_budget_reduces_sets():
    big = bregman_bundle_size(_opp(), bankroll=500.0, max_bundle_usd=50.0)
    small = bregman_bundle_size(_opp(), bankroll=500.0, max_bundle_usd=1.0)
    assert small["sets"] < big["sets"]
    assert small["required_capital"] <= 1.0 + 1e-9


def test_depth_limits_bundle_size():
    thin = bregman_bundle_size(_opp(depth=2.0), bankroll=500.0, max_bundle_usd=50.0)
    deep = bregman_bundle_size(_opp(depth=10_000.0), bankroll=500.0, max_bundle_usd=50.0)
    assert thin["sets"] < deep["sets"]


def test_uncertified_opportunity_gets_zero_size():
    over_round = _opp(asks=(0.40, 0.40, 0.40))        # sum 1.20 -> no positive edge
    s = bregman_bundle_size(over_round, bankroll=500.0, max_bundle_usd=10.0)
    assert s["tradable"] is False
    assert s["sets"] == 0.0
    assert s["required_capital"] == 0.0


def test_bankroll_constrains_size():
    s = bregman_bundle_size(_opp(), bankroll=0.5, max_bundle_usd=50.0)
    assert s["required_capital"] <= 0.5 + 1e-9
