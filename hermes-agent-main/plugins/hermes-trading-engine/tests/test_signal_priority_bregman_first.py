"""Hierarchical signal priority: Bregman > statistical > directional (TDD).

Quant scope: Bregman arbitrage priority + Signal Generation & Strategy
Development + Strategy Optimization. Verifies the resolver always ranks a
certified Bregman bundle first, calibrated statistical mispricing second, and
directional predictive trading third — and that Bregman only outranks
directional when its certified profit lower bound is positive.
"""

from __future__ import annotations

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.training.edge_engine import EdgeResult
from engine.training.probability_stack import ProbabilityEstimate
from engine.training.signal_resolver import (
    STRATEGY_PRIORITIES,
    SignalResolver,
    rank_signals,
    select_best,
)


def _est(*, mid=0.50, p_model=0.50, p_research=0.50, p_final=0.50, research_usable=True,
         model_has_edge=False, calibrated=None, fresh=True, spread=0.02, **kw):
    return ProbabilityEstimate(
        market_id=kw.get("market_id", "m"), p_market_mid=mid, p_model=p_model,
        p_research=p_research, p_raw=p_final, p_final=p_final, shrink=0.25,
        confidence=kw.get("confidence", 0.8), research_source="grok_cache",
        research_usable=research_usable, model_has_edge=model_has_edge,
        ambiguity_score=kw.get("ambiguity", 0.0), evidence_score=kw.get("evidence", 0.8),
        stale_score=kw.get("stale_score", 0.0), spread=spread,
        liquidity_usd=kw.get("liquidity", 20_000.0), calibration_error=kw.get("calib_err", 0.0),
        fresh_book=fresh, best_ask=kw.get("best_ask", mid + spread / 2),
        calibrated_probability=(calibrated if calibrated is not None else p_final),
        effective_sample_size=kw.get("ess", 40.0), calibration_method="platt",
        chainlink_confidence=kw.get("cl_conf", 0.0))


def _edge(*, should_trade=True, net_edge=0.05, reason="trade", price=0.52, p_final=0.60,
          side="BUY"):
    return EdgeResult(
        market_id="m", outcome="YES", side=side, executable_price=price, p_final=p_final,
        gross_edge=p_final - price, cost_penalty=0.01, net_edge=net_edge,
        uncertainty_band=0.02, threshold=0.03, should_trade=should_trade, reason=reason)


def _bregman_opp(asks=(0.30, 0.30, 0.30)):
    legs = [SimplexLeg(market_id="m", outcome=f"O{i}", token_id=f"t{i}", ask=a,
                       depth_usd=10_000.0, fresh_book=True) for i, a in enumerate(asks)]
    grp = SimplexGroup(group_id="event:e", group_type="exhaustive_event", legs=legs,
                       mutually_exclusive=True, exhaustive=True)
    return BregmanArbitrageEngine(slippage_bps=25.0).certify(grp)


# --------------------------------------------------------------------------- #
def test_priority_constants_ordered():
    assert STRATEGY_PRIORITIES["bregman_arbitrage"] == 1
    assert STRATEGY_PRIORITIES["statistical_mispricing"] == 2
    assert STRATEGY_PRIORITIES["directional"] == 3


def test_certified_bregman_wins_over_directional():
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.85, p_final=0.62), edge=_edge(),
                    bregman_opp=_bregman_opp())
    assert sig.strategy == "bregman_arbitrage"
    assert sig.priority == 1
    assert sig.should_trade is True
    assert sig.edge_after_costs > 0.0


def test_uncertified_bregman_does_not_win():
    # over-round group (sum 1.20) -> no certified arb -> directional/statistical wins
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.85, p_final=0.62, model_has_edge=False),
                    edge=_edge(), bregman_opp=_bregman_opp(asks=(0.40, 0.40, 0.40)))
    assert sig.strategy != "bregman_arbitrage"
    assert sig.priority in (2, 3)
    # rejected Bregman is recorded as a no-trade diagnostic
    assert any(rs["strategy"] == "bregman_arbitrage" for rs in sig.rejected_signals)


def test_statistical_mispricing_is_priority_2_when_model_driven():
    # model/calibration drives the edge (research not the dominant mover) -> P2
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.60, p_research=0.50, p_final=0.60,
                             model_has_edge=True),
                    edge=_edge(net_edge=0.05))
    assert sig.strategy == "statistical_mispricing"
    assert sig.priority == 2


def test_directional_is_priority_3_when_research_driven():
    # research view dominates the deviation from market -> P3 directional
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.50, p_research=0.80, p_final=0.66,
                             research_usable=True),
                    edge=_edge(net_edge=0.05))
    assert sig.strategy == "directional"
    assert sig.priority == 3


def test_rank_and_select_orders_by_priority():
    r = SignalResolver()
    breg = r.resolve(est=_est(), edge=_edge(), bregman_opp=_bregman_opp())
    direc = r.resolve(est=_est(p_research=0.80, p_final=0.66), edge=_edge())
    ranked = rank_signals([direc, breg])
    assert ranked[0].strategy == "bregman_arbitrage"
    assert select_best([direc, breg]).priority == 1


def test_no_trade_when_edge_gate_fails_and_no_bregman():
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.85, p_final=0.62),
                    edge=_edge(should_trade=False, reason="edge_too_low"))
    assert sig.should_trade is False
    assert sig.strategy == "none"
    assert sig.no_trade_reason == "edge_too_low"
    # every strategy that did not fire has a recorded reason
    strategies = {rs["strategy"] for rs in sig.rejected_signals}
    assert {"bregman_arbitrage", "statistical_mispricing", "directional"} <= strategies
