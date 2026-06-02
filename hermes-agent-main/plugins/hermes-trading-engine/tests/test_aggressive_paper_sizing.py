"""Aggressive paper sizing policy (TDD, deterministic, offline).

Quant scope: Strategy Optimization & Robustness Testing + Risk Management.
Aggressive mode trades MORE OFTEN, not recklessly: smaller paper sizes, more
simultaneous positions, larger diversity target, tighter per-event/category/
bundle caps, explicit exploration budget — and it can NEVER exceed the hard paper
caps or bypass the mandatory gates (TrainingRiskGate / RiskEngine).
"""

from __future__ import annotations

from engine.training.config import AggressivePaperTrainingConfig, TrainingConfig
from engine.training.paper_policy import TradeProposal
from engine.training.polymarket_trainer import TrainingRiskGate
from engine.training.portfolio import PortfolioLimits


def test_aggressive_uses_smaller_size_and_more_positions():
    base = TrainingConfig()
    agg = AggressivePaperTrainingConfig()
    assert agg.mode == "paper_train"
    assert agg.fixed_notional_usd < base.fixed_notional_usd       # smaller paper size
    assert agg.max_open_trades >= base.max_open_trades            # more positions
    assert agg.exploration_enabled is True
    assert agg.exploration_budget_usd > 0.0                       # explicit budget


def test_aggressive_is_more_diversified_with_tighter_caps():
    base = TrainingConfig()
    agg = AggressivePaperTrainingConfig()
    assert agg.diversity_target >= base.diversity_target
    # tighter per-event hard cap (force more, smaller, diversified positions)
    assert agg.max_event_exposure_usd <= base.max_event_exposure_usd


def test_hard_paper_caps_clamped_even_if_config_tries_to_raise_them():
    cfg = TrainingConfig(
        mode="paper_train", fixed_notional_usd=500.0, max_open_trades=100,
        max_kelly_size_usd=999.0, max_market_exposure_usd=99_999.0,
        max_total_exposure_usd=10_000_000.0, max_event_exposure_usd=99_999.0,
        max_category_exposure_usd=99_999.0, max_bregman_bundle_exposure_usd=99_999.0,
        exploration_budget_usd=99_999.0, exploration_notional_usd=999.0)
    assert cfg.fixed_notional_usd <= 50.0
    assert cfg.max_kelly_size_usd <= 50.0
    assert cfg.max_open_trades <= 8
    assert cfg.max_market_exposure_usd <= 500.0
    assert cfg.max_total_exposure_usd <= 5000.0
    assert cfg.max_event_exposure_usd <= 500.0
    assert cfg.max_category_exposure_usd <= 1000.0
    assert cfg.max_bregman_bundle_exposure_usd <= 1000.0
    assert cfg.exploration_budget_usd <= 200.0
    # exploratory order size can never exceed the hard per-order notional ceiling
    assert cfg.exploration_notional_usd <= cfg.max_order_notional_usd + 1e-9


def test_aggressive_max_order_notional_within_hard_ceiling():
    agg = AggressivePaperTrainingConfig()
    assert agg.max_order_notional_usd <= 50.0


def test_aggressive_still_routed_through_mandatory_risk_gate():
    agg = AggressivePaperTrainingConfig()
    gate = TrainingRiskGate(agg)
    # an oversize order is rejected even in aggressive mode (mandatory gate)
    oversize = TradeProposal(
        market_id="m", asset_id="a", outcome="YES", side="BUY", price=0.5,
        notional_usd=agg.max_order_notional_usd + 100.0, qty=1.0, p_final=0.6,
        net_edge=0.05, confidence=0.9, research_source="grok_cache", sizing_method="fixed")
    d = gate.evaluate(oversize, fresh_book=True, market_exposure=0.0,
                      total_exposure=0.0, open_orders=0, daily_loss=0.0)
    assert d.approved is False
    assert d.code == "order_notional_exceeds_cap"


def test_portfolio_limits_from_aggressive_within_hard_ceilings():
    lim = PortfolioLimits.from_config(AggressivePaperTrainingConfig())
    assert lim.max_total_exposure_usd <= 5000.0
    assert lim.max_event_exposure_usd <= 500.0
    assert lim.max_bregman_bundle_exposure_usd <= 1000.0
    assert lim.exploration_budget_usd <= 200.0
