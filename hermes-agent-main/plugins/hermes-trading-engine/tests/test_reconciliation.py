"""Reconciliation: rebuild positions from fills + detect overfills."""

from __future__ import annotations

from decimal import Decimal

from engine.execution.reconciliation import SEV_HIGH, ReconciliationService
from engine.execution.types import OrderSide
from engine.storage import Store


def _fill(store, coid, side, price, qty, ts):
    store.add_fill({
        "fill_id": f"fl-{coid}-{ts}", "client_order_id": coid, "broker_order_id": None,
        "venue": "crypto", "market_id": "BTCUSDT", "asset_id": None, "side": side,
        "price": price, "quantity": qty, "notional": str(Decimal(price) * Decimal(qty)),
        "fee": "0", "liquidity_flag": "SIMULATED", "ts_ms": ts})


def test_position_rebuilt_from_fills(tmp_path):
    store = Store(tmp_path / "recon.sqlite3")
    _fill(store, "o1", OrderSide.BUY, "100", "10", 1)
    _fill(store, "o1", OrderSide.BUY, "110", "10", 2)
    _fill(store, "o2", OrderSide.SELL, "120", "5", 3)
    positions = ReconciliationService(store).rebuild_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.quantity == Decimal("15")
    assert p.avg_price == Decimal("105")
    assert p.realized_pnl == Decimal("75")  # (120-105)*5


def test_reconciliation_detects_overfill(tmp_path):
    store = Store(tmp_path / "overfill.sqlite3")
    # an order for qty 10 ...
    store.add_order({
        "client_order_id": "o-over", "venue": "crypto", "market_id": "BTCUSDT",
        "side": OrderSide.BUY, "order_type": "MARKETABLE_LIMIT", "limit_price": "100",
        "quantity": "10", "notional": "1000", "time_in_force": "IOC", "status": "FILLED",
        "source": "t", "created_ts_ms": 1, "updated_ts_ms": 1})
    # ... but fills total 15 (overfill)
    _fill(store, "o-over", OrderSide.BUY, "100", "10", 1)
    _fill(store, "o-over", OrderSide.BUY, "100", "5", 2)
    report = ReconciliationService(store).run()
    assert report["severity"] == SEV_HIGH
    assert any(w["type"] == "overfill" for w in report["warnings"])
    # audit event persisted
    assert any(e["severity"] == SEV_HIGH for e in store.get_reconciliation_events(10))
