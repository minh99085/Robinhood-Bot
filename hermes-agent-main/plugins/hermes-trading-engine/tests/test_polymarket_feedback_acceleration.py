"""Polymarket acceleration raises SOFT capacity only; hard caps untouched."""

from __future__ import annotations

from engine.training.config import TrainingConfig
from engine.training.feedback_accelerator import apply_feedback_accelerator


def test_no_op_when_disabled():
    cfg = TrainingConfig()
    before = cfg.paper_decision_budget
    rep = apply_feedback_accelerator(cfg)
    assert rep["applied"] is False
    assert cfg.paper_decision_budget == before


def test_raises_decision_and_candidate_capacity():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         feedback_accelerator_target_multiplier=10,
                         paper_decision_budget=30, trade_candidate_limit=30,
                         shortlist_limit=150, live_watch_limit=100)
    rep = apply_feedback_accelerator(cfg)
    assert rep["applied"] is True
    assert rep["after"]["paper_decision_budget"] > rep["before"]["paper_decision_budget"]
    assert rep["after"]["trade_candidate_limit"] > rep["before"]["trade_candidate_limit"]
    assert rep["after"]["shortlist_limit"] > rep["before"]["shortlist_limit"]
    # ~10x more decisions per tick
    assert cfg.paper_decision_budget >= 10 * 30


def test_hard_caps_are_not_touched():
    cfg = TrainingConfig(feedback_accelerator_enabled=True)
    open_cap = cfg.max_open_trades
    notional = cfg.max_order_notional_usd
    total_exp = cfg.max_total_exposure_usd
    daily_loss = cfg.max_daily_loss_usd
    apply_feedback_accelerator(cfg)
    assert cfg.max_open_trades == open_cap
    assert cfg.max_order_notional_usd == notional
    assert cfg.max_total_exposure_usd == total_exp
    assert cfg.max_daily_loss_usd == daily_loss


def test_capacity_is_bounded():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         feedback_accelerator_target_multiplier=20,
                         paper_decision_budget=900)
    apply_feedback_accelerator(cfg)
    assert cfg.paper_decision_budget <= 1000     # hard ceiling on the soft knob
    assert cfg.trade_candidate_limit <= 200
    assert cfg.shortlist_limit <= 400


def test_exploration_turned_on_for_acceleration():
    cfg = TrainingConfig(feedback_accelerator_enabled=True, exploration_enabled=False)
    apply_feedback_accelerator(cfg)
    assert cfg.exploration_enabled is True
