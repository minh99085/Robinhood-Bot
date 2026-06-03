"""BTC Pulse stays PAPER ONLY: namespace + status never assert live."""

from __future__ import annotations

from engine.training.btc_pulse import EXPERIMENT_ID, STRATEGY_FAMILY, BtcPulsePaperTrainer
from engine.training.config import TrainingConfig


def _trainer(**kw):
    cfg = TrainingConfig(btc_pulse_enabled=True, **kw)
    return BtcPulsePaperTrainer(cfg)


def test_namespace_is_paper_and_not_live():
    ns = _trainer().namespace()
    assert ns["experiment_id"] == EXPERIMENT_ID
    assert ns["strategy_family"] == STRATEGY_FAMILY
    assert ns["paper_only"] is True
    assert ns["live_enabled"] is False
    assert ns["isolated_learning"] is True


def test_status_reports_paper_only():
    st = _trainer().status()
    assert st["paper_only"] is True
    assert st["live_enabled"] is False
    assert st["legacy_autotrade_enabled"] is False
    assert st["experiment_id"] == EXPERIMENT_ID


def test_every_event_carries_namespace():
    prices = iter([100000.0 + i * 40 for i in range(200)])
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=-1.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000,
                             price_fn=lambda: next(prices), rng_seed=3)
    for i in range(40):
        ev = t.tick(now_ms=1_700_000_000_000 + i * 30_000)
        if ev.get("event") in ("no_trade", "paper_trade", "resolve"):
            assert ev["paper_only"] is True
            assert ev["live_enabled"] is False
            assert ev["experiment_id"] == EXPERIMENT_ID


def test_live_flag_keeps_module_frozen():
    t = _trainer(btc_pulse_live_enabled=True)
    assert t.frozen is True
    # frozen trainer never advances / trades
    out = t.tick(now_ms=1)
    assert out.get("frozen") is True
    assert t.status()["btc_pulse_paper_trades"] == 0
