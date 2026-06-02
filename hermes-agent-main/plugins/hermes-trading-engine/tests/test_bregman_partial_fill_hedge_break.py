"""Bregman partial-fill hedge-break — a hedge that can break is not risk-free.

Quant scope — *CLOB v2 Execution* + *Backtesting & Simulation* + *Risk
Management*: proves that a certified "buy the complete set" hedge realizes its
profit ONLY when every leg fully fills; a partial / delayed fill breaks the hedge
into an unhedged basket whose worst case is a loss, and the certification removes
the ``risk_free`` label when the all-leg fill probability is too low. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.bregman_execution import (
    BregmanArbitrageEngine, BregmanBundleExecutionSimulator)
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.replay.metrics import bregman_certification_metrics


def _leg(mid, ask, depth=5000.0):
    return SimplexLeg(market_id=mid, outcome="YES", token_id=f"{mid}:YES", ask=ask,
                      bid=ask - 0.01, depth_usd=depth, tick_size=0.01,
                      fresh_book=True, stale=False)


def _group(legs):
    return SimplexGroup(group_id="g", group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=True, exhaustive=True)


def _engine():
    return BregmanArbitrageEngine(min_depth_usd=10.0, max_spread=0.10,
                                  slippage_bps=0.0, taker_fee_bps=0.0,
                                  target_capital_usd=100.0)


class _LowFill:
    """Fill model whose all-leg fill probability is far below the floor."""

    def fill_probability(self, **kw):
        return 0.3

    def fill_fraction(self, **kw):
        return 1.0


def test_full_fill_realizes_certified_profit():
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    sim = BregmanBundleExecutionSimulator()
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 1.0])
    assert res.fully_hedged is True and res.failure_mode == ""
    assert res.realized_pnl == pytest.approx(opp.worst_case_pnl, rel=1e-6)


def test_partial_fill_breaks_hedge_into_a_loss():
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    sim = BregmanBundleExecutionSimulator()
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 0.5])
    assert res.fully_hedged is False
    assert res.failure_mode == "partial_fill_breaks_hedge"
    assert res.realized_pnl < 0.0


def test_delayed_fill_times_out_and_breaks_hedge():
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    sim = BregmanBundleExecutionSimulator(timeout_ms=1000)
    res = sim.simulate(opp, leg_latencies_ms=[200, 5000])
    assert res.timed_out is True and res.fully_hedged is False
    assert res.realized_pnl < 0.0


def test_low_all_leg_fill_probability_removes_risk_free_label():
    g = _group([_leg("m1", 0.45), _leg("m2", 0.45)])
    opp = _engine().certify(g, fill_model=_LowFill(), min_all_leg_fill_prob=0.95)
    assert opp.risk_free is False
    assert "partial_fill_breaks_hedge" in opp.failure_modes


def test_hedge_break_rate_metric_counts_broken_bundles():
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    sim = BregmanBundleExecutionSimulator()
    full = sim.simulate(opp, leg_fill_fractions=[1.0, 1.0])
    broken = sim.simulate(opp, leg_fill_fractions=[1.0, 0.0])
    m = bregman_certification_metrics([opp], bundles=[full, broken])
    assert m["hedge_break_rate"] == pytest.approx(0.5)
