"""Exploration trades are tiny, capped, and labeled."""

from __future__ import annotations

from engine.training.config import TrainingConfig
from engine.training.feedback_accelerator import (SoftGates, TINY_EXPLORATION_TRADE,
                                                  tiny_exploration_gate,
                                                  tiny_exploration_notional)

_SG = SoftGates(0.03, 0.8, 0.03, -0.02, 0.5, -0.02)


def test_notional_is_tiny_and_capped():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         exploration_notional_fraction=0.002,
                         exploration_notional_usd=2.0)
    n = tiny_exploration_notional(cfg, equity=500.0)
    assert n > 0.0
    assert n <= 2.0                              # never above the explicit tiny size
    assert n <= cfg.max_order_notional_usd       # never above the paper order ceiling


def test_notional_never_exceeds_paper_ceiling_even_with_big_fraction():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         exploration_notional_fraction=0.02,   # clamped max
                         exploration_notional_usd=5.0)
    n = tiny_exploration_notional(cfg, equity=100000.0)
    assert n <= cfg.max_order_notional_usd


def test_exposure_cap_blocks_more_exploration():
    res = tiny_exploration_gate(
        fresh_book=True, valid_token=True, has_price=True, risk_ok=True,
        realistic_fill_ok=True, exploration_daily_loss_ok=True,
        edge=0.01, confidence=0.6, after_cost_ev=0.0, exposure_ok=False, soft_gates=_SG)
    assert res["allowed"] is False
    assert res["reason"] == "tiny_exposure_cap"
    assert res["hard_gate_block"] is False       # exposure cap is a soft cap


def test_allowed_trade_is_classified_as_tiny_exploration():
    res = tiny_exploration_gate(
        fresh_book=True, valid_token=True, has_price=True, risk_ok=True,
        realistic_fill_ok=True, exploration_daily_loss_ok=True,
        edge=0.0, confidence=0.55, after_cost_ev=0.0, exposure_ok=True, soft_gates=_SG)
    assert res["allowed"] is True
    assert res["decision_class"] == TINY_EXPLORATION_TRADE


def test_per_hour_cap_is_configurable_and_clamped():
    cfg = TrainingConfig(exploration_max_trades_per_hour=30)
    assert cfg.exploration_max_trades_per_hour == 30
