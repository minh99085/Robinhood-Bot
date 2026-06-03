"""Feedback Accelerator config: off by default, paper-only, hard invariants."""

from __future__ import annotations

from engine.training.config import TrainingConfig


def test_off_by_default():
    cfg = TrainingConfig()
    assert cfg.feedback_accelerator_enabled is False
    assert cfg.feedback_accelerator_mode == "paper_only"
    assert cfg.feedback_accelerator_target_multiplier == 10


def test_fields_present_with_safe_defaults():
    cfg = TrainingConfig()
    assert cfg.exploration_tiny_size_enabled is True
    assert cfg.exploration_requires_realistic_fill is True
    assert cfg.exploration_requires_risk_gate is True
    assert cfg.exploration_min_book_freshness_required is True
    assert cfg.exploration_can_bypass_hard_gate is False
    assert cfg.exploration_counts_for_readiness is False
    assert cfg.shadow_decision_logging_enabled is True
    assert cfg.no_trade_labeling_enabled is True


def test_env_enables_accelerator(monkeypatch):
    monkeypatch.setenv("FEEDBACK_ACCELERATOR_ENABLED", "1")
    monkeypatch.setenv("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", "10")
    monkeypatch.setenv("EXPLORATION_ENABLED", "1")
    monkeypatch.setenv("EXPLORATION_TINY_SIZE_ENABLED", "1")
    monkeypatch.setenv("EXPLORATION_COUNTS_FOR_READINESS", "0")
    cfg = TrainingConfig.from_env()
    assert cfg.feedback_accelerator_enabled is True
    assert cfg.feedback_accelerator_target_multiplier == 10
    assert cfg.exploration_enabled is True
    assert cfg.exploration_tiny_size_enabled is True
    assert cfg.exploration_counts_for_readiness is False


def test_hard_invariants_reasserted_even_if_overridden():
    # Even if someone tries to let exploration bypass hard gates, __post_init__
    # forces the safe invariants back on.
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         exploration_can_bypass_hard_gate=True,
                         exploration_requires_realistic_fill=False,
                         exploration_requires_risk_gate=False,
                         exploration_min_book_freshness_required=False)
    assert cfg.exploration_can_bypass_hard_gate is False
    assert cfg.exploration_requires_realistic_fill is True
    assert cfg.exploration_requires_risk_gate is True
    assert cfg.exploration_min_book_freshness_required is True
    assert cfg.feedback_accelerator_mode == "paper_only"


def test_target_multiplier_clamped():
    cfg = TrainingConfig(feedback_accelerator_target_multiplier=999)
    assert cfg.feedback_accelerator_target_multiplier <= 20


def test_exploration_caps_clamped():
    cfg = TrainingConfig(exploration_notional_fraction=1.0,
                         exploration_max_daily_loss=100000.0,
                         exploration_max_event_exposure=100000.0)
    assert cfg.exploration_notional_fraction <= 0.02
    assert cfg.exploration_max_daily_loss <= 100.0
    assert cfg.exploration_max_event_exposure <= 50.0


def test_campaign_safe_forces_exploration_not_readiness():
    cfg = TrainingConfig.institutional_campaign_defaults(
        feedback_accelerator_enabled=True, exploration_counts_for_readiness=True)
    assert cfg.exploration_counts_for_readiness is False
    assert cfg.exploration_can_bypass_hard_gate is False
