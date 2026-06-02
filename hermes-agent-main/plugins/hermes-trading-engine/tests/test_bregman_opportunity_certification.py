"""Bregman opportunity certification + stress tests (deterministic, offline).

Quant scope: Risk Management, Execution CLOB v2 simulation, Strategy
Optimization & Robustness Testing. Verifies that ONLY fully-hedged, all-leg
executable opportunities with a positive worst-case PnL after all costs are
certified, and that every adverse condition (stale book, missing leg, partial
depth, wide spread, tick-size change, high slippage, ambiguous settlement, stale
Chainlink) is rejected with the right reason. PAPER ONLY.
"""

from __future__ import annotations

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(outcome, ask, *, token=None, depth=10_000.0, bid=None, fresh=True,
         stale=False, tick=0.001, tick_dirty=False, ambiguity=0.0,
         cl_no_trade=False, cl_relevant=True):
    return SimplexLeg(
        market_id="m", outcome=outcome, token_id=token or f"tok-{outcome}",
        ask=ask, bid=bid, depth_usd=depth, tick_size=tick, fresh_book=fresh,
        stale=stale, tick_size_dirty=tick_dirty, ambiguity_score=ambiguity,
        chainlink_no_trade=cl_no_trade, chainlink_relevant=cl_relevant)


def _group(legs, *, exhaustive=True, me=True, gid="g", gtype="exhaustive_event"):
    return SimplexGroup(group_id=gid, group_type=gtype, legs=legs,
                        mutually_exclusive=me, exhaustive=exhaustive)


def _arb_group():
    # three exhaustive outcomes whose asks sum to 0.90 -> a clean buy-set arb
    return _group([_leg("A", 0.30, token="a"), _leg("B", 0.30, token="b"),
                   _leg("C", 0.30, token="c")])


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_clean_arbitrage_is_certified_and_risk_free():
    eng = BregmanArbitrageEngine(slippage_bps=25.0, taker_fee_bps=0.0)
    opp = eng.certify(_arb_group())
    assert opp.certified is True
    assert opp.risk_free is True
    assert opp.is_opportunity is True
    assert opp.profit_lower_bound > 0.0
    assert opp.worst_case_pnl > 0.0
    assert opp.required_capital > 0.0
    assert len(opp.legs) == 3 and len(opp.quantities) == 3
    assert opp.no_trade_reason == ""
    assert opp.fill_feasibility == 1.0
    assert 0.0 <= opp.persistence_score <= 1.0
    assert opp.divergence_gap > 0.0       # off the simplex (sum 0.90)


def test_executable_prices_are_conservative_after_costs():
    eng = BregmanArbitrageEngine(slippage_bps=25.0, taker_fee_bps=10.0)
    opp = eng.certify(_arb_group())
    # every executable price is >= the raw ask (costs only ever make it worse)
    assert all(px >= 0.30 - 1e-12 for px in opp.executable_prices)
    assert opp.cost_per_set > 0.90


# --------------------------------------------------------------------------- #
# stress: each adverse condition rejects with the right reason
# --------------------------------------------------------------------------- #
def test_stale_book_rejected():
    eng = BregmanArbitrageEngine()
    legs = [_leg("A", 0.30, token="a", fresh=False, stale=True),
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    opp = eng.certify(_group(legs))
    assert not opp.certified and opp.no_trade_reason == "stale_book"
    assert not opp.risk_free


def test_missing_leg_rejected():
    eng = BregmanArbitrageEngine()
    legs = [_leg("A", None, token="a"), _leg("B", 0.30, token="b"),
            _leg("C", 0.30, token="c")]
    opp = eng.certify(_group(legs))
    assert not opp.certified and opp.no_trade_reason == "no_executable_price"
    assert "missing_leg" in opp.failure_modes


def test_tick_size_change_rejected():
    eng = BregmanArbitrageEngine()
    legs = [_leg("A", 0.30, token="a", tick_dirty=True),
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    assert eng.certify(_group(legs)).no_trade_reason == "tick_size_changed"


def test_wide_spread_rejected():
    eng = BregmanArbitrageEngine(max_spread=0.08)
    legs = [_leg("A", 0.30, token="a", bid=0.10),       # spread 0.20
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    assert eng.certify(_group(legs)).no_trade_reason == "spread_too_wide"


def test_thin_depth_rejected():
    eng = BregmanArbitrageEngine(min_depth_usd=50.0)
    legs = [_leg("A", 0.30, token="a", depth=5.0),       # below floor
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    assert eng.certify(_group(legs)).no_trade_reason == "depth_too_thin"


def test_partial_depth_reduces_feasibility_but_can_certify():
    # depth above the floor but small -> fewer sets, fill_feasibility < 1
    eng = BregmanArbitrageEngine(min_depth_usd=1.0, target_capital_usd=100.0,
                                 slippage_bps=25.0)
    legs = [_leg("A", 0.30, token="a", depth=2.0),
            _leg("B", 0.30, token="b", depth=2.0),
            _leg("C", 0.30, token="c", depth=2.0)]
    opp = eng.certify(_group(legs))
    assert opp.certified is True
    assert 0.0 < opp.fill_feasibility < 1.0
    assert opp.profit_lower_bound > 0.0


def test_high_slippage_eliminates_edge():
    # 40% slippage pushes the buy-set cost above $1 -> no positive edge
    eng = BregmanArbitrageEngine(slippage_bps=4000.0)
    opp = eng.certify(_arb_group())
    assert not opp.certified and opp.no_trade_reason == "no_positive_edge"
    assert not opp.risk_free


def test_settlement_ambiguity_rejected():
    eng = BregmanArbitrageEngine(max_ambiguity=0.35)
    legs = [_leg("A", 0.30, token="a", ambiguity=0.9),
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    assert eng.certify(_group(legs)).no_trade_reason == "settlement_ambiguity"


def test_stale_chainlink_rejected():
    eng = BregmanArbitrageEngine()
    legs = [_leg("A", 0.30, token="a", cl_no_trade=True),
            _leg("B", 0.30, token="b"), _leg("C", 0.30, token="c")]
    assert eng.certify(_group(legs)).no_trade_reason == "chainlink_stale_or_irrelevant"


# --------------------------------------------------------------------------- #
# hedge / ranking invariants
# --------------------------------------------------------------------------- #
def test_non_exhaustive_group_not_certified_not_risk_free():
    eng = BregmanArbitrageEngine()
    legs = [_leg("A", 0.30, token="a"), _leg("B", 0.30, token="b")]
    opp = eng.certify(_group(legs, exhaustive=False))
    assert not opp.certified and not opp.risk_free


def test_bregman_outranks_directional_only_when_profit_positive():
    eng = BregmanArbitrageEngine(slippage_bps=25.0)
    good = eng.certify(_arb_group())
    bad = eng.certify(_group([_leg("A", 0.40, token="a"), _leg("B", 0.40, token="b"),
                              _leg("C", 0.40, token="c")]))   # sum 1.20 -> no edge
    assert BregmanArbitrageEngine.outranks_directional(good, directional_net_edge=0.05)
    assert not BregmanArbitrageEngine.outranks_directional(bad, directional_net_edge=0.0)


def test_scan_returns_only_tradable_sorted_by_profit():
    eng = BregmanArbitrageEngine(slippage_bps=25.0)
    big = _group([_leg("A", 0.20, token="a"), _leg("B", 0.20, token="b"),
                  _leg("C", 0.20, token="c")], gid="big")     # sum 0.60 (big edge)
    small = _group([_leg("A", 0.31, token="a2"), _leg("B", 0.31, token="b2"),
                    _leg("C", 0.31, token="c2")], gid="small")  # sum 0.93 (small edge)
    none = _group([_leg("A", 0.40, token="a3"), _leg("B", 0.40, token="b3"),
                   _leg("C", 0.40, token="c3")], gid="none")    # sum 1.20 (no edge)
    opps = eng.scan([small, none, big])
    assert [o.group_id for o in opps] == ["big", "small"]      # sorted desc, none dropped
