"""BTC 5-min PULSE paper market runs in PARALLEL with Polymarket paper training.

The legacy crypto pulse engine is frozen by default under Polymarket-only mode.
When ``btc_pulse_paper_enabled`` is set it may open the BTC 5-min PULSE market in
PARALLEL — but PAPER ONLY (never live, never the legacy stock/Polymarket paths),
still through the RiskEngine, and without tripping the campaign-safe guard.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.config import Settings
from engine.engine import TradingEngine
from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig

_can_open = TradingEngine._can_open  # unbound; test the gate without a full engine


def _eng(*, polymarket_only=True, pulse_paper=False, autotrade=False, live=False,
         day_breached=False, circuit_ok=True):
    s = SimpleNamespace(polymarket_only_mode=polymarket_only,
                        btc_pulse_paper_enabled=pulse_paper)
    return SimpleNamespace(
        s=s, autotrade=autotrade, _live=lambda: live,
        _daily_loss_breached=lambda: day_breached,
        circuit=SimpleNamespace(trading_allowed=lambda: circuit_ok))


def test_pulse_frozen_by_default_under_polymarket_only():
    e = _eng(polymarket_only=True, pulse_paper=False, autotrade=False)
    assert _can_open(e, "pulse") is False


def test_pulse_paper_unfrozen_in_parallel_when_enabled():
    # parallel: Polymarket-only mode ON (training engine owns Polymarket) AND the
    # BTC pulse paper market explicitly enabled -> pulse may open (paper).
    e = _eng(polymarket_only=True, pulse_paper=True, autotrade=False, live=False)
    assert _can_open(e, "pulse") is True


def test_only_pulse_is_unfrozen_not_legacy_markets():
    e = _eng(polymarket_only=True, pulse_paper=True, autotrade=False)
    # the legacy stock / legacy-polymarket open paths stay frozen
    assert _can_open(e, "generic") is False
    assert _can_open(e) is False


def test_pulse_paper_never_opens_when_live():
    e = _eng(polymarket_only=True, pulse_paper=True, autotrade=False, live=True)
    assert _can_open(e, "pulse") is False  # live is never reachable via paper pulse


def test_pulse_paper_respects_daily_loss_breach():
    e = _eng(polymarket_only=True, pulse_paper=True, autotrade=False, day_breached=True)
    assert _can_open(e, "pulse") is False


def test_pulse_paper_works_without_polymarket_only():
    e = _eng(polymarket_only=False, pulse_paper=True, autotrade=False)
    assert _can_open(e, "pulse") is True


def test_settings_flag_default_off_and_constructible():
    assert Settings().btc_pulse_paper_enabled is False
    assert Settings(btc_pulse_paper_enabled=True).btc_pulse_paper_enabled is True


def _clean(monkeypatch):
    for f in (*FORBIDDEN_LIVE_FLAGS, "HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED",
              "ARB_EXECUTION_ENABLED", "HTE_BTC_PULSE_PAPER_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def test_paper_pulse_parallel_does_not_trip_campaign_safety(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("HTE_BTC_PULSE_PAPER_ENABLED", "1")  # parallel paper pulse ON
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is True                  # campaign-safe stays green
    assert rep["btc_autotrade_disabled"] is True  # no LIVE/real BTC autotrade
    assert rep["btc_pulse_paper_parallel"] is True  # transparently surfaced


def test_real_btc_autotrade_still_fails_campaign_safety(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("HTE_AUTOTRADE", "1")       # legacy global autotrade -> unsafe
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is False
    assert rep["btc_autotrade_disabled"] is False
