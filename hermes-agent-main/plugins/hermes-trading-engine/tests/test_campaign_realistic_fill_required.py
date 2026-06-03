"""Campaign-safe profile requires realistic-fill simulation (PAPER ONLY).

Quant scope — *Execution Engine CLOB v2 Simulation* + *Backtesting & Simulation*:
the campaign demands realistic fills (slippage + depth, NO fantasy reference-price
fills, stale books rejected) so paper feedback is not an optimistic artifact.
"""

from __future__ import annotations

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def test_safe_profile_enables_realistic_fill(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.institutional_campaign_defaults()
    assert c.realistic_fill_enabled is True
    assert c.allow_pm_reference_price_fills is False  # no fantasy fills
    assert c.reject_on_stale_book is True
    rep = campaign_safety_check(c)
    assert rep["realistic_fill_enabled"] is True
    assert rep["checks"]["realistic_fill"] is True


def test_paper_broker_never_submits_live():
    from engine.execution import paper_broker
    assert paper_broker.PAPER_BROKER_LIVE_SUBMISSION is False
    assert paper_broker.is_paper_only() is True


def test_fail_closed_if_realistic_fill_disabled(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        realistic_fill_enabled=False)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["checks"]["realistic_fill"] is False
    assert "realistic_fill" in rep["fail_closed_reason"]


def test_fail_closed_if_fantasy_reference_price_fills_allowed(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        realistic_fill_enabled=True,
                                        allow_pm_reference_price_fills=True)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["checks"]["realistic_fill"] is False
