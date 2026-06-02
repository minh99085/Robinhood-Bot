"""Profitability truth governor — gross edge vs after-cost reality.

Quant scope — *Risk Management & Portfolio Optimization* + *Backtesting &
Simulation* + *Compliance/Security*: proves the truth report separates gross
edge from fees, spread, slippage, fill failure, adverse selection, label
ambiguity, and timing decay, and that the governor only marks a market live-ready
when after-cost edge survives. PAPER ONLY — produces verdicts, never trades.
"""

from __future__ import annotations

import pytest

from engine.training.profitability_governor import (
    ProfitabilityGovernor, after_cost_edge, profitability_score,
    profitability_truth_report)


def _costs(**kw):
    base = dict(fee=0.001, spread=0.004, slippage=0.0025, ambiguity=0.0,
                stale=0.0, evidence=0.0, calibration=0.0, liquidity=0.0)
    base.update(kw)
    return base


def test_after_cost_edge_decomposes_and_nets():
    ac = after_cost_edge(0.05, _costs(), fill_failure=0.002,
                         adverse_selection=0.003, timing_decay=0.001)
    for k in ("gross", "fees", "spread", "slippage", "fill_failure",
              "adverse_selection", "label_ambiguity", "timing_decay",
              "total_cost", "net_edge"):
        assert k in ac
    assert ac["gross"] == pytest.approx(0.05)
    assert ac["net_edge"] == pytest.approx(ac["gross"] - ac["total_cost"])
    assert ac["net_edge"] < ac["gross"]


def test_high_gross_but_high_cost_is_not_profitable():
    ac = after_cost_edge(0.03, _costs(spread=0.02, slippage=0.015, ambiguity=0.01),
                         fill_failure=0.005, adverse_selection=0.004)
    assert ac["net_edge"] <= 0.0
    assert profitability_score(ac["net_edge"]) < 0.5


def test_profitability_score_monotone_around_zero():
    assert profitability_score(-0.02) < profitability_score(0.0) < profitability_score(0.02)
    assert 0.0 <= profitability_score(-1.0) <= profitability_score(1.0) <= 1.0


def test_truth_report_aggregates_components():
    trades = [
        {"gross_edge": 0.05, "cost_components": _costs(), "fill_failure": 0.001,
         "adverse_selection": 0.002, "timing_decay": 0.0},
        {"gross_edge": 0.02, "cost_components": _costs(spread=0.02, slippage=0.01),
         "fill_failure": 0.004, "adverse_selection": 0.005, "timing_decay": 0.002},
    ]
    rep = profitability_truth_report(trades)
    assert rep["n"] == 2
    for k in ("gross_edge", "fees", "spread", "slippage", "fill_failure",
              "adverse_selection", "label_ambiguity", "timing_decay",
              "total_cost", "net_edge"):
        assert k in rep
    # net edge after every cost bucket
    assert rep["net_edge"] == pytest.approx(rep["gross_edge"] - rep["total_cost"], abs=1e-9)


def test_governor_marks_positive_market_live_ready_and_negative_not():
    gov = ProfitabilityGovernor()
    good = gov.evaluate(market_id="m_good", strategy="bregman", gross_edge=0.06,
                        cost_components=_costs(), liquidity_usd=50000.0, spread=0.01,
                        market_type="binary", time_to_resolution_s=7 * 86400.0)
    bad = gov.evaluate(market_id="m_bad", strategy="directional", gross_edge=0.02,
                       cost_components=_costs(spread=0.02, slippage=0.02, ambiguity=0.01),
                       liquidity_usd=200.0, spread=0.09, market_type="binary",
                       time_to_resolution_s=300.0)
    assert good.live_ready is True and good.after_cost["net_edge"] > 0
    assert bad.live_ready is False and bad.after_cost["net_edge"] <= 0
