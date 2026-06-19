"""Tier-2 institutional risk: confidence-aware Kelly sizing, regime detector, and the
portfolio-risk engine (VaR/CVaR + concentration limits). All pure, tighten-only, paper-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.training.kelly_sizing import confidence_multiplier, confidence_kelly_size_usd
from engine.training.regime import detect_regime
from engine.training.portfolio_risk import (PortfolioRiskEngine, historical_var,
                                             historical_cvar, concentration_report)


# ---------------- confidence-aware Kelly ----------------

def test_confidence_multiplier_monotonic():
    assert confidence_multiplier(0.0) == 1.0                  # tight CI -> full
    assert confidence_multiplier(0.5, ci_width_max=0.5, floor=0.2) == 0.2  # wide -> floor
    assert confidence_multiplier(0.1) > confidence_multiplier(0.3)         # wider -> smaller


def test_kelly_size_shrinks_with_uncertainty_and_regime():
    base, _ = confidence_kelly_size_usd(0.7, 0.5, bankroll=500.0, ci_width=0.0,
                                        max_size_usd=10.0, regime_multiplier=1.0)
    wide, _ = confidence_kelly_size_usd(0.7, 0.5, bankroll=500.0, ci_width=0.4,
                                        max_size_usd=10.0, regime_multiplier=1.0)
    stressed, _ = confidence_kelly_size_usd(0.7, 0.5, bankroll=500.0, ci_width=0.0,
                                            max_size_usd=10.0, regime_multiplier=0.3)
    assert 0 < wide < base                       # wider CI -> smaller stake
    assert 0 < stressed < base                   # stressed regime -> smaller stake
    assert base <= 10.0                          # never exceeds the band cap


def test_kelly_no_edge_zero_size():
    size, _ = confidence_kelly_size_usd(0.4, 0.5, bankroll=500.0, max_size_usd=10.0)
    assert size == 0.0                           # p < price -> no Kelly edge -> no stake


def test_kelly_never_exceeds_cap():
    size, _ = confidence_kelly_size_usd(0.95, 0.05, bankroll=100000.0, ci_width=0.0,
                                        max_size_usd=10.0, max_fraction=0.5)
    assert size <= 10.0


# ---------------- regime detector ----------------

def test_regime_calm_full_aggression():
    r = detect_regime(recent_returns=[0.01, -0.005, 0.008, 0.002], drawdown_pct=0.0)
    assert r.regime == "calm" and r.aggression_multiplier == 1.0


def test_regime_stressed_on_drawdown():
    r = detect_regime(recent_returns=[0.0], drawdown_pct=0.20, drawdown_stressed=0.10)
    assert r.regime == "stressed" and r.aggression_multiplier <= 0.5
    assert "drawdown_breach" in r.reasons


def test_regime_multiplier_never_above_one():
    r = detect_regime(recent_returns=[0.5, -0.5, 0.4, -0.6], drawdown_pct=0.3,
                      stale_rate=0.5, loss_streak=10)
    assert 0.0 < r.aggression_multiplier <= 1.0


# ---------------- VaR / CVaR ----------------

def test_var_cvar_tail():
    rets = [-0.5, -0.3, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.05]
    assert historical_var(rets, alpha=0.9) <= 0.0
    assert historical_cvar(rets, alpha=0.9) <= historical_var(rets, alpha=0.9)


def test_var_empty():
    assert historical_var([]) == 0.0 and historical_cvar([]) == 0.0


# ---------------- portfolio concentration gate ----------------

def _pos(event, cat, cost):
    return SimpleNamespace(group_key=event, category=cat, cluster_id=event, cost=cost,
                           closed=False)


def test_concentration_report_hhi():
    pos = [_pos("E1", "crypto", 50.0), _pos("E2", "crypto", 50.0)]
    rep = concentration_report(pos, bankroll=1000.0)
    assert rep["total_exposure_usd"] == 100.0
    assert rep["event_count"] == 2 and rep["category_count"] == 1
    assert rep["category_hhi"] == 1.0          # one category -> fully concentrated
    assert abs(rep["event_hhi"] - 0.5) < 1e-6


def test_event_concentration_caps_size():
    cfg = SimpleNamespace(max_event_exposure_frac=0.20, max_category_exposure_frac=0.40,
                          max_portfolio_exposure_frac=0.80, portfolio_cvar_limit_frac=0.0)
    eng = PortfolioRiskEngine(cfg)
    # bankroll 100 -> event cap 20; already 18 in E1 -> only 2 headroom for a 10 request
    pos = [_pos("E1", "crypto", 18.0)]
    dec = eng.check_candidate(notional_usd=10.0, event_key="E1", category="crypto",
                              positions=pos, bankroll=100.0)
    assert dec.allow is True and dec.capped_notional_usd == 2.0
    assert "event_concentration_cap" in dec.reasons


def test_concentration_blocks_when_full():
    cfg = SimpleNamespace(max_event_exposure_frac=0.20, max_category_exposure_frac=0.40,
                          max_portfolio_exposure_frac=0.80, portfolio_cvar_limit_frac=0.0)
    eng = PortfolioRiskEngine(cfg)
    pos = [_pos("E1", "crypto", 20.0)]          # event cap fully used
    dec = eng.check_candidate(notional_usd=5.0, event_key="E1", category="crypto",
                              positions=pos, bankroll=100.0)
    assert dec.allow is False and dec.capped_notional_usd == 0.0


def test_no_positions_allows_full():
    cfg = SimpleNamespace(max_event_exposure_frac=0.20, max_category_exposure_frac=0.40,
                          max_portfolio_exposure_frac=0.80, portfolio_cvar_limit_frac=0.0)
    eng = PortfolioRiskEngine(cfg)
    dec = eng.check_candidate(notional_usd=10.0, event_key="E1", category="crypto",
                              positions=[], bankroll=500.0)
    assert dec.allow is True and dec.capped_notional_usd == 10.0   # tighten-only: unchanged
