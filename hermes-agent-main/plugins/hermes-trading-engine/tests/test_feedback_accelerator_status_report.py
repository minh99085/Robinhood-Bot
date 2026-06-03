"""Feedback Accelerator status + report visibility + metrics."""

from __future__ import annotations

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.feedback_accelerator import (EXPLOIT_TRADE, FeedbackAcceleratorMetrics,
                                                  NO_TRADE_LABEL, SHADOW_DECISION_ONLY,
                                                  TINY_EXPLORATION_TRADE)
from engine.training.reports import _markdown

_REQUIRED_METRIC_KEYS = (
    "feedback_multiplier_actual", "decisions_per_hour", "shadow_decisions_per_hour",
    "no_trade_labels_per_hour", "tiny_exploration_trades_per_hour", "exploit_trades_per_hour",
    "btc_pulse_decisions_per_hour", "polymarket_decisions_per_hour", "useful_feedback_samples",
    "feedback_samples_resolved", "exploration_pnl", "exploit_pnl", "exploration_hit_rate",
    "exploit_hit_rate", "exploration_drawdown", "hard_gate_rejections",
    "soft_gate_relaxed_count", "soft_gate_relaxation_pnl", "blockers_correct_rate",
    "edge_too_low_correct_rate", "no_fresh_book_correct_rate", "depth_too_thin_correct_rate",
    "naive_price_extreme_correct_rate",
)


def test_metrics_to_dict_has_all_keys():
    m = FeedbackAcceleratorMetrics(enabled=True, target_multiplier=10)
    out = m.to_dict(runtime_hours=1.0)
    for k in _REQUIRED_METRIC_KEYS:
        assert k in out, k


def test_metrics_count_and_separate_exploit_exploration():
    m = FeedbackAcceleratorMetrics(enabled=True, target_multiplier=10,
                                   baseline_decisions_per_hour=1.0)
    for _ in range(10):
        m.record_decision(SHADOW_DECISION_ONLY)
    m.record_decision(NO_TRADE_LABEL)
    m.record_decision(EXPLOIT_TRADE)
    m.record_decision(TINY_EXPLORATION_TRADE)
    m.record_resolution(EXPLOIT_TRADE, won=True, pnl=2.0)
    m.record_resolution(TINY_EXPLORATION_TRADE, won=False, pnl=-1.0)
    out = m.to_dict(runtime_hours=1.0)
    assert out["shadow_decisions_per_hour"] == 10.0
    assert out["exploit_pnl"] == 2.0
    assert out["exploration_pnl"] == -1.0
    assert out["exploit_hit_rate"] == 1.0
    assert out["exploration_hit_rate"] == 0.0
    # decisions/hour vs baseline => actual multiplier reflects the speed-up
    assert out["feedback_multiplier_actual"] >= 10.0


def test_blocker_correctness_rates():
    m = FeedbackAcceleratorMetrics(enabled=True)
    m.record_blocker_score("edge_too_low", True)
    m.record_blocker_score("edge_too_low", False)
    m.record_blocker_score("no_fresh_book", True)
    out = m.to_dict(runtime_hours=1.0)
    assert out["edge_too_low_correct_rate"] == 0.5
    assert out["no_fresh_book_correct_rate"] == 1.0


def test_trainer_status_has_feedback_accelerator_block(tmp_path):
    cfg = TrainingConfig(mode="observe_only", feedback_accelerator_enabled=True,
                         exploration_enabled=True, exploration_tiny_size_enabled=True)
    t = PolymarketPaperTrainer(cfg, data_dir=tmp_path)
    st = t.status()
    fa = st["feedback_accelerator"]
    assert fa["feedback_accelerator_enabled"] is True
    assert fa["mode"] == "paper_only"
    assert fa["hard_gates_locked"]["exploration_can_bypass_hard_gate"] is False
    assert "soft_gates" in fa


def test_markdown_report_includes_accelerator_section():
    status = {"mode": "paper_train", "pnl": {}, "scan_metrics": {}, "risk": {},
              "learning": {}, "feedback": {}, "safety": {},
              "feedback_accelerator": {"feedback_accelerator_enabled": True,
                                       "target_multiplier": 10, "mode": "paper_only",
                                       "exploration_enabled": True,
                                       "exploration_tiny_size_enabled": True,
                                       "capacity": {"paper_decision_budget": 300,
                                                    "trade_candidate_limit": 60,
                                                    "shortlist_limit": 300},
                                       "soft_gates": {"exploit_min_edge": 0.03,
                                                      "exploit_min_confidence": 0.8,
                                                      "exploration_min_edge": -0.02,
                                                      "exploration_min_confidence": 0.6},
                                       "shadow_decision_logging_enabled": True,
                                       "no_trade_labeling_enabled": True,
                                       "exploration_counts_for_readiness": False}}
    md = _markdown(status, "run-test")
    assert "10x Feedback Accelerator" in md
    assert "HARD gates locked" in md
