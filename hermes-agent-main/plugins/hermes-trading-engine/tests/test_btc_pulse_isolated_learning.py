"""BTC Pulse learning is isolated to its own experiment namespace."""

from __future__ import annotations

from engine.training.btc_pulse import (EXPERIMENT_ID, BtcPulsePaperTrainer,
                                       _IsolatedPulseLearner)
from engine.training.config import TrainingConfig


def test_learner_is_isolated_namespace():
    t = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True))
    assert isinstance(t.learner, _IsolatedPulseLearner)
    assert t.learner.namespace == EXPERIMENT_ID
    assert t.safety["checks"]["no_polymarket_learner_write"] is True


def test_isolated_learning_off_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_isolated_learning=False)
    t = BtcPulsePaperTrainer(cfg)
    assert t.frozen is True
    assert t.safety["fail_closed_reason"] == "isolated_learning"


def test_transfer_learning_blocked_by_default():
    t = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True))
    assert t.transfer_allowed is False
    assert t.transfer_gate_status() == "blocked"
    assert t.status()["btc_pulse_transfer_gate_status"] == "blocked"


def test_learner_updates_only_pulse_state():
    prices = iter([100000.0 + i * 50 for i in range(400)])
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=-1.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000,
                             price_fn=lambda: next(prices), rng_seed=9)
    for i in range(80):
        t.tick(now_ms=1_700_000_000_000 + i * 30_000)
    # the isolated learner accumulated pulse-only rounds and exposes no hook to
    # any Polymarket learner namespace.
    assert t.learner.settled >= 1
    assert not hasattr(t.learner, "record_signal")   # not a Polymarket OnlineLearner
    assert t.learner.namespace == EXPERIMENT_ID
