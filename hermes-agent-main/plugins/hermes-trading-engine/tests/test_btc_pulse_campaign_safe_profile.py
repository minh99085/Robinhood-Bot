"""BTC Pulse under the campaign-safe institutional profile."""

from __future__ import annotations

from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import TrainingConfig


def test_campaign_safe_without_pulse_keeps_it_disabled():
    cfg = TrainingConfig.institutional_campaign_defaults()
    assert cfg.btc_pulse_enabled is False
    safety = campaign_safety_check(cfg)
    assert safety["passed"] is True
    assert safety["btc_pulse_enabled"] is False


def test_campaign_safe_with_pulse_forces_safe_invariants():
    cfg = TrainingConfig.institutional_campaign_defaults(btc_pulse_enabled=True)
    assert cfg.btc_pulse_enabled is True
    assert cfg.btc_pulse_paper_only is True
    assert cfg.btc_pulse_isolated_learning is True
    assert cfg.btc_pulse_live_enabled is False
    assert cfg.btc_pulse_legacy_autotrade_enabled is False
    # campaign safety still passes and reports the pulse fields
    safety = campaign_safety_check(cfg)
    assert safety["passed"] is True
    assert safety["btc_pulse_enabled"] is True
    assert safety["btc_pulse_paper_only"] is True
    assert safety["btc_pulse_isolated_learning"] is True
    assert safety["btc_pulse_live_disabled"] is True
    assert safety["btc_pulse_legacy_autotrade_disabled"] is True


def test_campaign_safe_forces_off_unsafe_pulse_overrides():
    # Even if an operator tries to flip live/autotrade on, the campaign-safe
    # profile re-asserts them OFF (never enables a live path).
    cfg = TrainingConfig.institutional_campaign_defaults(
        btc_pulse_enabled=True, btc_pulse_live_enabled=True,
        btc_pulse_legacy_autotrade_enabled=True, btc_pulse_paper_only=False,
        btc_pulse_isolated_learning=False)
    assert cfg.btc_pulse_live_enabled is False
    assert cfg.btc_pulse_legacy_autotrade_enabled is False
    assert cfg.btc_pulse_paper_only is True
    assert cfg.btc_pulse_isolated_learning is True
    t = BtcPulsePaperTrainer(cfg)
    assert t.frozen is False               # safe + enabled -> active
    assert t.safety["passed"] is True


def test_campaign_safe_pulse_is_paper_active():
    cfg = TrainingConfig.institutional_campaign_defaults(btc_pulse_enabled=True)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1_700_000_000_000)
    t.tick(now_ms=1_700_000_000_000)
    st = t.status()
    assert st["btc_pulse_frozen"] is False
    assert st["paper_only"] is True
    assert st["live_enabled"] is False
