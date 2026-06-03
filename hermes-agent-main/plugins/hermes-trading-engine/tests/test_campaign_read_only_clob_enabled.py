"""Campaign-safe profile: read-only Polymarket CLOB feed (PAPER ONLY).

Quant scope — *Data Acquisition & Ingestion* + *Compliance*: the CLOB v2 feed is
read-only (consume-only; never signs/submits). The campaign enables it for
realistic prices/freshness and fails closed if it is ever enabled non-read-only.
"""

from __future__ import annotations

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def test_safe_profile_enables_read_only_clob(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.institutional_campaign_defaults()
    assert c.clob_enabled is True and c.clob_read_only is True
    rep = campaign_safety_check(c)
    assert rep["clob_read_only_enabled"] is True
    assert rep["checks"]["clob_read_only"] is True


def test_clob_market_data_module_is_read_only():
    from engine.market_data import polymarket_ws
    assert polymarket_ws.CLOB_READ_ONLY is True
    assert polymarket_ws.is_read_only() is True
    # the consume-only client never exposes an order-submission method
    assert not hasattr(polymarket_ws.PolymarketWSClient, "submit_order")


def test_env_maps_to_read_only_clob(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("POLYMARKET_CLOB_ENABLED", "1")
    monkeypatch.setenv("POLYMARKET_CLOB_READ_ONLY", "1")
    c = TrainingConfig.from_env()
    assert c.clob_enabled is True
    assert c.clob_read_only is True


def test_fail_closed_if_clob_enabled_without_read_only(monkeypatch):
    _clean(monkeypatch)
    # bypass __post_init__ enforcement by NOT using the safe profile marker
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        clob_enabled=True, clob_read_only=False,
                                        realistic_fill_enabled=True)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["checks"]["clob_read_only"] is False
    assert "clob_read_only" in rep["fail_closed_reason"]
