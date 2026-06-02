"""Alpha attribution for resolved signals (TDD, deterministic).

Quant scope: Strategy Optimization & Robustness Testing + Live Monitoring.
Verifies every resolved signal attributes its alpha across the named sources
(bregman divergence, market microstructure, Chainlink oracle, research/Grok,
calibration, learner/category, liquidity, execution edge) and exposes the
opportunity-quality + decay + uncertainty scores.
"""

from __future__ import annotations

from engine.training.signal_resolver import ALPHA_SOURCES, SignalResolver

from tests.test_signal_priority_bregman_first import _bregman_opp, _edge, _est


def test_attribution_has_all_sources():
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.80, p_final=0.66, cl_conf=0.5),
                    edge=_edge(net_edge=0.05))
    for src in ALPHA_SOURCES:
        assert src in sig.alpha_attribution, src
        assert isinstance(sig.alpha_attribution[src], float)


def test_research_driven_attribution_credits_research():
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.50, p_research=0.85, p_final=0.66),
                    edge=_edge())
    assert sig.alpha_attribution["research_grok"] > 0.0
    assert sig.alpha_attribution["learner_category"] == 0.0   # p_model == mid


def test_learner_driven_attribution_credits_learner():
    r = SignalResolver()
    sig = r.resolve(est=_est(mid=0.50, p_model=0.62, p_research=0.50, p_final=0.60,
                             research_usable=False),
                    edge=_edge())
    assert sig.alpha_attribution["learner_category"] > 0.0
    assert sig.alpha_attribution["research_grok"] == 0.0      # research not usable


def test_bregman_attribution_credits_divergence():
    r = SignalResolver()
    sig = r.resolve(est=_est(), edge=_edge(), bregman_opp=_bregman_opp())
    assert sig.alpha_attribution["bregman_divergence"] > 0.0


def test_calibration_shift_attributed():
    r = SignalResolver()
    # calibrated probability differs from p_final -> calibration alpha recorded
    sig = r.resolve(est=_est(p_final=0.66, calibrated=0.60), edge=_edge())
    assert abs(sig.alpha_attribution["calibration"] - 0.06) < 1e-6


def test_scores_present_and_bounded():
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.80, p_final=0.66), edge=_edge(net_edge=0.05))
    assert 0.0 <= sig.confidence <= 1.0
    assert 0.0 <= sig.persistence <= 1.0
    assert 0.0 <= sig.alpha_decay <= 1.0
    assert 0.0 <= sig.uncertainty_penalty <= 1.0
    assert 0.0 <= sig.chainlink_relevance <= 1.0
    assert sig.opportunity_quality >= 0.0
    assert sig.edge_after_costs == 0.05


def test_to_dict_serializes_attribution_and_scores():
    r = SignalResolver()
    d = r.resolve(est=_est(), edge=_edge(), bregman_opp=_bregman_opp()).to_dict()
    for key in ("strategy", "priority", "alpha_attribution", "opportunity_quality",
                "rejected_signals", "conflict", "no_trade_reason"):
        assert key in d
