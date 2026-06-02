"""Aggressive paper-training profile: more trades + more feedback, but PAPER-only
and unable to bypass hard risk caps."""

from __future__ import annotations

import pytest

from engine.training import (AggressivePaperTrainingConfig, TrainingConfig,
                             PolymarketPaperTrainer)
from tests._pmtrain_helpers import clean_live_env, catalog, market, FakeResearch


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


# --- profile enables every non-live learning feature ------------------------

def test_aggressive_profile_enables_all_nonlive_features():
    cfg = AggressivePaperTrainingConfig()
    assert cfg.mode == "paper_train"
    assert cfg.exploration_enabled is True and cfg.exploration_rate > 0
    assert cfg.learner_enabled and cfg.feedback_enabled and cfg.chainlink_enabled
    # wider scan + more candidates + lower edge bar than conservative defaults
    base = TrainingConfig()
    assert cfg.trade_candidate_limit >= base.trade_candidate_limit
    assert cfg.shortlist_limit >= base.shortlist_limit
    assert cfg.live_watch_limit >= base.live_watch_limit
    assert cfg.min_net_edge < base.min_net_edge
    assert cfg.max_spread >= base.max_spread
    assert cfg.base_shrink_factor >= base.base_shrink_factor


# --- paper-only + hard caps cannot be bypassed ------------------------------

def test_aggressive_is_paper_only():
    cfg = AggressivePaperTrainingConfig()
    assert cfg.is_paper_only is True
    assert cfg.mode in ("disabled", "observe_only", "paper_train")


def test_aggressive_cannot_set_live_mode():
    # an attempt to override into a live mode is clamped to a paper mode
    cfg = AggressivePaperTrainingConfig(mode="live")
    assert cfg.mode != "live" and cfg.is_paper_only is True


def test_aggressive_respects_hard_risk_caps():
    cfg = AggressivePaperTrainingConfig(
        fixed_notional_usd=10_000, max_market_exposure_usd=10_000_000,
        max_total_exposure_usd=10_000_000, max_open_trades=999,
        exploration_notional_usd=10_000)
    assert cfg.max_order_notional_usd <= 50.0
    assert cfg.max_market_exposure_usd <= 500.0
    assert cfg.max_total_exposure_usd <= 5000.0
    assert cfg.max_open_trades <= 8
    # exploratory size can never exceed the hard order-notional cap
    assert cfg.exploration_notional_usd <= cfg.max_order_notional_usd


# --- conservative vs aggressive: trade-count / candidate / rejection uplift --

def _trainer(env, cfg, fair=0.80):
    return PolymarketPaperTrainer(cfg, data_dir=env, signal_model=FakeResearch(fair=fair))


def test_aggressive_trades_more_than_conservative(env):
    cat = catalog(12, bid=0.28, ask=0.30)
    cons = _trainer(env, TrainingConfig(mode="paper_train", max_hold_ticks=1))
    aggr = _trainer(env, AggressivePaperTrainingConfig(max_hold_ticks=1))
    for _ in range(4):
        cons.run_tick(cat)
        aggr.run_tick(cat)
    cons.finalize()
    aggr.finalize()
    cons_n = cons.pnl_summary()["trades_opened"]
    aggr_n = aggr.pnl_summary()["trades_opened"]
    assert aggr_n >= cons_n                                   # trade-count uplift
    assert aggr.decision_count >= cons.decision_count          # candidate uplift
    # aggressive rejection rate is no worse (lower or equal) than conservative
    cons_rej = cons.rejection_count / max(1, cons.decision_count)
    aggr_rej = aggr.rejection_count / max(1, aggr.decision_count)
    assert aggr_rej <= cons_rej + 1e-9


def test_aggressive_exploration_generates_extra_feedback(env):
    # near-miss markets: small edge that conservative blocks (edge_too_low) but
    # aggressive controlled-exploration trades at small size for feedback signal.
    cat = catalog(12, bid=0.28, ask=0.30)
    cons = _trainer(env, TrainingConfig(mode="paper_train", max_hold_ticks=1), fair=0.315)
    aggr = _trainer(env, AggressivePaperTrainingConfig(max_hold_ticks=1,
                                                       exploration_rate=0.9,
                                                       exploration_min_edge=-0.5), fair=0.315)
    for _ in range(4):
        cons.run_tick(cat)
        aggr.run_tick(cat)
    cons.finalize()
    aggr.finalize()
    assert aggr.exploration_count > 0                          # exploration fired
    assert aggr.pnl_summary()["trades_opened"] > cons.pnl_summary()["trades_opened"]
    # feedback samples generated (closed trades feed the learner/calibrator)
    assert aggr.learner.closed >= cons.learner.closed


def test_aggressive_exploration_trades_are_small_and_capped(env):
    aggr = _trainer(env, AggressivePaperTrainingConfig(max_hold_ticks=5,
                                                       exploration_rate=1.0,
                                                       exploration_min_edge=-0.5), fair=0.315)
    aggr.run_tick(catalog(8, bid=0.28, ask=0.30))
    explore_fills = [f for f in aggr.fills_log if f.get("exploration")]
    for f in explore_fills:
        assert f["notional"] <= aggr.cfg.max_order_notional_usd + 1e-9
