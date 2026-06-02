"""Arbitrage is permanently removed/disabled — proof tests.

Cross-exchange arbitrage must never start, never trade, and the API/dashboard
must report it disabled. PAPER ONLY everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.arb.execution import (ArbExecutionEngine, ARBITRAGE_PERMANENTLY_DISABLED)

PLUGIN = Path(__file__).resolve().parents[1]


class _Ledger:
    def recent(self, n=20):
        return []

    def metrics(self):
        return {}


class _Detector:
    def __init__(self):
        self.scans = 0
        self.simulate = False

    def scan(self):
        self.scans += 1
        return [{"symbol": "BTC", "buyExchange": "a", "sellExchange": "b"}]


def _arb(detector=None):
    return ArbExecutionEngine(
        detector=detector or _Detector(), gateway=None, ledger=_Ledger(),
        feeds=None, universe=None, brain=None, get_mode=lambda: "paper",
        circuit=None)


def test_arbitrage_disabled_by_default(monkeypatch):
    # even with ARB_EXECUTION_ENABLED=1, arbitrage stays OFF (hard constant)
    monkeypatch.setenv("ARB_EXECUTION_ENABLED", "1")
    assert ARBITRAGE_PERMANENTLY_DISABLED is True
    arb = _arb()
    assert arb.enabled is False
    assert arb.snapshot()["enabled"] is False
    assert arb.snapshot()["permanently_disabled"] is True


def test_arbitrage_not_called_from_engine_tick():
    # the legacy engine tick path must not import/instantiate the arb executor
    src = (PLUGIN / "engine" / "engine.py").read_text(encoding="utf-8")
    assert "ArbExecutionEngine" not in src
    # start() never spawns the scan loop (permanently disabled)
    arb = _arb()
    arb.start()
    assert arb._thread is None


def test_no_arbitrage_orders_or_fills_created():
    det = _Detector()
    arb = _arb(det)
    arb._cycle()  # disabled -> returns immediately
    assert det.scans == 0          # detector never scanned
    assert arb.last_opps == []     # no opportunities recorded
    assert arb.snapshot()["recent_trades"] == []


def test_dashboard_has_no_arbitrage_panel():
    html = (PLUGIN / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="arb-panel" style="display:none"' in html
    js = (PLUGIN / "web" / "app.js").read_text(encoding="utf-8")
    # renderArb force-hides the panel and the toggle is inert
    assert 'panel.style.display = "none"' in js


def test_arbitrage_api_returns_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    try:
        from engine.app import api_arb_toggle, api_arb
    except Exception as exc:  # pragma: no cover - app import needs full env
        pytest.skip(f"app import unavailable: {exc}")
    assert api_arb_toggle("on") == {"arb_enabled": False, "permanently_disabled": True,
                                    "reason": "arbitrage removed — Polymarket-only PAPER training"}
    assert api_arb()["enabled"] is False
