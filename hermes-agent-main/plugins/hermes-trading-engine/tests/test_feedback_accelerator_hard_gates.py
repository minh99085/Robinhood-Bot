"""Hard gates can NEVER be bypassed by exploration."""

from __future__ import annotations

import pytest

from engine.training.feedback_accelerator import (NO_TRADE_LABEL, SoftGates,
                                                  TINY_EXPLORATION_TRADE,
                                                  tiny_exploration_gate)

# Relaxed soft gates so soft thresholds never accidentally cause the block.
_SG = SoftGates(0.03, 0.8, 0.03, -0.02, 0.5, -0.02)


def _ok(**over):
    base = dict(live_blocked=False, fresh_book=True, valid_token=True, has_price=True,
                chainlink_relevant=False, chainlink_stale=False, ambiguity_score=0.0,
                risk_ok=True, realistic_fill_ok=True, exploration_daily_loss_ok=True,
                drawdown_kill_switch=False, edge=0.01, confidence=0.6, after_cost_ev=0.0,
                exposure_ok=True, soft_gates=_SG)
    base.update(over)
    return base


def test_allows_tiny_exploration_when_all_hard_gates_pass():
    res = tiny_exploration_gate(**_ok())
    assert res["allowed"] is True
    assert res["decision_class"] == TINY_EXPLORATION_TRADE
    assert res["hard_gate_block"] is False


@pytest.mark.parametrize("override,reason", [
    ({"live_blocked": True}, "live_blocked"),
    ({"fresh_book": False}, "no_fresh_book"),
    ({"valid_token": False}, "invalid_token"),
    ({"has_price": False}, "missing_price"),
    ({"chainlink_relevant": True, "chainlink_stale": True}, "stale_chainlink"),
    ({"ambiguity_score": 0.9}, "settlement_ambiguous"),
    ({"risk_ok": False}, "risk_rejected"),
    ({"realistic_fill_ok": False}, "realistic_fill_rejected"),
    ({"exploration_daily_loss_ok": False}, "exploration_daily_loss"),
    ({"drawdown_kill_switch": True}, "drawdown_kill_switch"),
])
def test_hard_gate_failures_block_and_flag(override, reason):
    res = tiny_exploration_gate(**_ok(**override))
    assert res["allowed"] is False
    assert res["decision_class"] == NO_TRADE_LABEL
    assert res["reason"] == reason
    assert res["hard_gate_block"] is True


def test_good_ev_cannot_override_a_hard_gate():
    # Even a fat positive EV cannot open a trade when the book is stale.
    res = tiny_exploration_gate(**_ok(fresh_book=False, edge=0.9, after_cost_ev=0.9,
                                      confidence=0.99))
    assert res["allowed"] is False
    assert res["hard_gate_block"] is True
    assert res["reason"] == "no_fresh_book"


def test_soft_floor_block_is_not_a_hard_gate():
    # Edge below the exploration floor blocks the trade, but is NOT a hard gate.
    res = tiny_exploration_gate(**_ok(edge=-0.5))
    assert res["allowed"] is False
    assert res["hard_gate_block"] is False
