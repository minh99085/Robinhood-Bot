"""Exploration / shadow / no-trade never count as proven live-readiness edge."""

from __future__ import annotations

from engine.training.feedback_accelerator import (EXPLOIT_TRADE, NO_TRADE_LABEL,
                                                  SHADOW_DECISION_ONLY,
                                                  TINY_EXPLORATION_TRADE,
                                                  counts_for_readiness)


def test_shadow_never_counts():
    assert counts_for_readiness(SHADOW_DECISION_ONLY, resolved=True, validated=True) is False


def test_no_trade_never_counts():
    assert counts_for_readiness(NO_TRADE_LABEL, resolved=True, validated=True) is False


def test_exploration_does_not_count_by_default():
    # Even cleanly resolved + validated, exploration is NOT readiness proof unless
    # the operator explicitly opts in.
    assert counts_for_readiness(TINY_EXPLORATION_TRADE, resolved=True, validated=True) is False
    assert counts_for_readiness(TINY_EXPLORATION_TRADE, resolved=True, validated=True,
                                exploration_counts=False) is False


def test_exploration_counts_only_if_optin_and_resolved_and_validated():
    # opt-in but not resolved -> still no
    assert counts_for_readiness(TINY_EXPLORATION_TRADE, resolved=False, validated=True,
                                exploration_counts=True) is False
    # opt-in but not validated -> still no
    assert counts_for_readiness(TINY_EXPLORATION_TRADE, resolved=True, validated=False,
                                exploration_counts=True) is False
    # opt-in + resolved + validated -> yes
    assert counts_for_readiness(TINY_EXPLORATION_TRADE, resolved=True, validated=True,
                                exploration_counts=True) is True


def test_exploit_trade_counts_only_when_resolved_and_validated():
    assert counts_for_readiness(EXPLOIT_TRADE, resolved=True, validated=True) is True
    assert counts_for_readiness(EXPLOIT_TRADE, resolved=False, validated=True) is False
    assert counts_for_readiness(EXPLOIT_TRADE, resolved=True, validated=False) is False
