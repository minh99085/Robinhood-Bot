"""BTC Pulse status + report visibility."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.config import TrainingConfig
from engine.training.reports import _markdown

_REQUIRED_KEYS = (
    "btc_pulse_enabled", "btc_pulse_frozen", "btc_pulse_ticks", "btc_pulse_rounds_seen",
    "btc_pulse_decisions", "btc_pulse_no_trade_decisions", "btc_pulse_paper_trades",
    "btc_pulse_rejected_trades", "btc_pulse_rejection_reasons",
    "btc_pulse_ev_positive_count", "btc_pulse_ev_negative_rejected_count",
    "btc_pulse_win_rate", "btc_pulse_sharpe", "btc_pulse_sortino", "btc_pulse_calmar",
    "btc_pulse_max_drawdown", "btc_pulse_brier", "btc_pulse_log_loss", "btc_pulse_ece",
    "btc_pulse_realistic_fill_pnl", "btc_pulse_after_cost_pnl",
    "btc_pulse_transfer_gate_status", "btc_pulse_last_tick_ts", "btc_pulse_last_error",
)


def test_status_has_all_required_metrics():
    st = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True)).status()
    for k in _REQUIRED_KEYS:
        assert k in st, k


def test_frozen_false_after_activation():
    t = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True),
                             clock=lambda: 1_700_000_000_000)
    t.tick(now_ms=1_700_000_000_000)
    assert t.status()["btc_pulse_frozen"] is False


def test_markdown_report_includes_pulse_section():
    pulse_status = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True)).status()
    status = {"mode": "paper_train", "pnl": {}, "scan_metrics": {}, "risk": {},
              "learning": {}, "feedback": {}, "safety": {}, "btc_pulse": pulse_status}
    md = _markdown(status, "run-test")
    assert "BTC 5-min Pulse" in md
    assert "paper_only" in md


def test_markdown_report_shows_off_when_disabled():
    status = {"mode": "paper_train", "pnl": {}, "scan_metrics": {}, "risk": {},
              "learning": {}, "feedback": {}, "safety": {},
              "btc_pulse": {"btc_pulse_enabled": False}}
    md = _markdown(status, "run-test")
    assert "BTC Pulse OFF" in md
