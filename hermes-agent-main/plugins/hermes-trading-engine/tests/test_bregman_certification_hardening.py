"""Institutional-grade Bregman certification hardening.

Quant scope — *Signal Generation (Bregman priority)* + *CLOB v2 Execution* +
*Risk Management* + *Strategy Optimization & Robustness Testing*: proves the
Bregman certificate captures the full cost/feasibility decomposition (fee /
spread / slippage / tick-rounding drag, depth sufficiency, fill probability,
stale-book + settlement-ambiguity scores, failure modes) and that an opportunity
is only ``risk_free`` when full hedge + all-leg executability + positive
worst-case PnL + settlement consistency are all proven. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.bregman_execution import (
    BregmanArbitrageEngine, BregmanCertificate, FAILURE_MODES)
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(mid, ask, *, bid=None, depth=5000.0, tick=0.01, fresh=True, amb=0.0,
         accepting=True, outcome="YES"):
    return SimplexLeg(market_id=mid, outcome=outcome, token_id=f"{mid}:{outcome}",
                      ask=ask, bid=bid if bid is not None else (ask - 0.01),
                      depth_usd=depth, tick_size=tick, fresh_book=fresh,
                      stale=not fresh, ambiguity_score=amb, accepting_orders=accepting)


def _group(legs, *, exhaustive=True, me=True, gid="g1", gtype="exhaustive_event"):
    return SimplexGroup(group_id=gid, group_type=gtype, legs=legs,
                        mutually_exclusive=me, exhaustive=exhaustive)


def _engine(**kw):
    base = dict(min_depth_usd=10.0, max_spread=0.10, max_ambiguity=0.35,
                slippage_bps=0.0, taker_fee_bps=0.0, target_capital_usd=100.0)
    base.update(kw)
    return BregmanArbitrageEngine(**base)


# --------------------------------------------------------------------------- #
# certificate shape
# --------------------------------------------------------------------------- #
def test_clean_exhaustive_group_is_certified_risk_free_with_full_certificate():
    g = _group([_leg("m1", 0.45), _leg("m2", 0.45)])
    opp = _engine().certify(g)
    assert opp.certified is True and opp.risk_free is True
    assert opp.profit_lower_bound > 0.0 and opp.worst_case_pnl > 0.0
    cert = opp.certificate
    assert isinstance(cert, BregmanCertificate)
    assert cert.market_set == ["m1", "m2"]
    assert cert.outcome_legs == ["m1:YES", "m2:YES"]
    for k in ("fee_drag", "spread_drag", "slippage_drag", "tick_rounding_drag",
              "depth_sufficiency", "fill_probability", "stale_book_score",
              "settlement_ambiguity_score", "required_capital", "worst_case_pnl",
              "size"):
        assert hasattr(cert, k)
    assert cert.settlement_consistent is True
    assert cert.failure_modes == []


def test_cost_drags_are_decomposed_and_positive_under_costs():
    # misaligned ask + non-zero fees/slippage -> every drag component positive
    g = _group([_leg("m1", 0.453, tick=0.01), _leg("m2", 0.401, tick=0.01)])
    opp = _engine(slippage_bps=50.0, taker_fee_bps=20.0).certify(g)
    cert = opp.certificate
    assert cert.tick_rounding_drag > 0.0   # 0.453 -> 0.46, 0.401 -> 0.41
    assert cert.slippage_drag > 0.0
    assert cert.fee_drag > 0.0
    assert 0.0 < cert.depth_sufficiency <= 1.0
    assert 0.0 <= cert.fill_probability <= 1.0


# --------------------------------------------------------------------------- #
# adversarial stress cases -> each is a distinct failure mode (NOT risk-free)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mutate,reason", [
    (lambda g: setattr(g.legs[0], "ask", None), "no_executable_price"),
    (lambda g: (setattr(g.legs[0], "fresh_book", False),
                setattr(g.legs[0], "stale", True)), "stale_book"),
    (lambda g: setattr(g.legs[0], "depth_usd", 1.0), "depth_too_thin"),
    (lambda g: (setattr(g.legs[0], "ask", 0.45), setattr(g.legs[0], "bid", 0.10)),
     "spread_too_wide"),
    (lambda g: setattr(g.legs[0], "ambiguity_score", 0.9), "settlement_ambiguity"),
    (lambda g: setattr(g.legs[0], "tick_size_dirty", True), "tick_size_changed"),
    (lambda g: setattr(g.legs[0], "accepting_orders", False), "market_closed"),
])
def test_adversarial_stress_rejects_with_failure_mode(mutate, reason):
    g = _group([_leg("m1", 0.45), _leg("m2", 0.45)])
    mutate(g)
    opp = _engine(min_depth_usd=10.0, max_spread=0.10).certify(g)
    assert opp.certified is False and opp.risk_free is False
    assert reason in opp.failure_modes or opp.no_trade_reason == reason
    assert reason in FAILURE_MODES


def test_fee_increase_can_flip_a_thin_edge_to_no_edge():
    g = _group([_leg("m1", 0.49), _leg("m2", 0.49)])     # cost 0.98 -> thin edge
    assert _engine(taker_fee_bps=0.0).certify(g).certified is True
    # a large fee increase erases the edge -> not certified
    opp = _engine(taker_fee_bps=300.0).certify(g)
    assert opp.certified is False
    assert "no_positive_edge" in opp.failure_modes or opp.no_trade_reason == "no_positive_edge"
