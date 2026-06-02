"""Bregman settlement consistency — ambiguous / incomplete resolution blocks arb.

Quant scope — *Data Preprocessing & Feature Engineering* + *Compliance/Security*:
proves that a certified full hedge requires settlement consistency — the group
must be mutually-exclusive + exhaustive (exactly one leg pays) and every leg's
settlement-ambiguity score must be below the cap. An ambiguous or incomplete
group is never certified risk-free. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(mid, ask, *, amb=0.0):
    return SimplexLeg(market_id=mid, outcome="YES", token_id=f"{mid}:YES", ask=ask,
                      bid=ask - 0.01, depth_usd=5000.0, tick_size=0.01,
                      fresh_book=True, stale=False, ambiguity_score=amb)


def _group(legs, *, exhaustive=True, me=True):
    return SimplexGroup(group_id="g", group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=me, exhaustive=exhaustive)


def _engine(**kw):
    base = dict(min_depth_usd=10.0, max_spread=0.10, max_ambiguity=0.35,
                slippage_bps=0.0, taker_fee_bps=0.0, target_capital_usd=100.0)
    base.update(kw)
    return BregmanArbitrageEngine(**base)


def test_ambiguous_leg_blocks_certification_and_is_scored():
    g = _group([_leg("m1", 0.45, amb=0.9), _leg("m2", 0.45)])
    opp = _engine().certify(g)
    assert opp.certified is False and opp.risk_free is False
    assert "settlement_ambiguity" in opp.failure_modes or \
        opp.no_trade_reason == "settlement_ambiguity"
    assert opp.certificate.settlement_ambiguity_score == pytest.approx(0.9)
    assert opp.certificate.settlement_consistent is False


def test_clean_group_is_settlement_consistent():
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)]))
    assert opp.certificate.settlement_consistent is True
    assert opp.certificate.settlement_ambiguity_score == pytest.approx(0.0)


def test_non_exhaustive_group_is_not_settlement_consistent():
    # mutually-exclusive but NOT exhaustive: no guarantee exactly one leg pays $1
    opp = _engine().certify(_group([_leg("m1", 0.45), _leg("m2", 0.45)], exhaustive=False))
    assert opp.certified is False
    assert opp.certificate.settlement_consistent is False


def test_ambiguity_just_under_cap_still_certifies():
    opp = _engine(max_ambiguity=0.35).certify(
        _group([_leg("m1", 0.45, amb=0.30), _leg("m2", 0.45, amb=0.20)]))
    assert opp.certified is True and opp.risk_free is True
    assert opp.certificate.settlement_ambiguity_score == pytest.approx(0.30)
    assert opp.certificate.settlement_consistent is True
