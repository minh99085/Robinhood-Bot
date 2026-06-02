"""Every TradingEngine open path routes through the OMS (no direct insert)."""

from __future__ import annotations

import json
import time

import pytest

import engine.engine as engine_mod
from engine.config import Settings
from engine.engine import TradingEngine
from engine.execution.types import OrderAck, OrderRequest, OrderResult, OrderStatus
from engine.storage import Store


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.crypto, "get_spot", lambda *a, **k: 100.0)
    monkeypatch.setattr(engine_mod.crypto, "order_book_imbalance", lambda *a, **k: 0.0)
    monkeypatch.setattr(engine_mod.crypto, "get_klines", lambda *a, **k: [])
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    s = Settings()
    s.data_dir = tmp_path
    e = TradingEngine(s, Store(tmp_path / "paths.sqlite3"))
    e._last_data_ts = time.time()
    return e


def test_all_trade_paths_use_oms_not_direct_insert(eng, monkeypatch):
    calls = []
    real = eng.oms.submit

    def spy(order, decision, **kw):
        calls.append(order.venue)
        return real(order, decision, **kw)

    monkeypatch.setattr(eng.oms, "submit", spy)

    # crypto (legacy reference path -> fills)
    tid_c = eng._open_trade(market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1,
                            price=100.0, stake=10.0, status="open", risk_edge=0.1,
                            meta={"strategy": "crypto_momentum"})
    # pulse (legacy reference path -> fills)
    tid_p = eng._open_trade(market="pulse", symbol="BTCUSDT", side="UP", qty=1.0,
                            price=0.52, stake=5.0, status="open", risk_edge=0.05,
                            meta={"strategy": "pulse"})
    # polymarket (PM, no CLOB book, PM reference disabled by default -> rejected)
    tid_pm = eng._open_trade(market="polymarket", symbol="mkt-x", side="YES", qty=10.0,
                             price=0.7, stake=20.0, status="open", risk_edge=0.2,
                             meta={"strategy": "polymarket"})

    assert calls == ["crypto", "pulse", "polymarket"], "all opens must go through OMS.submit"
    assert tid_c > 0 and tid_p > 0
    assert tid_pm == 0, "PM order with no CLOB book + PM reference disabled must not fill"

    # the filled legacy rows are auditable back to an OMS client_order_id
    rows = eng.store.recent_trades(5)
    assert rows, "filled opens should still populate the legacy trade view"
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        assert meta.get("client_order_id"), "every projected trade must carry its OMS order id"


def test_no_fill_means_no_direct_trade_insert(eng, monkeypatch):
    before = len(eng.store.recent_trades(100))

    def no_fill(order, decision, **kw):
        return OrderResult(order=order,
                           ack=OrderAck(order.client_order_id, False, OrderStatus.REJECTED, "x"),
                           fills=[], status=OrderStatus.REJECTED, reject_reason="x")

    monkeypatch.setattr(eng.oms, "submit", no_fill)
    tid = eng._open_trade(market="crypto", symbol="ETHUSDT", side="BUY", qty=0.1,
                          price=100.0, stake=10.0, status="open", risk_edge=0.1,
                          meta={"strategy": "crypto_momentum"})
    assert tid == 0
    assert len(eng.store.recent_trades(100)) == before  # no legacy row inserted on no-fill
