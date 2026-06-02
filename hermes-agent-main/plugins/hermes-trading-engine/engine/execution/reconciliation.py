"""ReconciliationService — rebuild positions from fills and detect mismatches.

Quant scope — *Compliance/Security/Operational Excellence* + *Risk Management*:
position/PnL reconciliation underpins the portfolio risk analytics (gross/net
exposure, drawdown, CVaR). More frequent, smaller aggressive paper trades must
still reconcile exactly so the risk analytics are not corrupted by the higher
trade rate.


Never mutates silently: every correction / mismatch is written to
``reconciliation_events`` with a severity. High-severity findings (e.g. an order
filled beyond its quantity) flag the system degraded so the OMS blocks new
orders until resolved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional

from .types import D, OrderSide, OrderStatus, Position, now_ms

SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_HIGH = "high"

_TOL = Decimal("0.000001")


def _fold_fills(fills: list[dict]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Standard signed position accounting over time-ordered fills.

    Returns (net_qty, avg_price_of_open_side, realized_pnl, fees_paid).
    """
    qty = Decimal(0)
    avg = Decimal(0)
    realized = Decimal(0)
    fees = Decimal(0)
    for f in sorted(fills, key=lambda x: x.get("ts_ms") or 0):
        price = D(f.get("price"))
        q = D(f.get("quantity"))
        fees += D(f.get("fee"))
        signed = q if f.get("side") == OrderSide.BUY else -q
        if qty == 0 or (qty > 0) == (signed > 0):
            # opening / adding to the same side
            new_qty = qty + signed
            if new_qty != 0:
                avg = (avg * abs(qty) + price * abs(signed)) / abs(new_qty)
            qty = new_qty
        else:
            # reducing / closing (possibly flipping)
            close_qty = min(abs(signed), abs(qty))
            direction = Decimal(1) if qty > 0 else Decimal(-1)
            realized += (price - avg) * close_qty * direction
            qty = qty + signed
            if (qty > 0) != (qty - signed > 0) and qty != 0:
                # flipped through zero: new open side starts at this price
                avg = price
            elif qty == 0:
                avg = Decimal(0)
    return qty, avg, realized, fees


class ReconciliationService:
    def __init__(self, store):
        self.store = store
        self.last_report: dict = {"severity": SEV_INFO, "warnings": [], "ts_ms": 0}

    def rebuild_positions(self, price_provider: Optional[Callable] = None) -> list[Position]:
        fills = self.store.get_fills(limit=100000)
        groups: dict[tuple, list[dict]] = {}
        for f in fills:
            key = (f.get("venue") or "", f.get("market_id") or "", f.get("asset_id"))
            groups.setdefault(key, []).append(f)
        positions: list[Position] = []
        for (venue, market_id, asset_id), fs in groups.items():
            qty, avg, realized, fees = _fold_fills(fs)
            unreal = Decimal(0)
            if price_provider and qty != 0:
                mark = price_provider(venue, market_id, asset_id)
                if mark is not None:
                    unreal = (D(mark) - avg) * qty
            positions.append(Position(
                venue=venue, market_id=market_id, asset_id=asset_id, quantity=qty,
                avg_price=avg, realized_pnl=realized, unrealized_pnl=unreal,
                fees_paid=fees, updated_ts_ms=now_ms()))
        return positions

    def run(self, price_provider: Optional[Callable] = None) -> dict:
        warnings: list[dict] = []
        severity = SEV_INFO

        # 1) rebuild + persist positions
        positions = self.rebuild_positions(price_provider)
        for p in positions:
            self.store.upsert_position(p.record())

        # 2) per-order integrity: filled qty vs ordered qty, status sanity
        orders = self.store.get_orders(limit=100000)
        for o in orders:
            coid = o.get("client_order_id")
            ordered = D(o.get("quantity"))
            fills = self.store.get_fills_for_order(coid)
            filled = sum((D(f.get("quantity")) for f in fills), Decimal(0))
            status = o.get("status")
            if filled > ordered + _TOL:
                severity = SEV_HIGH
                warnings.append({"type": "overfill", "client_order_id": coid,
                                 "ordered": str(ordered), "filled": str(filled)})
            if status == OrderStatus.FILLED and filled + _TOL < ordered:
                severity = max(severity, SEV_WARNING, key=_sev_rank)
                warnings.append({"type": "status_mismatch", "client_order_id": coid,
                                 "status": status, "ordered": str(ordered), "filled": str(filled)})
            if status == OrderStatus.OPEN and ordered > 0 and filled + _TOL >= ordered:
                severity = max(severity, SEV_WARNING, key=_sev_rank)
                warnings.append({"type": "open_but_filled", "client_order_id": coid})

        # 3) orphan fills (fill with no matching order)
        known = {o.get("client_order_id") for o in orders}
        for f in self.store.get_fills(limit=100000):
            if f.get("client_order_id") not in known:
                severity = max(severity, SEV_WARNING, key=_sev_rank)
                warnings.append({"type": "orphan_fill", "fill_id": f.get("fill_id")})

        report = {"severity": severity, "warnings": warnings, "ts_ms": now_ms(),
                  "position_count": len(positions)}
        # audit every non-clean run
        if warnings or severity != SEV_INFO:
            self.store.add_reconciliation_event(now_ms(), severity, "reconciliation", report)
        self.last_report = report
        return report


def _sev_rank(s: str) -> int:
    return {SEV_INFO: 0, SEV_WARNING: 1, SEV_HIGH: 2}.get(s, 0)
