"""Soft-gate relaxation applies ONLY to exploration; exploit gates unchanged."""

from __future__ import annotations

from engine.training.config import TrainingConfig
from engine.training.feedback_accelerator import feedback_value_score, resolve_soft_gates


def test_no_relaxation_when_disabled():
    cfg = TrainingConfig()        # accelerator off
    sg = resolve_soft_gates(cfg)
    # exploration gates equal exploit gates (conservative)
    assert sg.exploration_min_edge == sg.exploit_min_edge
    assert sg.exploration_min_confidence == sg.exploit_min_confidence


def test_relaxation_only_for_exploration_when_enabled():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         exploration_enabled=True, exploration_tiny_size_enabled=True)
    sg = resolve_soft_gates(cfg)
    # exploit thresholds are UNCHANGED (still strict)
    assert sg.exploit_min_edge == cfg.min_net_edge
    assert sg.exploit_min_confidence == cfg.research_high_confidence
    # exploration thresholds are LOWER (relaxed) — but bounded
    assert sg.exploration_min_edge <= sg.exploit_min_edge
    assert sg.exploration_min_confidence < sg.exploit_min_confidence
    assert sg.exploration_min_edge >= -0.02
    assert sg.exploration_min_confidence >= 0.50


def test_relaxation_requires_tiny_exploration_on():
    cfg = TrainingConfig(feedback_accelerator_enabled=True,
                         exploration_enabled=True, exploration_tiny_size_enabled=False)
    sg = resolve_soft_gates(cfg)
    assert sg.exploration_min_edge == sg.exploit_min_edge   # no relaxation


def test_feedback_value_prioritizes_uncertainty_and_near_threshold():
    high = feedback_value_score(model_uncertainty=0.9, near_threshold_edge=0.9,
                                evidence_disagreement=0.8,
                                expected_label_availability=1.0,
                                clean_label_probability=1.0)
    low = feedback_value_score(model_uncertainty=0.0, near_threshold_edge=0.0,
                               evidence_disagreement=0.0,
                               expected_label_availability=1.0,
                               clean_label_probability=1.0)
    assert high > low
    assert 0.0 <= low <= high <= 1.0


def test_feedback_value_gated_by_clean_label_probability():
    # A sample unlikely to ever resolve cleanly has reduced learning value.
    resolvable = feedback_value_score(model_uncertainty=0.9, near_threshold_edge=0.9,
                                      expected_label_availability=1.0,
                                      clean_label_probability=1.0)
    unresolvable = feedback_value_score(model_uncertainty=0.9, near_threshold_edge=0.9,
                                        expected_label_availability=0.0,
                                        clean_label_probability=0.0)
    assert resolvable > unresolvable
