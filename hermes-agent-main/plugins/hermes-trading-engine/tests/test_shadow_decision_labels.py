"""Shadow decisions + no-trade labels are recorded and scored after resolution."""

from __future__ import annotations

from engine.training.feedback_accelerator import (NO_TRADE_LABEL, NoTradeLabel,
                                                  SHADOW_DECISION_ONLY, ShadowDecision)


def _shadow(ev=0.05, price=0.45):
    return ShadowDecision(market_id="m1", ts_ms=1_700_000_000_000,
                          hypothetical_side="yes", hypothetical_price=price,
                          hypothetical_ev=ev, blocker_reason="edge_too_low",
                          probability=0.6, edge=0.04)


def test_shadow_default_class_and_unresolved():
    s = _shadow()
    assert s.decision_class == SHADOW_DECISION_ONLY
    assert s.resolved is False
    assert s.would_have_won is None


def test_shadow_scored_win_marks_blocker_incorrect():
    # A positive-EV shadow that WOULD have won => the blocker was a missed chance.
    s = _shadow(ev=0.05).score(realized_outcome=1)
    assert s.resolved is True
    assert s.would_have_won is True
    assert s.would_have_lost is False
    assert s.blocker_correct is False
    assert s.realized_edge is not None


def test_shadow_scored_loss_marks_blocker_correct():
    s = _shadow(ev=0.05).score(realized_outcome=0)
    assert s.would_have_won is False
    assert s.blocker_correct is True


def test_shadow_nonpositive_ev_blocker_always_correct():
    s = _shadow(ev=-0.02).score(realized_outcome=1)
    assert s.blocker_correct is True            # never should have traded anyway


def test_no_trade_label_scoring():
    lbl = NoTradeLabel(market_id="m1", ts_ms=1, probability=0.6, edge=0.04,
                       rejection_reason="depth_too_thin")
    assert lbl.decision_class == NO_TRADE_LABEL
    # would have won (edge>0 and outcome=1) => no-trade was incorrect
    lbl.score(realized_outcome=1)
    assert lbl.no_trade_correct is False
    assert lbl.blocker_correct is False


def test_no_trade_label_correct_when_loss():
    lbl = NoTradeLabel(market_id="m1", ts_ms=1, probability=0.6, edge=0.04,
                       rejection_reason="naive_price_extreme")
    lbl.score(realized_outcome=0)
    assert lbl.no_trade_correct is True
    assert lbl.blocker_correct is True


def test_no_trade_label_nonpositive_edge_always_correct():
    lbl = NoTradeLabel(market_id="m1", ts_ms=1, probability=0.5, edge=-0.01,
                       rejection_reason="edge_too_low")
    lbl.score(realized_outcome=1)
    assert lbl.no_trade_correct is True
