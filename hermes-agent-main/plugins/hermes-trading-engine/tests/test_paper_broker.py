"""PaperBroker fill simulation (CLOB-backed + reference fallback)."""

from __future__ import annotations

from decimal import Decimal

from engine.execution.fees import FeeModel
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


def _book(bids, asks) -> OrderbookState:
    ob = OrderbookState("a1", "m1")
    ob.apply_book_event(bids=[{"price": p, "size": s} for p, s in bids],
                        asks=[{"price": p, "size": s} for p, s in asks])
    return ob


def _broker(depth="1", **kw) -> PaperBroker:
    return PaperBroker(
        fee_model=FeeModel(taker_bps=Decimal("0"), maker_bps=Decimal("0"), min_fee=Decimal("0")),
        slippage_model=SlippageModel(slippage_bps=Decimal("0"), spread_aware=False),
        max_fill_depth_fraction=Decimal(depth), **kw)


def _order(side, price, qty, tif=TimeInForce.IOC, venue_kind="pm") -> OrderRequest:
    return OrderRequest(client_order_id="", venue="polymarket", market_id="m1", asset_id="a1",
                        side=side, order_type=OrderType.MARKETABLE_LIMIT,
                        limit_price=Decimal(price), quantity=Decimal(qty),
                        time_in_force=tif, venue_kind=venue_kind)


def test_paper_buy_crosses_ask_and_fills_from_depth():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100"), ("0.45", "50")])
    res = _broker().execute(_order(OrderSide.BUY, "0.45", "30"), book=book, venue_kind="pm")
    assert res.status == OrderStatus.FILLED
    assert res.filled_quantity == Decimal("30")
    assert res.avg_fill_price == Decimal("0.42")
    assert res.fills[0].notional == Decimal("12.60")


def test_paper_sell_crosses_bid_and_fills_from_depth():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    res = _broker().execute(_order(OrderSide.SELL, "0.39", "30"), book=book, venue_kind="pm")
    assert res.status == OrderStatus.FILLED
    assert res.filled_quantity == Decimal("30")
    assert res.avg_fill_price == Decimal("0.40")


def test_partial_fill_when_depth_insufficient():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    res = _broker(depth="0.35").execute(_order(OrderSide.BUY, "0.45", "50"), book=book, venue_kind="pm")
    assert res.status == OrderStatus.PARTIALLY_FILLED
    assert res.filled_quantity == Decimal("35.00")
    assert res.remaining == Decimal("15.00")


def test_ioc_cancels_unfilled_remainder():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    res = _broker(depth="0.35").execute(
        _order(OrderSide.BUY, "0.45", "50", tif=TimeInForce.IOC), book=book, venue_kind="pm")
    assert res.status == OrderStatus.PARTIALLY_FILLED
    assert res.resting is False  # IOC: remainder cancelled, not rested
    assert res.filled_quantity == Decimal("35.00")


def test_fok_rejects_when_full_depth_unavailable():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    res = _broker(depth="0.35").execute(
        _order(OrderSide.BUY, "0.45", "50", tif=TimeInForce.FOK), book=book, venue_kind="pm")
    assert res.status == OrderStatus.REJECTED
    assert res.reject_reason == OrderRejectReason.INSUFFICIENT_DEPTH
    assert res.fills == []


def test_resting_order_does_not_fill_until_crossed():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    broker = _broker()
    order = _order(OrderSide.BUY, "0.41", "10", tif=TimeInForce.GTC)
    res = broker.execute(order, book=book, venue_kind="pm")
    assert res.status == OrderStatus.OPEN
    assert res.fills == []
    # book moves so the resting 0.41 bid is now marketable against a 0.41 ask
    book2 = _book(bids=[("0.40", "100")], asks=[("0.41", "100")])
    res2 = broker.check_resting(order, book2)
    assert res2.status == OrderStatus.FILLED
    assert res2.filled_quantity == Decimal("10")


def test_stale_book_rejects_order():
    book = _book(bids=[("0.40", "100")], asks=[("0.42", "100")])
    book.last_update_ms = 1  # ancient
    broker = _broker(reject_on_stale=True, stale_ms=1000)
    res = broker.execute(_order(OrderSide.BUY, "0.45", "10"), book=book, venue_kind="pm")
    assert res.status == OrderStatus.REJECTED
    assert res.reject_reason == OrderRejectReason.STALE_MARKET_DATA


def test_missing_book_rejects_prediction_market_order():
    broker = _broker(allow_pm_reference=False)
    res = broker.execute(_order(OrderSide.BUY, "0.50", "10", venue_kind="pm"),
                         book=None, reference_price=Decimal("0.50"), venue_kind="pm")
    assert res.status == OrderStatus.REJECTED
    assert res.reject_reason == OrderRejectReason.MISSING_ORDERBOOK


def test_reference_price_fallback_legacy_crypto():
    broker = _broker(allow_reference=True)
    order = OrderRequest(client_order_id="", venue="crypto", market_id="BTCUSDT",
                         side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                         limit_price=Decimal("100"), quantity=Decimal("2"),
                         time_in_force=TimeInForce.IOC, venue_kind="legacy")
    res = broker.execute(order, book=None, reference_price=Decimal("100"), venue_kind="legacy")
    assert res.status == OrderStatus.FILLED
    assert res.filled_quantity == Decimal("2")
    assert res.fills[0].liquidity_flag == "SIMULATED"
