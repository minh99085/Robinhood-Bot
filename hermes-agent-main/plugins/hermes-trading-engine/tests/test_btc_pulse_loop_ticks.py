"""BTC Pulse loop ticks, records no-trade + paper-trade decisions (risk-gated)."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.config import TrainingConfig


class _RejectRisk:
    """Deterministic risk engine that rejects every proposal."""

    def evaluate(self, proposal, ctx):
        class _D:
            approved = False
            code = "TEST_REJECT"
        return _D()


def _rising(n=400):
    seq = iter([100000.0 + i * 50 for i in range(n)])
    return lambda: next(seq)


def test_ticks_at_least_once():
    t = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True),
                             clock=lambda: 1_700_000_000_000)
    t.tick(now_ms=1_700_000_000_000)
    assert t.ticks >= 1
    assert t.frozen is False
    assert t.status()["btc_pulse_frozen"] is False


def test_records_no_trade_decisions():
    # impossible EV threshold -> every decision is a no-trade
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=10.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000, price_fn=_rising())
    for i in range(30):
        t.tick(now_ms=1_700_000_000_000 + i * 30_000)
    assert t.no_trade_decisions >= 1
    assert t.paper_trades == 0
    assert sum(t.rejection_reasons.values()) >= 1


def test_records_paper_trades_after_risk_approval():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=-1.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000,
                             price_fn=_rising(), rng_seed=11)
    for i in range(80):
        t.tick(now_ms=1_700_000_000_000 + i * 30_000)
    assert t.paper_trades >= 1
    assert t.decisions >= t.paper_trades


def test_risk_rejection_blocks_paper_trade():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=-1.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000,
                             price_fn=_rising(), rng_seed=11, risk_engine=_RejectRisk())
    for i in range(80):
        t.tick(now_ms=1_700_000_000_000 + i * 30_000)
    assert t.paper_trades == 0                       # never trades without approval
    assert t.rejection_reasons.get("risk_rejected", 0) >= 1


def test_resolves_rounds_and_updates_isolated_learner():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_min_ev_threshold=-1.0)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000,
                             price_fn=_rising(), rng_seed=5)
    for i in range(80):
        t.tick(now_ms=1_700_000_000_000 + i * 30_000)
    assert t.learner.settled >= 1                    # rounds resolved -> learner updated
    st = t.status()
    assert st["btc_pulse_rounds_seen"] >= 1
    assert st["btc_pulse_last_tick_ts"] is not None
