"""Bregman replay validation + fail-closed certification (TDD, deterministic).

Quant scope: Bregman arbitrage validation + Backtesting & Simulation + Risk
Management. Replay analytics over certified bundles, plus the hard invariant
that a stale book, missing leg, ambiguous settlement, wide spread, thin depth,
or stale Chainlink can NEVER create an approved (certified) opportunity.
"""

from __future__ import annotations

from engine.replay.metrics import bregman_replay_analytics
from engine.training.bregman import project_to_simplex
from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(o, ask, **kw):
    d = dict(market_id="m", outcome=o, token_id=f"t-{o}", ask=ask, depth_usd=10_000.0,
             fresh_book=True)
    d.update(kw)
    return SimplexLeg(**d)


def _group(legs, gid="g"):
    return SimplexGroup(group_id=gid, group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=True, exhaustive=True)


def _arb():
    return _group([_leg("A", 0.30), _leg("B", 0.30), _leg("C", 0.30)], gid="arb")


def test_replay_analytics_over_certified_bundles():
    eng = BregmanArbitrageEngine(slippage_bps=25.0)
    opps = [eng.certify(_arb()), eng.certify(_group(
        [_leg("A", 0.40), _leg("B", 0.40), _leg("C", 0.40)], gid="overround"))]
    a = bregman_replay_analytics([o.to_dict() for o in opps])
    assert a["opportunity_count"] == 1
    assert a["certified_profit"] > 0.0
    assert 0.0 <= a["all_leg_fill_feasibility"] <= 1.0
    assert 0.0 <= a["depth_decay"] <= 1.0
    assert a["capital_lock_duration"] > 0.0
    assert a["false_positive_check_passed"] is True
    assert "no_positive_edge" in a["rejected_opportunity_reasons"]


def test_no_false_positive_on_simplex():
    eng = BregmanArbitrageEngine(slippage_bps=0.0, taker_fee_bps=0.0)
    opps = [eng.certify(_group([_leg(f"O{i}", p) for i, p in enumerate(project_to_simplex(s))]))
            for s in ([0.5, 0.5], [0.3, 0.3, 0.4], [0.7, 0.3])]
    a = bregman_replay_analytics([o.to_dict() for o in opps])
    assert a["opportunity_count"] == 0
    assert a["false_positive_rate"] == 0.0


# --- fail-closed: each adverse condition must block certification ----------
def test_stale_book_never_certified():
    eng = BregmanArbitrageEngine()
    o = eng.certify(_group([_leg("A", 0.30, fresh_book=False, stale=True),
                            _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.certified and not o.is_opportunity and o.no_trade_reason == "stale_book"


def test_missing_leg_never_certified():
    eng = BregmanArbitrageEngine()
    o = eng.certify(_group([_leg("A", None), _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.is_opportunity and o.no_trade_reason == "no_executable_price"


def test_ambiguous_settlement_never_certified():
    eng = BregmanArbitrageEngine(max_ambiguity=0.35)
    o = eng.certify(_group([_leg("A", 0.30, ambiguity_score=0.9),
                            _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.is_opportunity and o.no_trade_reason == "settlement_ambiguity"


def test_wide_spread_never_certified():
    eng = BregmanArbitrageEngine(max_spread=0.08)
    o = eng.certify(_group([_leg("A", 0.30, bid=0.05), _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.is_opportunity and o.no_trade_reason == "spread_too_wide"


def test_thin_depth_never_certified():
    eng = BregmanArbitrageEngine(min_depth_usd=50.0)
    o = eng.certify(_group([_leg("A", 0.30, depth_usd=5.0), _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.is_opportunity and o.no_trade_reason == "depth_too_thin"


def test_stale_chainlink_never_certified():
    eng = BregmanArbitrageEngine()
    o = eng.certify(_group([_leg("A", 0.30, chainlink_no_trade=True),
                            _leg("B", 0.30), _leg("C", 0.30)]))
    assert not o.is_opportunity and o.no_trade_reason == "chainlink_stale_or_irrelevant"
