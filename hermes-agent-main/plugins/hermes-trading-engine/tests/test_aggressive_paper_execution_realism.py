"""Aggressive paper execution realism — optimistic vs realistic.

Realistic fills generate MORE attempted trades but fewer guaranteed fills, with
slippage / partial fills / markout, so paper PnL/feedback is honest. Execution
diagnostics (fill rate, partial-fill rate, slippage, markout, failed bundle rate)
are reported. PAPER ONLY; deterministic.
"""

from __future__ import annotations

from decimal import Decimal

from engine.execution.paper_broker import PaperBroker
from engine.execution.types import (D, OrderRequest, OrderSide, OrderStatus,
                                     OrderType, TimeInForce)
from engine.replay.metrics import execution_diagnostics


class FakeBook:
    def __init__(self, *, best_bid, best_ask, depth=1000.0, stale=False):
        self.best_bid = D(best_bid)
        self.best_ask = D(best_ask)
        self.spread = self.best_ask - self.best_bid
        self.asks = {self.best_ask: D(depth)}
        self.bids = {self.best_bid: D(depth)}
        self.resolved = False
        self._stale = stale

    def is_stale(self, _ms):
        return self._stale


def _order(coid, qty="200", price="0.56"):
    return OrderRequest(client_order_id=coid, venue="polymarket", market_id="m1",
                        asset_id="a1", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                        limit_price=D(price), quantity=D(qty), time_in_force=TimeInForce.IOC,
                        venue_kind="pm")


def test_optimistic_vs_realistic_fill_rate_and_pnl():
    book = FakeBook(best_bid="0.50", best_ask="0.56", depth=150.0)   # marginal book
    optimistic = PaperBroker(reject_on_stale=False)                  # deterministic depth fills
    realistic = PaperBroker(realistic=True, reject_on_stale=False)

    def run(broker):
        orders, fills = [], []
        for i in range(200):
            res = broker.execute(_order(f"o-{i}"), book=book)
            orders.append({"status": res.status,
                           "filled_quantity": str(res.filled_quantity),
                           "quantity": "200"})
            for f in res.fills:
                fills.append({"price": str(f.price), "quantity": str(f.quantity),
                              "fee": str(f.fee)})
        return orders, fills

    o_orders, o_fills = run(optimistic)
    r_orders, r_fills = run(realistic)
    o_fill_rate = sum(1 for o in o_orders if o["status"] in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)) / 200
    r_fill_rate = sum(1 for o in r_orders if o["status"] in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)) / 200
    # realistic is NOT guaranteed -> strictly fewer fills than the optimistic model
    assert r_fill_rate < o_fill_rate
    assert 0.0 < r_fill_rate < 1.0


def test_execution_diagnostics_shape():
    book = FakeBook(best_bid="0.50", best_ask="0.55", depth=120.0)
    broker = PaperBroker(realistic=True, reject_on_stale=False)
    orders, fills = [], []
    for i in range(100):
        res = broker.execute(_order(f"d-{i}", qty="300"), book=book)
        orders.append({"status": res.status, "filled_quantity": str(res.filled_quantity),
                       "quantity": "300", "limit_price": "0.55"})
        for f in res.fills:
            fills.append({"price": str(f.price), "quantity": str(f.quantity), "fee": str(f.fee),
                          "limit_price": "0.55", "side": "BUY"})
    diag = execution_diagnostics(orders, fills)
    for k in ("fill_rate", "partial_fill_rate", "avg_slippage_bps", "avg_markout_bps",
              "failed_bundle_rate", "order_count", "fill_count"):
        assert k in diag
    assert 0.0 <= diag["fill_rate"] <= 1.0
    assert 0.0 <= diag["partial_fill_rate"] <= 1.0


def test_execution_diagnostics_failed_bundle_rate():
    bundles = [{"fully_hedged": True}, {"fully_hedged": False}, {"fully_hedged": False}]
    diag = execution_diagnostics([], [], bundles=bundles)
    assert abs(diag["failed_bundle_rate"] - (2 / 3)) < 1e-5
    assert diag["bundle_count"] == 3


def test_aggressive_generates_more_attempts_but_not_guaranteed():
    # Aggressive: smaller size, more orders; realistic fills mean some miss.
    book = FakeBook(best_bid="0.51", best_ask="0.55", depth=200.0)
    broker = PaperBroker(realistic=True, reject_on_stale=False)
    attempts = 300
    statuses = [broker.execute(_order(f"a-{i}", qty="120"), book=book).status
                for i in range(attempts)]
    fills = sum(1 for s in statuses if s in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED))
    misses = attempts - fills
    assert fills > 0 and misses > 0          # more attempts, realistic (not guaranteed) fills
