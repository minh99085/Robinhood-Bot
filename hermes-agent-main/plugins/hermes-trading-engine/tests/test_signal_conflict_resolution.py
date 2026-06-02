"""Signal conflict resolution across disagreeing sources (TDD, deterministic).

Quant scope: Signal Generation + Risk Management. When Bregman, Chainlink-
conditioned probability, the research/Grok estimate, market microstructure, and
the learner disagree, the resolver resolves by priority, records every source's
vote + the disagreement, and never lets a stale oracle force a more aggressive
trade.
"""

from __future__ import annotations

from engine.training.edge_engine import EdgeResult
from engine.training.probability_stack import ProbabilityEstimate
from engine.training.signal_resolver import SignalResolver

from tests.test_signal_priority_bregman_first import _bregman_opp, _edge, _est


def test_bregman_overrides_directional_disagreement():
    # research says SELL, but a certified Bregman bundle exists -> Bregman wins
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_research=0.20, p_final=0.40),
                    edge=_edge(side="SELL", net_edge=0.05), bregman_opp=_bregman_opp())
    assert sig.strategy == "bregman_arbitrage"
    assert sig.conflict["resolution"] == "bregman_priority"
    assert sig.conflict["votes"]["research"] == "sell"


def test_conflict_records_all_source_votes():
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.55, p_research=0.80, p_final=0.66,
                             cl_conf=0.7),
                    edge=_edge(side="BUY"))
    votes = sig.conflict["votes"]
    for src in ("bregman", "chainlink", "research", "microstructure", "learner"):
        assert src in votes
    assert votes["research"] == "buy"
    assert votes["learner"] == "buy"          # p_model 0.55 > mid 0.50
    assert votes["microstructure"] == "buy"   # edge side BUY


def test_disagreement_flagged_when_sources_diverge():
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.40, p_research=0.80, p_final=0.66),
                    edge=_edge(side="BUY"))
    # learner leans down (0.40<0.50) while research leans up -> disagreement
    assert sig.conflict["disagreement"] is True


def test_stale_chainlink_never_forces_more_aggressive_trade():
    # a stale oracle (chainlink_no_trade) must not produce a tradable signal here
    r = SignalResolver()
    est = _est(mid=0.50, p_research=0.85, p_final=0.62)
    est.chainlink_no_trade = True
    est.no_trade_probability_reason = "chainlink_stale_or_irrelevant"
    sig = r.resolve(est=est, edge=_edge(should_trade=False,
                                        reason="chainlink_stale_or_irrelevant"))
    assert sig.should_trade is False
    assert sig.no_trade_reason == "chainlink_stale_or_irrelevant"
    assert sig.conflict["votes"]["chainlink"] == "stale"


def test_agreement_yields_clean_directional_trade():
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.50, p_research=0.80, p_final=0.66),
                    edge=_edge(side="BUY", net_edge=0.06))
    assert sig.should_trade is True
    assert sig.strategy == "directional"
    assert sig.conflict["disagreement"] is False
