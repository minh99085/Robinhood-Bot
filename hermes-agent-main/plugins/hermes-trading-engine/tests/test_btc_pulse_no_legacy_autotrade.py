"""BTC Pulse never enables legacy BTC autotrade."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer, pulse_preflight
from engine.training.config import TrainingConfig


def test_legacy_autotrade_off_by_default():
    cfg = TrainingConfig(btc_pulse_enabled=True)
    assert cfg.btc_pulse_legacy_autotrade_enabled is False
    t = BtcPulsePaperTrainer(cfg)
    assert t.status()["legacy_autotrade_enabled"] is False
    assert t.safety["checks"]["btc_autotrade_disabled"] is True


def test_legacy_autotrade_flag_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_legacy_autotrade_enabled=True)
    t = BtcPulsePaperTrainer(cfg)
    assert t.frozen is True
    assert t.safety["fail_closed_reason"] == "btc_autotrade_disabled"


def test_btc_autotrade_env_fails_closed(monkeypatch):
    monkeypatch.setenv("BTC_AUTOTRADE_ENABLED", "1")
    cfg = TrainingConfig(btc_pulse_enabled=True)
    pf = pulse_preflight(cfg)
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "btc_autotrade_disabled"


def test_disable_btc_pulse_trading_unaffected():
    # The legacy DISABLE_BTC_PULSE_TRADING gate (engine.engine autotrade) is
    # independent and stays on; the isolated paper experiment does not touch it.
    cfg = TrainingConfig(btc_pulse_enabled=True)
    assert cfg.disable_btc_pulse_trading is True
