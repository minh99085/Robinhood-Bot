"""BTC Pulse runs beside Polymarket without contaminating its learner state."""

from __future__ import annotations

from engine.training import PolymarketPaperTrainer, TrainingConfig


def _trainer(tmp_path, **kw):
    cfg = TrainingConfig(mode="observe_only", btc_pulse_enabled=True, **kw)
    return PolymarketPaperTrainer(cfg, data_dir=tmp_path)


def test_pulse_learner_is_separate_object(tmp_path):
    t = _trainer(tmp_path)
    assert t.btc_pulse is not None
    assert t.btc_pulse.learner is not t.learner
    assert type(t.btc_pulse.learner).__name__ == "_IsolatedPulseLearner"


def test_pulse_ticks_inside_training_loop(tmp_path):
    t = _trainer(tmp_path, btc_pulse_min_ev_threshold=-1.0)
    for _ in range(3):
        t.run_tick([])           # empty catalog: Polymarket does nothing
    assert t.btc_pulse.ticks >= 1
    st = t.status()
    assert st["btc_pulse"]["btc_pulse_enabled"] is True
    assert st["btc_pulse"]["experiment_id"] == "btc_5min_pulse"


def test_pulse_failure_does_not_block_polymarket(tmp_path):
    t = _trainer(tmp_path)

    def _boom(**kw):
        raise RuntimeError("pulse boom")

    t.btc_pulse.tick = _boom     # simulate a pulse failure
    out = t.run_tick([])         # Polymarket tick must still succeed
    assert out["tick"] >= 1
    assert t._btc_pulse_error and "pulse boom" in t._btc_pulse_error


def test_polymarket_learner_namespace_untouched_by_pulse(tmp_path):
    t = _trainer(tmp_path, btc_pulse_min_ev_threshold=-1.0)
    before = dict(t.learner.summary())
    for _ in range(3):
        t.run_tick([])
    after = dict(t.learner.summary())
    # Polymarket learner signal strategies never gain a btc_pulse entry.
    sig = after.get("signal_strategies", {}) or {}
    assert "btc_pulse" not in sig
    assert "btc_5min_pulse" not in sig
    # no Polymarket paper trades were created by the pulse experiment
    assert after.get("closed", before.get("closed")) == before.get("closed")
