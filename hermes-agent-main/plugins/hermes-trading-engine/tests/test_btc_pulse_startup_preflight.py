"""BTC Pulse fail-closed preflight: prints resolved config + checks + status."""

from __future__ import annotations

from engine.training.btc_pulse import pulse_preflight, resolved_pulse_config
from engine.training.config import TrainingConfig

_REQUIRED = (
    "BTC_PULSE_ENABLED", "BTC_PULSE_PAPER_ONLY", "BTC_PULSE_ISOLATED_LEARNING",
    "BTC_PULSE_ALLOW_TRANSFER_LEARNING", "BTC_PULSE_LIVE_ENABLED",
    "BTC_AUTOTRADE_ENABLED", "btc_pulse_risk_gate_required",
    "btc_pulse_realistic_fill_required",
)


def test_resolved_config_has_required_keys():
    cfg = TrainingConfig(btc_pulse_enabled=True)
    resolved = resolved_pulse_config(cfg)
    for k in _REQUIRED:
        assert k in resolved


def test_disabled_preflight_passes_with_disabled_status():
    cfg = TrainingConfig()
    pf = pulse_preflight(cfg)
    assert pf["btc_pulse_status"] == "disabled"
    assert pf["passed"] is True


def test_enabled_safe_preflight_active():
    cfg = TrainingConfig(btc_pulse_enabled=True)
    pf = pulse_preflight(cfg)
    assert pf["btc_pulse_status"] == "active"
    assert pf["passed"] is True
    assert pf["checks"]["paper_only"] is True
    assert pf["checks"]["live_disabled"] is True
    assert pf["checks"]["btc_autotrade_disabled"] is True


def test_live_enabled_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_live_enabled=True)
    pf = pulse_preflight(cfg)
    assert pf["btc_pulse_status"] == "frozen"
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "live_disabled"


def test_autotrade_enabled_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_legacy_autotrade_enabled=True)
    pf = pulse_preflight(cfg)
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "btc_autotrade_disabled"


def test_paper_only_off_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_paper_only=False)
    pf = pulse_preflight(cfg)
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "paper_only"


def test_isolated_off_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_isolated_learning=False)
    pf = pulse_preflight(cfg)
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "isolated_learning"


def test_risk_engine_disabled_fails_closed():
    cfg = TrainingConfig(btc_pulse_enabled=True, risk_engine_enabled=False)
    pf = pulse_preflight(cfg)
    assert pf["passed"] is False
    assert pf["fail_closed_reason"] == "risk_gate_available"
