"""Campaign-safe profile: read-only Chainlink scanner/features (PAPER ONLY).

Quant scope — *Data Preprocessing & Feature Engineering* + *Compliance*: Chainlink
is read-only + advisory-only (never triggers a trade). The campaign enables it for
fair-value features and fails closed if enabled non-read-only.
"""

from __future__ import annotations

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def test_safe_profile_enables_read_only_chainlink(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.institutional_campaign_defaults()
    assert c.chainlink_enabled is True and c.chainlink_read_only is True
    rep = campaign_safety_check(c)
    assert rep["chainlink_read_only_enabled"] is True
    assert rep["checks"]["chainlink_read_only"] is True


def test_chainlink_modules_are_read_only():
    from engine.feeds import chainlink
    from engine.training import chainlink_scanner
    assert chainlink.CHAINLINK_READ_ONLY is True
    assert chainlink.is_read_only() is True
    assert chainlink_scanner.READ_ONLY is True


def test_env_maps_to_read_only_chainlink(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("CHAINLINK_ENABLED", "1")
    monkeypatch.setenv("CHAINLINK_READ_ONLY", "1")
    c = TrainingConfig.from_env()
    assert c.chainlink_enabled is True
    assert c.chainlink_read_only is True


def test_fail_closed_if_chainlink_enabled_without_read_only(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        chainlink_enabled=True, chainlink_read_only=False,
                                        realistic_fill_enabled=True)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["checks"]["chainlink_read_only"] is False
    assert "chainlink_read_only" in rep["fail_closed_reason"]
