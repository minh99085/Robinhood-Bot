"""Adaptive capital allocator — proven, calibrated, after-cost edge only.

Quant scope — *Risk Management & Portfolio Optimization* + *Compliance*: proves
the allocator funds only proven after-cost edge, routes candidates into the
correct capital bucket, refuses to grow capital for a negative-expectancy
strategy UNLESS it is explicitly tagged tiny exploration (and then only a capped
sliver), enforces portfolio exposure constraints, and emits a capital-allocation
report with expected return, expected shortfall / CVaR, concentration, capital
efficiency, and feedback per risk unit. PAPER ONLY — no live execution.
"""

from __future__ import annotations

import pytest

from engine.training.capital_allocator import (
    BUCKET_CHAINLINK, BUCKET_DIRECTIONAL, BUCKET_EXPLORATION, BUCKET_STATISTICAL,
    AdaptiveCapitalAllocator, CapitalCandidate, PortfolioConstraints,
    summarize_sizing_rejections)


def _alloc():
    return AdaptiveCapitalAllocator()


def _positive(strategy="directional", net=0.05, market="m1"):
    return CapitalCandidate(
        strategy=strategy, market_id=market, event_group="e1", cluster="c1",
        price=0.5, p_final=0.7, gross_edge=net, net_after_cost_edge=net,
        feedback_value=1.0)


def test_proven_after_cost_edge_is_funded():
    d = _alloc().allocate(_positive())
    assert d.approved is True
    assert d.notional_usd > 0.0
    assert 0.0 < d.haircut <= 1.0


def test_strategy_routes_to_bucket():
    a = _alloc()
    assert a.allocate(_positive("statistical_mispricing")).bucket == BUCKET_STATISTICAL
    assert a.allocate(_positive("chainlink_edge")).bucket == BUCKET_CHAINLINK
    assert a.allocate(_positive("directional")).bucket == BUCKET_DIRECTIONAL


def test_negative_expectancy_without_exploration_gets_zero():
    cand = _positive(net=-0.03)
    d = _alloc().allocate(cand)
    assert d.approved is False
    assert d.notional_usd == 0.0
    assert "expectancy" in d.reason.lower() or "edge" in d.reason.lower()


def test_negative_expectancy_with_exploration_gets_capped_sliver():
    cand = _positive(net=-0.03)
    cand.exploration = True
    d = _alloc().allocate(cand)
    assert d.approved is True
    assert d.bucket == BUCKET_EXPLORATION
    assert d.exploration is True
    # tiny + capped: must be no larger than the exploration cap
    assert 0.0 < d.notional_usd <= _alloc().exploration_notional_usd + 1e-9


def test_negative_expectancy_never_outsizes_a_positive_edge_trade():
    a = _alloc()
    pos = a.allocate(_positive(net=0.06))
    neg = _positive(net=-0.05)
    neg.exploration = True
    neg_dec = a.allocate(neg)
    assert neg_dec.notional_usd <= pos.notional_usd
    # the regression guard: a NON-explore negative-expectancy candidate gets nothing
    plain_neg = a.allocate(_positive(net=-0.05))
    assert plain_neg.notional_usd == 0.0


def test_portfolio_constraints_block_when_market_exposure_exceeded():
    cons = PortfolioConstraints(max_market_exposure_usd=5.0)
    a = AdaptiveCapitalAllocator(constraints=cons)
    d = a.allocate(_positive(), market_exposure=5.0)
    assert d.approved is False
    assert "exposure" in d.reason.lower() or "constraint" in d.reason.lower()


def test_portfolio_constraints_block_open_capital_lock():
    cons = PortfolioConstraints(max_open_capital_lock_usd=10.0)
    a = AdaptiveCapitalAllocator(constraints=cons)
    d = a.allocate(_positive(), open_capital_lock=10.0)
    assert d.approved is False
    assert "lock" in d.reason.lower() or "capital" in d.reason.lower()


def test_size_never_exceeds_hard_order_cap():
    a = AdaptiveCapitalAllocator(max_size_usd=5.0)
    d = a.allocate(_positive(net=0.2))  # huge edge
    assert d.notional_usd <= 5.0


def test_capital_allocation_report_shape():
    a = _alloc()
    decisions = [a.allocate(_positive(strategy="directional", market="m1")),
                 a.allocate(_positive(strategy="statistical_mispricing", market="m2")),
                 a.allocate(_positive(net=-0.04, market="m3"))]
    rep = a.capital_allocation_report(
        decisions, returns=[-0.02, 0.01, 0.03, -0.05, 0.02],
        equity_curve=[100, 101, 99, 102], feedback_events=4)
    for key in ("expected_return", "expected_shortfall", "cvar", "concentration",
                "capital_efficiency", "feedback_per_risk_unit", "bucket_allocations",
                "total_allocated", "rejected_sizing_reasons", "sharpe", "sortino",
                "calmar", "max_drawdown"):
        assert key in rep
    assert rep["cvar"] >= 0.0
    assert 0.0 <= rep["concentration"] <= 1.0
    assert rep["total_allocated"] >= 0.0


def test_summarize_sizing_rejections_counts_reasons():
    a = _alloc()
    decisions = [a.allocate(_positive(net=-0.04, market="m1")),
                 a.allocate(_positive(net=-0.04, market="m2")),
                 a.allocate(_positive(net=0.05, market="m3"))]
    reasons = summarize_sizing_rejections(decisions)
    assert isinstance(reasons, dict)
    assert sum(reasons.values()) == 2  # two negative-expectancy rejections


def test_no_negative_expectancy_strategy_is_ever_funded_in_batch():
    # Validation guard: across a mixed batch, NO approved decision may have a
    # non-positive after-cost edge unless it is in the tiny-exploration bucket.
    a = _alloc()
    cands = [_positive(net=0.05, market="p1"),
             _positive(net=-0.04, market="n1"),               # plain negative -> reject
             _positive(net=0.04, strategy="statistical_mispricing", market="p2")]
    explore = _positive(net=-0.06, market="x1")
    explore.exploration = True
    cands.append(explore)
    decisions = a.allocate_batch(cands)
    for d in decisions:
        if d.approved and d.notional_usd > 0.0:
            assert (d.net_after_cost_edge > 0.0) or (d.exploration and d.bucket == BUCKET_EXPLORATION)
    rep = a.capital_allocation_report(decisions)
    assert rep["sharpe"] == rep["sharpe"]  # not NaN


def test_drawdown_downgrade_blocks_all_allocation():
    a = _alloc()
    d = a.allocate(_positive(net=0.06), drawdown=999.0, max_drawdown_usd=50.0)
    assert d.approved is False
    assert d.notional_usd == 0.0
