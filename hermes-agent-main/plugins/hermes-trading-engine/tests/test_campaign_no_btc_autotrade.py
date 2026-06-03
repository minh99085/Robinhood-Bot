"""Campaign-safe profile disables the legacy BTC autotrade (PAPER ONLY).

Quant scope — *Compliance/Security*: the retired BTC 5-min pulse engine must stay
off during the Polymarket campaign (its fills are fantasy, not training signal),
and any legacy cross-exchange arbitrage stays disabled. Fail closed if either is on.
"""

from __future__ import annotations

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def test_safe_profile_disables_btc_autotrade(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.institutional_campaign_defaults()
    assert c.disable_btc_pulse_trading is True
    rep = campaign_safety_check(c)
    assert rep["btc_autotrade_disabled"] is True
    assert rep["checks"]["btc_autotrade_disabled"] is True
    assert rep["checks"]["legacy_arbitrage_disabled"] is True


def test_hte_autotrade_env_fails_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("HTE_AUTOTRADE", "1")
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is False
    assert rep["btc_autotrade_disabled"] is False
    assert "btc_autotrade" in rep["fail_closed_reason"]


def test_btc_autotrade_env_alias_fails_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("BTC_AUTOTRADE_ENABLED", "1")
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is False
    assert rep["btc_autotrade_disabled"] is False


def test_legacy_arbitrage_env_fails_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("ARB_EXECUTION_ENABLED", "1")
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is False
    assert rep["checks"]["legacy_arbitrage_disabled"] is False
