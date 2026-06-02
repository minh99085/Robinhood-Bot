"""OrderManagementSystem lifecycle tests (risk gate, idempotency, cancel/replace)."""

from __future__ import annotations

from decimal import Decimal

from engine.execution.fees import FeeModel
from engine.execution.oms import OrderManagementSystem
from engine.execution.paper_broker import PaperBroker
from engine.execution.slippage import SlippageModel
from engine.execution.types import (
    OrderRejectReason,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from engine.market_data.orderbook import OrderbookState
from engine.schemas import RiskDecision
from engine.storage import Store


def _oms(tmp_path, **broker_kw):
    broker = PaperBroker(
        fee_model=FeeModel(taker_bps=Decimal("0"), maker_bps=Decimal("0"), min_fee=Decimal("0")),
        slippage_model=SlippageModel(slippage_bps=Decimal("0"), spread_aware=False),
        max_fill_depth_fraction=Decimal("1"), allow_reference=True, **broker_kw)
    return OrderManagementSystem(Store(tmp_path / "oms.sqlite3"), broker,
                                 mode_provider=lambda: "paper")


def _book():
    ob = OrderbookState("a1", "m1")
    ob.apply_book_event(bids=[{"price": "0.40", "size": "100"}],
                        asks=[{"price": "0.42", "size": "100"}])
    return ob


def _legacy_order(coid="") -> OrderRequest:
    return OrderRequest(client_order_id=coid, venue="crypto", market_id="BTCUSDT",
                        side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                        limit_price=Decimal("100"), quantity=Decimal("1"),
                        time_in_force=TimeInForce.IOC, venue_kind="legacy")


def _approved():
    return RiskDecision(approved=True, code="OK", proposal_id="p1")


def test_oms_requires_risk_approval(tmp_path):
    oms = _oms(tmp_path)
    rejected = RiskDecision(approved=False, code="stale_market_data", reasons=["x"])
    res = oms.submit(_legacy_order(), rejected, reference_price=Decimal("100"))
    assert res.status == OrderStatus.RISK_REJECTED
    assert res.fills == []
    assert oms.get_fills() == []  # no broker order / fill created


def test_oms_generates_idempotent_client_order_id(tmp_path):
    oms = _oms(tmp_path)
    order = _legacy_order(coid="co-fixed-1")
    r1 = oms.submit(order, _approved(), reference_price=Decimal("100"))
    assert r1.status == OrderStatus.FILLED
    # resubmit the SAME client_order_id -> must not double-book
    r2 = oms.submit(_legacy_order(coid="co-fixed-1"), _approved(), reference_price=Decimal("100"))
    assert r2.fills == []
    assert len(oms.store.get_fills_for_order("co-fixed-1")) == len(r1.fills) == 1


def test_cancel_open_order(tmp_path):
    oms = _oms(tmp_path)
    order = OrderRequest(client_order_id="co-open-1", venue="polymarket", market_id="m1",
                         asset_id="a1", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                         limit_price=Decimal("0.41"), quantity=Decimal("10"),
                         time_in_force=TimeInForce.GTC, venue_kind="pm")
    res = oms.submit(order, _approved(), book=_book())  # 0.41 < best_ask 0.42 -> rests
    assert res.status == OrderStatus.OPEN
    out = oms.cancel_order("co-open-1")
    assert out["ok"] is True and out["status"] == OrderStatus.CANCELLED
    assert oms.get_order("co-open-1")["status"] == OrderStatus.CANCELLED
    events = oms.store.get_order_events("co-open-1")
    assert any(e["event_type"] == "order_cancelled" for e in events)


def test_cancel_filled_order_rejected(tmp_path):
    oms = _oms(tmp_path)
    order = OrderRequest(client_order_id="co-fill-1", venue="polymarket", market_id="m1",
                         asset_id="a1", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                         limit_price=Decimal("0.45"), quantity=Decimal("10"),
                         time_in_force=TimeInForce.IOC, venue_kind="pm")
    res = oms.submit(order, _approved(), book=_book())
    assert res.status == OrderStatus.FILLED
    out = oms.cancel_order("co-fill-1")
    assert out["ok"] is False
    assert "cannot_cancel" in out["reason"]


def test_replace_order_creates_new_linked_order(tmp_path):
    oms = _oms(tmp_path)
    order = OrderRequest(client_order_id="co-rep-1", venue="polymarket", market_id="m1",
                         asset_id="a1", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                         limit_price=Decimal("0.41"), quantity=Decimal("10"),
                         time_in_force=TimeInForce.GTC, venue_kind="pm")
    oms.submit(order, _approved(), book=_book())
    out = oms.replace_order("co-rep-1", new_limit_price=Decimal("0.405"), new_quantity=Decimal("8"))
    assert out["ok"] is True
    assert oms.get_order("co-rep-1")["status"] == OrderStatus.CANCELLED
    new = oms.get_order(out["new_client_order_id"])
    assert new is not None
    assert new["parent_client_order_id"] == "co-rep-1"
    assert new["quantity"] == "8"


def test_storage_failure_fails_closed(tmp_path, monkeypatch):
    oms = _oms(tmp_path)
    monkeypatch.setattr(oms.store, "add_order", lambda record: False)  # simulate write failure
    res = oms.submit(_legacy_order(coid="co-fail-1"), _approved(), reference_price=Decimal("100"))
    assert res.status == OrderStatus.REJECTED
    assert res.reject_reason == OrderRejectReason.BROKER_UNAVAILABLE
    assert oms.get_fills() == []
