"""Bregman no-false-positive guarantees (deterministic, offline).

Quant scope: Strategy Optimization & Robustness Testing — prices that already
lie on the valid probability simplex (or that only appear profitable before
costs) must NEVER be certified as opportunities. Reports a 0.0 false-positive
rate across a deterministic battery of on-simplex price vectors.
"""

from __future__ import annotations

from engine.training.bregman import divergence_gap, project_to_simplex
from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _group(prices, *, depth=10_000.0):
    legs = [SimplexLeg(market_id="m", outcome=f"O{i}", token_id=f"t{i}", ask=p,
                       depth_usd=depth, fresh_book=True)
            for i, p in enumerate(prices)]
    return SimplexGroup(group_id="g", group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=True, exhaustive=True)


def test_on_simplex_prices_have_zero_gap_and_no_opportunity():
    eng = BregmanArbitrageEngine(slippage_bps=0.0, taker_fee_bps=0.0)
    for prices in ([0.5, 0.5], [0.3, 0.3, 0.4], [0.25, 0.25, 0.25, 0.25]):
        assert divergence_gap(prices) < 1e-9
        opp = eng.certify(_group(prices))
        assert opp.is_opportunity is False
        assert opp.no_trade_reason == "no_positive_edge"
        assert opp.certified is False


def test_overround_book_is_not_an_opportunity():
    # sum > 1 (the house edge / over-round) is never a buy-set arb
    eng = BregmanArbitrageEngine(slippage_bps=0.0)
    opp = eng.certify(_group([0.55, 0.55]))
    assert not opp.is_opportunity and opp.no_trade_reason == "no_positive_edge"


def test_thin_edge_eaten_by_costs_is_rejected():
    # sum 0.999 looks profitable raw, but fees+slippage erase it
    eng = BregmanArbitrageEngine(slippage_bps=25.0, taker_fee_bps=10.0)
    opp = eng.certify(_group([0.333, 0.333, 0.333]))
    assert not opp.is_opportunity
    assert opp.no_trade_reason == "no_positive_edge"


def test_false_positive_rate_is_zero_on_simplex_battery():
    """Deterministic battery: project arbitrary vectors onto the simplex (so each
    sums to exactly 1) and confirm NONE are certified as opportunities."""
    eng = BregmanArbitrageEngine(slippage_bps=10.0, taker_fee_bps=5.0)
    seeds = [
        [0.9, 0.05, 0.05], [0.1, 0.2, 0.7], [0.4, 0.4, 0.2], [0.33, 0.33, 0.34],
        [0.6, 0.1, 0.1, 0.2], [0.5, 0.5], [0.7, 0.3], [0.2, 0.2, 0.2, 0.2, 0.2],
        [0.8, 0.15, 0.05], [0.45, 0.55],
    ]
    false_positives = 0
    for seed in seeds:
        on_simplex = project_to_simplex(seed)          # sums to exactly 1.0
        opp = eng.certify(_group(on_simplex))
        if opp.is_opportunity:
            false_positives += 1
    assert false_positives == 0, f"false positives on the simplex: {false_positives}"


def test_just_below_one_with_zero_costs_is_break_even_not_opportunity():
    # exactly on simplex with zero costs -> profit per set == 0 -> not tradable
    eng = BregmanArbitrageEngine(slippage_bps=0.0, taker_fee_bps=0.0)
    opp = eng.certify(_group([0.5, 0.5]))
    assert opp.profit_lower_bound == 0.0
    assert not opp.is_opportunity
