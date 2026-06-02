"""ShadowOMS — routes APPROVED shadow proposals to the Phase 3 PaperBroker ONLY.

There is no real broker, no live submit, no cancel endpoint. Simulated orders /
fills are written into the isolated shadow_* tables tagged with the shadow
session. This is programmatically distinct from operational paper mode.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from ..execution.paper_broker import PaperBroker
from ..execution.types import OrderRequest, OrderType
from .schemas import ShadowFill, ShadowOrder


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


class ShadowOMS:
    def __init__(self, store=None, broker: Optional[PaperBroker] = None,
                 session_id: str = ""):
        self.store = store
        self.broker = broker or PaperBroker()
        self.session_id = session_id
        self.open_orders: list[ShadowOrder] = []

    def submit(self, proposal, decision, *, book=None, reference_price=None,
               venue_kind: str = "pm", now_ms: Optional[int] = None) -> ShadowOrder:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        venue = (proposal.meta or {}).get("venue", "polymarket")
        outcome = (proposal.meta or {}).get("outcome", "YES")
        asset_id = (proposal.meta or {}).get("asset_id")
        qty = _d((proposal.meta or {}).get("quantity")
                 or (Decimal(str(proposal.notional)) / _d(proposal.price) if proposal.price else 0))
        order = ShadowOrder(
            shadow_session_id=self.session_id, decision_id=decision.decision_id,
            proposal_id=proposal.proposal_id, venue=venue, market_id=proposal.symbol or None,
            asset_id=asset_id, outcome=outcome, side=proposal.side,
            order_type="MARKETABLE_LIMIT", limit_price=_d(proposal.price),
            quantity=qty, notional=_d(proposal.notional), created_ts_ms=now, updated_ts_ms=now)

        req = OrderRequest(
            venue=venue, market_id=order.market_id or "", asset_id=asset_id, outcome=outcome,
            side=proposal.side, order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=_d(proposal.price), quantity=qty, venue_kind=venue_kind)
        order.client_order_id = req.client_order_id

        # Simulated execution via PaperBroker ONLY (never a real venue).
        res = self.broker.execute(req, book=book, reference_price=reference_price,
                                  venue_kind=venue_kind, now=now)
        order.status = getattr(res, "status", "REJECTED")
        order.reject_reason = getattr(res, "reject_reason", None)
        order.updated_ts_ms = now

        if self.store is not None:
            self.store.add_shadow_order(order.record())
        fills_out = []
        for f in getattr(res, "fills", []) or []:
            sf = ShadowFill(
                shadow_session_id=self.session_id, shadow_order_id=order.shadow_order_id,
                client_order_id=order.client_order_id, venue=venue, market_id=order.market_id,
                asset_id=asset_id, side=proposal.side, price=_d(f.price), quantity=_d(f.quantity),
                notional=_d(f.price) * _d(f.quantity), fee=_d(getattr(f, "fee", 0)),
                liquidity_flag=getattr(f, "liquidity_flag", "taker"), ts_ms=now)
            fills_out.append(sf)
            if self.store is not None:
                self.store.add_shadow_fill(sf.record())
        order._fills = fills_out  # type: ignore[attr-defined]
        if order.status in ("CREATED", "OPEN", "PARTIALLY_FILLED"):
            self.open_orders.append(order)
        return order

    def cancel_open(self, now_ms: Optional[int] = None) -> int:
        """On session stop, mark any open simulated orders cancelled (no venue call)."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        n = 0
        for order in self.open_orders:
            order.status = "CANCELLED"
            order.updated_ts_ms = now
            if self.store is not None:
                self.store.update_shadow_order(order.shadow_order_id,
                                               {"status": "CANCELLED", "updated_ts_ms": now})
            n += 1
        self.open_orders = []
        return n
