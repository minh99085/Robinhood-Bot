"""Campaign-safe profile fails closed on ANY live/unsafe flag (PAPER ONLY).

Quant scope — *Compliance/Security/Operational Excellence*: a live, micro-live,
guarded-live, or production-execution flag, an un-frozen algorithm, a disabled
RiskEngine, or a disabled clean-label guard each blocks the campaign from
starting (fail closed). The verdict never enables live trading.
"""

from __future__ import annotations

import pytest

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def _safe():
    return TrainingConfig.institutional_campaign_defaults()


@pytest.mark.parametrize("flag", list(FORBIDDEN_LIVE_FLAGS))
def test_any_forbidden_live_flag_fails_closed(flag, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv(flag, "1")
    rep = campaign_safety_check(_safe())
    assert rep["passed"] is False
    assert rep["startup_safety_passed"] is False
    assert rep["live_disabled"] is False or rep["micro_live_disabled"] is False \
        or rep["guarded_live_disabled"] is False
    assert rep["fail_closed_reason"]


def test_guarded_live_flag_fails_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("GUARDED_LIVE_ENABLED", "1")
    rep = campaign_safety_check(_safe())
    assert rep["passed"] is False
    assert rep["guarded_live_disabled"] is False


def test_algorithm_freeze_false_fails_closed(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=False,
                                        clob_read_only=True, chainlink_read_only=True,
                                        realistic_fill_enabled=True)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["checks"]["algorithm_freeze"] is False
    assert "algorithm_freeze" in rep["fail_closed_reason"]


def test_risk_engine_disabled_fails_closed(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        realistic_fill_enabled=True, risk_engine_enabled=False)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["risk_gates_required"] is False
    assert "risk" in rep["fail_closed_reason"]


def test_clean_label_guard_disabled_fails_closed(monkeypatch):
    _clean(monkeypatch)
    c = TrainingConfig.aggressive_paper(campaign_enabled=True, algorithm_freeze_mode=True,
                                        realistic_fill_enabled=True, clean_label_guard=False)
    rep = campaign_safety_check(c)
    assert rep["passed"] is False
    assert rep["clean_label_guard_enabled"] is False
    assert "clean_label" in rep["fail_closed_reason"]
