"""Bregman false-positive blocker — a non-arb can never be certified risk-free.

Quant scope — *Compliance/Security* + *Risk Management*: proves a group that
merely LOOKS cheap (asks sum < $1) is rejected unless it is a true full hedge
(mutually-exclusive + exhaustive) with positive worst-case PnL after costs, and
that a non-certified candidate can never reach trade approval (it may only be
logged as a candidate). PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.replay.metrics import bregman_certification_metrics
from engine.risk import bregman_trade_allowed


def _leg(mid, ask, **kw):
    return SimplexLeg(market_id=mid, outcome="YES", token_id=f"{mid}:YES", ask=ask,
                      bid=kw.get("bid", ask - 0.01), depth_usd=kw.get("depth", 5000.0),
                      tick_size=0.01, fresh_book=True, stale=False,
                      ambiguity_score=kw.get("amb", 0.0))


def _group(legs, *, exhaustive=True, me=True, gid="g"):
    return SimplexGroup(group_id=gid, group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=me, exhaustive=exhaustive)


def _engine():
    return BregmanArbitrageEngine(min_depth_usd=10.0, max_spread=0.10,
                                  slippage_bps=0.0, taker_fee_bps=0.0,
                                  target_capital_usd=100.0)


def test_cheap_but_non_exhaustive_group_is_not_certified():
    # asks sum to 0.90 (< $1) but the group is NOT a complete set -> NOT arb
    g = _group([_leg("m1", 0.45), _leg("m2", 0.45)], exhaustive=False)
    opp = _engine().certify(g)
    assert opp.certified is False and opp.is_opportunity is False
    assert opp.risk_free is False


def test_overpriced_complete_set_has_no_positive_edge():
    g = _group([_leg("m1", 0.55), _leg("m2", 0.55)])     # cost 1.10 > $1
    opp = _engine().certify(g)
    assert opp.certified is False
    assert "no_positive_edge" in opp.failure_modes or opp.no_trade_reason == "no_positive_edge"


def test_false_positive_rate_is_zero_across_a_mixed_batch():
    groups = [
        _group([_leg("a1", 0.45), _leg("a2", 0.45)], gid="arb"),            # true arb
        _group([_leg("b1", 0.45), _leg("b2", 0.45)], exhaustive=False, gid="ne"),  # not exhaustive
        _group([_leg("c1", 0.6), _leg("c2", 0.6)], gid="over"),             # overpriced
    ]
    certs = _engine().certify_all(groups)
    m = bregman_certification_metrics(certs)
    assert m["certified_count"] == 1
    assert m["rejected_count"] == 2
    assert m["false_positive_rate"] == 0.0     # no non-arb was ever certified


def test_non_certified_candidate_cannot_reach_trade_approval():
    bad = _engine().certify(_group([_leg("m1", 0.6), _leg("m2", 0.6)]))
    good = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    # outranks_directional only true once certified with positive lower bound
    assert BregmanArbitrageEngine.outranks_directional(bad) is False
    assert BregmanArbitrageEngine.outranks_directional(good) is True
    # the risk-layer trade gate refuses any non-certified opportunity
    assert bregman_trade_allowed(bad) is False
    assert bregman_trade_allowed(good) is True
