"""BTC Pulse config: frozen by default, unfreezes only when explicitly enabled."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.config import TrainingConfig


def test_frozen_by_default():
    cfg = TrainingConfig()
    assert cfg.btc_pulse_enabled is False
    t = BtcPulsePaperTrainer(cfg)
    assert t.frozen is True
    assert t.status()["btc_pulse_enabled"] is False
    assert t.status()["btc_pulse_frozen"] is True


def test_unfreezes_when_enabled_flag_set():
    cfg = TrainingConfig(btc_pulse_enabled=True)
    t = BtcPulsePaperTrainer(cfg)
    assert t.frozen is False
    assert t.status()["btc_pulse_enabled"] is True
    assert t.status()["btc_pulse_frozen"] is False


def test_unfreezes_from_env(monkeypatch):
    monkeypatch.setenv("BTC_PULSE_ENABLED", "1")
    cfg = TrainingConfig.from_env()
    assert cfg.btc_pulse_enabled is True
    assert cfg.btc_pulse_paper_only is True
    assert cfg.btc_pulse_isolated_learning is True
    assert cfg.btc_pulse_live_enabled is False
    assert cfg.btc_pulse_legacy_autotrade_enabled is False


def test_defaults_are_paper_only_and_isolated():
    cfg = TrainingConfig(btc_pulse_enabled=True)
    assert cfg.btc_pulse_paper_only is True
    assert cfg.btc_pulse_isolated_learning is True
    assert cfg.btc_pulse_allow_transfer_learning is False
    assert cfg.btc_pulse_live_enabled is False
    assert cfg.btc_pulse_require_risk_gate is True
    assert cfg.btc_pulse_require_realistic_fill is True


def test_numeric_clamps_applied():
    cfg = TrainingConfig(btc_pulse_enabled=True,
                         btc_pulse_max_paper_notional_per_trade=99999.0,
                         btc_pulse_tick_seconds=0, btc_pulse_round_seconds=1)
    assert cfg.btc_pulse_max_paper_notional_per_trade <= 50.0
    assert cfg.btc_pulse_tick_seconds >= 1
    assert cfg.btc_pulse_round_seconds >= cfg.btc_pulse_tick_seconds
