"""BTC Pulse feedback acceleration: shadow near-threshold rounds, never forces."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.config import TrainingConfig

_NOW = 1_700_000_000_000


def _run(cfg, price_fn, ticks=80, seed=5):
    t = BtcPulsePaperTrainer(cfg, clock=lambda: _NOW, price_fn=price_fn, rng_seed=seed)
    for i in range(ticks):
        t.tick(now_ms=_NOW + i * 30_000)
    return t


def test_records_a_decision_every_round():
    cfg = TrainingConfig(btc_pulse_enabled=True, feedback_accelerator_enabled=True)
    rising = iter([100000.0 + i * 50 for i in range(400)])
    t = _run(cfg, lambda: next(rising))
    # one decision per round opened
    assert t.decisions == t.rounds_seen
    assert t.rounds_seen >= 1
    assert t.status()["btc_pulse_feedback_acceleration_enabled"] is True


def test_near_threshold_no_trade_becomes_shadow_decision():
    # Impossible EV threshold => positive-EV rounds become below_ev_threshold
    # no-trades. With acceleration ON they are recorded as shadow decisions.
    cfg = TrainingConfig(btc_pulse_enabled=True, feedback_accelerator_enabled=True,
                         btc_pulse_min_ev_threshold=5.0)
    rising = iter([100000.0 + i * 60 for i in range(400)])
    t = _run(cfg, lambda: next(rising))
    assert t.paper_trades == 0                     # min_ev too high to ever trade
    assert t.shadow_decisions >= 1                 # near-threshold rounds shadowed
    assert t.status()["btc_pulse_shadow_decisions"] >= 1


def test_clearly_negative_ev_is_not_shadowed_and_never_trades():
    # Flat price => coin-flip regime => EV clearly negative (below -0.03 floor).
    cfg = TrainingConfig(btc_pulse_enabled=True, feedback_accelerator_enabled=True)
    t = _run(cfg, lambda: 100000.0)
    assert t.paper_trades == 0
    assert t.shadow_decisions == 0                 # clearly -EV is NOT shadowed
    assert t.no_trade_decisions >= 1


def test_acceleration_off_records_no_shadow():
    cfg = TrainingConfig(btc_pulse_enabled=True, feedback_accelerator_enabled=False,
                         btc_pulse_min_ev_threshold=5.0)
    rising = iter([100000.0 + i * 60 for i in range(400)])
    t = _run(cfg, lambda: next(rising))
    assert t.shadow_decisions == 0
    assert t.status()["btc_pulse_feedback_acceleration_enabled"] is False


def test_pulse_learner_stays_isolated():
    cfg = TrainingConfig(btc_pulse_enabled=True, feedback_accelerator_enabled=True,
                         btc_pulse_min_ev_threshold=5.0)
    rising = iter([100000.0 + i * 60 for i in range(400)])
    t = _run(cfg, lambda: next(rising))
    assert t.learner.namespace == "btc_5min_pulse"
    assert not hasattr(t.learner, "record_signal")
