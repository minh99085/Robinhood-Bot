"""Aggressive-mode final validation + PAPER-ONLY safety (TDD, deterministic).

Quant scope: Strategy Optimization & Robustness Testing + Compliance/Security.
The aggressive PAPER-training mode turns on every non-live feature, trades more,
generates more feedback, and shows measurable learning improvement — and it can
NEVER execute live, arm a live broker, or bypass the paper-only restrictions.
"""

from __future__ import annotations

from engine.training import AggressivePaperTrainingConfig, PolymarketPaperTrainer, TrainingConfig
from engine.training.final_validation import aggressive_mode_metrics

from tests._pmtrain_helpers import FakeResearch, catalog, clean_live_env


# --- aggressive-mode learning metrics --------------------------------------
def test_aggressive_mode_metrics_show_improvement():
    m = aggressive_mode_metrics(
        total_trades=55, unique_markets=18, unique_categories=5, feedback_samples=60,
        exploration_trades=20, exploit_trades=35, bregman_bundles=4,
        chainlink_linked_trades=12, rejection_rate_before=0.6, rejection_rate_after=0.4,
        ece_before=0.15, ece_after=0.02, learning_samples_before=18,
        learning_samples_after=60, max_drawdown=13.0)
    assert m["rejection_reduction"] > 0          # fewer rejections
    assert m["calibration_improvement"] > 0      # ECE dropped
    assert m["learning_rate_improvement"] > 0    # more feedback samples
    assert m["feedback_generated_per_drawdown_unit"] > 0
    assert m["total_paper_trades"] == 55 and m["bregman_bundles"] == 4


def test_aggressive_mode_metrics_attest_paper_only():
    m = aggressive_mode_metrics(
        total_trades=1, unique_markets=1, unique_categories=1, feedback_samples=1,
        exploration_trades=1, exploit_trades=0, bregman_bundles=0,
        chainlink_linked_trades=0, rejection_rate_before=0.5, rejection_rate_after=0.5,
        ece_before=0.1, ece_after=0.1, learning_samples_before=0,
        learning_samples_after=1, max_drawdown=1.0)
    assert m["paper_only"] is True
    assert m["live_orders"] == 0
    assert m["live_execution_enabled"] is False


# --- PAPER-ONLY safety: aggressive cannot go live --------------------------
def test_aggressive_config_cannot_enable_live():
    # any attempt to override into a live mode is clamped to paper
    cfg = AggressivePaperTrainingConfig(mode="live")
    assert cfg.is_paper_only is True
    assert cfg.mode in ("disabled", "observe_only", "paper_train")


def test_aggressive_trainer_runs_paper_only_and_passes_preflight(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(AggressivePaperTrainingConfig(max_hold_ticks=2),
                               data_dir=tmp_path, signal_model=FakeResearch(fair=0.80))
    for _ in range(3):
        t.run_tick(catalog(12, bid=0.28, ask=0.30))
    t.finalize()
    pf = t.preflight()
    assert pf["ok"] is True
    assert pf["live_detected"] is False
    assert pf["checks"]["arbitrage_disabled"] is True
    assert pf["checks"]["no_wallet_or_private_key"] is True
    assert t.cfg.is_paper_only is True
    assert t.status()["execution_mode"] == "paper"
    # aggressive actually traded (more feedback) but only on paper
    assert t.pnl_summary()["trades_opened"] > 0


def test_aggressive_trainer_has_no_live_order_surface():
    # the trainer exposes no live submit/cancel/sign methods
    for attr in ("submit_live", "place_live_order", "arm_live", "sign_order", "live_broker"):
        assert not hasattr(PolymarketPaperTrainer, attr)


def test_aggressive_trades_more_with_more_feedback_than_conservative(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    cat = catalog(12, bid=0.28, ask=0.30)
    cons = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_hold_ticks=1),
                                  data_dir=tmp_path / "c", signal_model=FakeResearch(fair=0.80))
    aggr = PolymarketPaperTrainer(AggressivePaperTrainingConfig(max_hold_ticks=1),
                                  data_dir=tmp_path / "a", signal_model=FakeResearch(fair=0.80))
    for _ in range(4):
        cons.run_tick(cat)
        aggr.run_tick(cat)
    cons.finalize()
    aggr.finalize()
    assert aggr.pnl_summary()["trades_opened"] >= cons.pnl_summary()["trades_opened"]
    assert aggr.learner.closed >= cons.learner.closed       # >= feedback samples
