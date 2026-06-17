"""Structured research signal (#2): per-call conviction + news freshness decay.

Advisory-only: these scale HOW MUCH / HOW LONG a research probability moves p_raw.
They never place, size, or gate a trade.
"""

from __future__ import annotations

from engine.research.signal_quality import (freshness_decay, conviction_multiplier,
                                            research_quality_multiplier)


# --- freshness decay --------------------------------------------------------

def test_freshness_decay_halves_each_half_life():
    now = 10_000.0
    assert freshness_decay(now, 100.0, now) == 1.0                 # fresh -> 1.0
    assert abs(freshness_decay(now - 100.0, 100.0, now) - 0.5) < 1e-9   # 1 half-life
    assert abs(freshness_decay(now - 200.0, 100.0, now) - 0.25) < 1e-9  # 2 half-lives


def test_freshness_decay_floor_applies():
    now = 10_000.0
    # very old -> would be ~0, but floored
    assert freshness_decay(now - 10_000.0, 100.0, now, floor=0.1) == 0.1


def test_freshness_decay_missing_data_is_no_op():
    now = 10_000.0
    assert freshness_decay(None, 100.0, now) == 1.0                # no as-of -> 1.0
    assert freshness_decay(now, None, now) == 1.0                  # no half-life -> 1.0
    assert freshness_decay(now, 0.0, now) == 1.0


# --- conviction -------------------------------------------------------------

def test_conviction_prefers_explicit_then_uncertainty_then_default():
    assert conviction_multiplier(conviction=0.8) == 0.8
    assert abs(conviction_multiplier(uncertainty=0.3) - 0.7) < 1e-9
    assert conviction_multiplier() == 1.0
    assert conviction_multiplier(conviction=5.0) == 1.0           # clamped


def test_combined_multiplier_is_conviction_times_freshness():
    now = 10_000.0
    rq = research_quality_multiplier(conviction=0.8, asof_ts=now - 100.0,
                                     half_life_s=100.0, now=now)
    assert abs(rq["conviction"] - 0.8) < 1e-9
    assert abs(rq["freshness"] - 0.5) < 1e-9
    assert abs(rq["multiplier"] - 0.4) < 1e-9


# --- effect on the probability stack ----------------------------------------

def test_stale_news_signal_moves_p_raw_less(tmp_path, monkeypatch):
    from engine.markets import universe_manager as um
    from engine.training import TrainingConfig
    from engine.training.probability_stack import ProbabilityStack
    from engine.campaigns.signal_models import SignalResult
    from tests._pmtrain_helpers import market, clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    now = 1_000_000.0
    raw = market(0, bid=0.39, ask=0.41, depth=2000, category="crypto", now=now)
    rec = um.MarketRecord.from_raw(raw, now=now)
    cfg = TrainingConfig(mode="paper_train", research_freshness_floor=0.0,
                         grok_news_half_life_s=600.0)

    class _Model:
        def __init__(self, asof):
            self.asof = asof

        def evaluate(self, _rec):
            return SignalResult(0.80, 0.9, "grok_cache", asof_ts=self.asof,
                                news_half_life_s=600.0)

    fresh = ProbabilityStack(cfg).estimate(rec, _Model(now), now=now)
    stale = ProbabilityStack(cfg).estimate(rec, _Model(now - 1200.0), now=now)  # 2 half-lives
    mid = fresh.p_market_mid
    # the fresh signal pulls p_raw toward 0.80 more than the 2-half-life-stale one
    assert abs(fresh.p_raw - mid) > abs(stale.p_raw - mid)


def test_structured_disabled_is_passthrough(tmp_path, monkeypatch):
    from engine.markets import universe_manager as um
    from engine.training import TrainingConfig
    from engine.training.probability_stack import ProbabilityStack
    from engine.campaigns.signal_models import SignalResult
    from tests._pmtrain_helpers import market, clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    now = 1_000_000.0
    raw = market(0, bid=0.39, ask=0.41, depth=2000, category="crypto", now=now)
    rec = um.MarketRecord.from_raw(raw, now=now)

    class _Model:
        def evaluate(self, _rec):
            return SignalResult(0.80, 0.9, "grok_cache", asof_ts=now - 5000.0,
                                news_half_life_s=600.0)
    on = ProbabilityStack(TrainingConfig(mode="paper_train", research_freshness_floor=0.0)
                          ).estimate(rec, _Model(), now=now)
    off = ProbabilityStack(TrainingConfig(mode="paper_train",
                                          research_structured_enabled=False)
                           ).estimate(rec, _Model(), now=now)
    mid = off.p_market_mid
    # with structured OFF, the stale signal still moves p_raw fully (no decay)
    assert abs(off.p_raw - mid) > abs(on.p_raw - mid)
