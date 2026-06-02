"""Every simulated trade path must funnel through the deterministic RiskEngine.

All of pulse / crypto / stock / Polymarket open via ``TradingEngine._open_trade``,
which calls ``assess_trade``; arb (paper) executions go through the injected
``risk_gate``. These tests spy those choke points.
"""

from __future__ import annotations

import types

import pytest

import engine.engine as engine_mod
from engine.arb.execution import ArbExecutionEngine
from engine.config import Settings
from engine.engine import TradingEngine
from engine.risk import RiskDecision
from engine.storage import Store


@pytest.fixture
def trading_engine(tmp_path, monkeypatch):
    # No network during construction: pulse init calls crypto.get_spot().
    monkeypatch.setattr(engine_mod.crypto, "get_spot", lambda *a, **k: 100.0)
    monkeypatch.setattr(engine_mod.crypto, "order_book_imbalance", lambda *a, **k: 0.0)
    monkeypatch.setattr(engine_mod.crypto, "get_klines", lambda *a, **k: [])
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    s = Settings()
    s.data_dir = tmp_path
    store = Store(tmp_path / "test.sqlite3")
    eng = TradingEngine(s, store)
    eng._last_data_ts = engine_mod.time.time()  # fresh so stale-data check passes
    return eng


def test_open_trade_invokes_risk_engine_and_opens_when_approved(trading_engine, monkeypatch):
    calls = []
    real = trading_engine.assess_trade

    def spy(proposal):
        calls.append(proposal)
        return real(proposal)

    monkeypatch.setattr(trading_engine, "assess_trade", spy)

    tid = trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="unit test")

    assert len(calls) == 1, "every _open_trade must consult the RiskEngine"
    assert tid > 0, "an approved proposal should open a (paper) trade"
    assert len(trading_engine.store.open_trades()) == 1


def test_open_trade_blocked_when_risk_rejects(trading_engine, monkeypatch):
    # Force a rejection by activating the kill switch.
    ks = trading_engine.s.data_dir / "KILL_SWITCH"
    trading_engine.risk.limits.kill_switch_file = ks
    ks.write_text("halt")

    tid = trading_engine._open_trade(
        market="crypto", symbol="ETHUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", rationale="should be blocked")

    assert tid == 0, "a rejected proposal must NOT open a trade"
    assert trading_engine.store.open_trades() == []
    assert trading_engine._risk_rejection_count == 1
    assert trading_engine.risk_status()["recent_rejections"][0]["code"] == "KILL_SWITCH"


def test_arb_execution_uses_risk_gate_and_blocks_on_reject():
    placed = []
    gate_calls = []

    fake_gateway = types.SimpleNamespace(
        get_balance=lambda ex: {"USD": 1000.0},
        place_order=lambda *a, **k: placed.append((a, k)) or {"ok": True},
    )
    fake_ledger = types.SimpleNamespace(
        is_open_trade=lambda sym: False,
        mark_open=lambda sym: None,
        mark_closed=lambda sym: None,
        record=lambda rec: None,
        recent=lambda n: [],
        metrics=lambda: {},
    )
    fake_universe = types.SimpleNamespace(is_active=lambda sym: True)
    opp = {
        "symbol": "SOL", "buyExchange": "kraken", "sellExchange": "coinbase",
        "buyAsk": 100.0, "sellBid": 101.0, "grossPct": 1.0, "netPct": 0.5,
        "executionNetPct": 0.45, "estimatedProfit_1k": 4.5, "staleness_ms": 100.0,
        "tier": "B", "simulated": True,
    }
    fake_detector = types.SimpleNamespace(scan=lambda: [opp], simulate=True)
    fake_brain = types.SimpleNamespace(enabled=False)
    fake_circuit = types.SimpleNamespace(halted=False)

    def reject_gate(o, size):
        gate_calls.append((o["symbol"], size))
        return RiskDecision(approved=False, code="TEST_BLOCK", reasons=["unit test block"])

    ee = ArbExecutionEngine(
        detector=fake_detector, gateway=fake_gateway, ledger=fake_ledger,
        feeds=None, universe=fake_universe, brain=fake_brain,
        get_mode=lambda: "paper", circuit=fake_circuit, risk_gate=reject_gate)
    ee.enabled = True
    ee.paused_until = 0.0

    ee._cycle()

    assert gate_calls, "arb execution must consult the risk gate after pre-flight"
    assert placed == [], "a risk rejection must block the (paper) arb order legs"
    assert "risk" in (ee.last_skip or "").lower()
