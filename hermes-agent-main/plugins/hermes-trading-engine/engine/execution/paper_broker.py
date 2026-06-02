"""PaperBroker — simulated fills (PAPER ONLY).

Quant scope — *Execution Engine CLOB v2 simulation* + *Backtesting & Simulation*:
depth-limited, slippage-aware simulated fills. Aggressive paper sizing uses
SMALLER orders against the same depth model, so liquidity-adjusted sizing and
the per-leg depth limits used by Bregman bundle allocation map directly onto the
broker's fill behaviour. Never places, signs, or cancels on a live venue.

Two execution paths:

1. **CLOB-backed** — when a local order book / BBO exists for the venue+asset.
   Models marketable-limit crossing, per-level depth consumption with a
   queue-position haircut, partial fills, IOC/FOK/GTC behavior, resting orders,
   slippage and fees. Stale books are rejected (not fantasy-filled).

2. **Reference-price fallback** — only for legacy crypto/stock/pulse paths with
   no CLOB book. Conservative slippage + fees, fills flagged SIMULATED.
   Prediction-market venues default to CLOB-only (reference fills disabled).

This class NEVER contacts a real exchange. It has no place/cancel network calls.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from .fees import FeeModel
from .slippage import SlippageModel
from .types import (
    D,
    ExecutionResult,
    Fill,
    LiquidityFlag,
    OrderRejectReason,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    now_ms,
)


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class KalshiBookView:
    """Adapts a venue-neutral NormalizedBinaryOrderbook (lists of price/size
    levels) to the dict-based book interface PaperBroker expects. The normalized
    book is already outcome-correct: BUY consumes ``asks`` (derived complement),
    SELL consumes ``bids``. Marked stale if the underlying book is unreliable so
    the broker rejects instead of fantasy-filling."""

    def __init__(self, normalized, *, resolved: bool = False):
        self.bids = {lvl.price: lvl.size for lvl in (getattr(normalized, "bids", None) or [])}
        self.asks = {lvl.price: lvl.size for lvl in (getattr(normalized, "asks", None) or [])}
        self.best_bid = getattr(normalized, "best_bid", None)
        self.best_ask = getattr(normalized, "best_ask", None)
        self.spread = getattr(normalized, "spread", None)
        self.resolved = resolved
        self._stale = bool(getattr(normalized, "stale", False)
                           or getattr(normalized, "needs_snapshot", False)
                           or getattr(normalized, "gap_detected", False)
                           or not getattr(normalized, "valid", True))

    def is_stale(self, _ms) -> bool:
        return self._stale


class PaperBroker:
    def __init__(self, *, fee_model: Optional[FeeModel] = None,
                 slippage_model: Optional[SlippageModel] = None,
                 max_fill_depth_fraction: Optional[Decimal] = None,
                 latency_ms: Optional[int] = None,
                 allow_reference: Optional[bool] = None,
                 allow_pm_reference: Optional[bool] = None,
                 resting_fill_on_cross: Optional[bool] = None,
                 reject_on_stale: Optional[bool] = None,
                 stale_ms: Optional[int] = None):
        self.fees = fee_model or FeeModel()
        self.slippage = slippage_model or SlippageModel()
        self.depth_fraction = (max_fill_depth_fraction if max_fill_depth_fraction is not None
                               else _env_dec("PAPER_MAX_FILL_DEPTH_FRACTION", "0.35"))
        self.latency_ms = latency_ms if latency_ms is not None else _env_int("PAPER_LATENCY_MS", "250")
        self.allow_reference = (allow_reference if allow_reference is not None
                                else _env_flag("PAPER_ALLOW_REFERENCE_PRICE_FILLS", "1"))
        self.allow_pm_reference = (allow_pm_reference if allow_pm_reference is not None
                                   else _env_flag("PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS", "0"))
        # Phase 6: Kalshi reference-price fills are OFF by default (CLOB-only).
        self.allow_kalshi_reference = _env_flag("PAPER_ALLOW_KALSHI_REFERENCE_PRICE_FILLS", "0")
        self.resting_fill_on_cross = (resting_fill_on_cross if resting_fill_on_cross is not None
                                      else _env_flag("PAPER_RESTING_ORDER_FILL_ON_CROSS", "1"))
        self.reject_on_stale = (reject_on_stale if reject_on_stale is not None
                                else _env_flag("PAPER_REJECT_ON_STALE_BOOK", "1"))
        self.stale_ms = stale_ms if stale_ms is not None else _env_int("POLYMARKET_CLOB_STALE_MS", "3000")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _reject(reason: str) -> ExecutionResult:
        return ExecutionResult(status=OrderStatus.REJECTED, fills=[], reject_reason=reason)

    def _mk_fill(self, order: OrderRequest, price: Decimal, qty: Decimal,
                 liquidity: str, ts_ms: int) -> Fill:
        notional = price * qty
        return Fill(client_order_id=order.client_order_id, venue=order.venue,
                    market_id=order.market_id, asset_id=order.asset_id, side=order.side,
                    price=price, quantity=qty, liquidity_flag=liquidity,
                    fee=self.fees.fee(notional, liquidity), ts_ms=ts_ms)

    # ------------------------------------------------------------------ #
    def execute(self, order: OrderRequest, *, book=None, reference_price=None,
                venue_kind: Optional[str] = None, now: Optional[int] = None) -> ExecutionResult:
        ts = now or (now_ms() + self.latency_ms)
        venue_kind = venue_kind or order.venue_kind or "legacy"

        # ---- structural validation ----
        if order.quantity is None or D(order.quantity) <= 0:
            return self._reject(OrderRejectReason.INVALID_QUANTITY)
        needs_price = order.order_type in (OrderType.LIMIT, OrderType.MARKETABLE_LIMIT)
        if needs_price and (order.limit_price is None or D(order.limit_price) <= 0):
            return self._reject(OrderRejectReason.INVALID_PRICE)
        if order.side not in OrderSide.ALL:
            return self._reject(OrderRejectReason.UNKNOWN)

        # ---- CLOB-backed path ----
        if book is not None and (getattr(book, "bids", None) or getattr(book, "asks", None)):
            if self.reject_on_stale and book.is_stale(self.stale_ms):
                return self._reject(OrderRejectReason.STALE_MARKET_DATA)
            if getattr(book, "resolved", False):
                return self._reject(OrderRejectReason.MARKET_RESOLVED)
            return self._execute_clob(order, book, ts)

        # ---- reference-price fallback ----
        ref = D(reference_price) if reference_price is not None else None
        if ref is not None and ref > 0:
            if venue_kind == "pm" and not self.allow_pm_reference:
                return self._reject(OrderRejectReason.MISSING_ORDERBOOK)
            # Kalshi is binary CLOB-only: reference fills disabled by default.
            if venue_kind == "kalshi" and not self.allow_kalshi_reference:
                return self._reject(OrderRejectReason.MISSING_ORDERBOOK)
            if venue_kind not in ("pm", "kalshi") and not self.allow_reference:
                return self._reject(OrderRejectReason.MODE_NOT_ALLOWED)
            return self._execute_reference(order, ref, ts)

        # ---- nothing to fill against ----
        return self._reject(OrderRejectReason.MISSING_BBO if venue_kind in ("pm", "kalshi")
                            else OrderRejectReason.MISSING_ORDERBOOK)

    # ------------------------------------------------------------------ #
    def _execute_clob(self, order: OrderRequest, book, ts: int) -> ExecutionResult:
        side = order.side
        limit = D(order.limit_price)
        spread = book.spread  # Decimal | None
        if side == OrderSide.BUY:
            best = book.best_ask
            levels = sorted(book.asks.items())                 # ascending price
            marketable = best is not None and limit >= best
            within = lambda p: p <= limit  # noqa: E731
        else:
            best = book.best_bid
            levels = sorted(book.bids.items(), reverse=True)    # descending price
            marketable = best is not None and limit <= best
            within = lambda p: p >= limit  # noqa: E731

        if not marketable:
            return self._non_marketable(order)

        remaining = D(order.quantity)
        fills: list[Fill] = []
        for price, size in levels:
            if not within(price):
                break
            avail = D(size) * self.depth_fraction          # queue-position haircut
            take = min(remaining, avail)
            if take <= 0:
                continue
            fill_price = self.slippage.adjust(price, side, spread=spread)
            fills.append(self._mk_fill(order, fill_price, take, LiquidityFlag.TAKER, ts))
            remaining -= take
            if remaining <= 0:
                break

        filled = D(order.quantity) - remaining

        # FOK must fully fill or do nothing.
        if order.time_in_force == TimeInForce.FOK and remaining > 0:
            return self._reject(OrderRejectReason.INSUFFICIENT_DEPTH)

        if filled <= 0:
            return self._non_marketable(order)

        if remaining <= 0:
            return ExecutionResult(status=OrderStatus.FILLED, fills=fills, remaining=Decimal(0))

        # partial fill
        rests = order.time_in_force in (TimeInForce.GTC, TimeInForce.DAY)
        return ExecutionResult(status=OrderStatus.PARTIALLY_FILLED, fills=fills,
                               resting=rests, remaining=remaining)

    def _non_marketable(self, order: OrderRequest) -> ExecutionResult:
        """No immediate fill: GTC/DAY rest OPEN; IOC cancels; FOK rejects."""
        if order.time_in_force in (TimeInForce.GTC, TimeInForce.DAY):
            return ExecutionResult(status=OrderStatus.OPEN, fills=[], resting=True,
                                   remaining=D(order.quantity))
        if order.time_in_force == TimeInForce.FOK:
            return self._reject(OrderRejectReason.INSUFFICIENT_DEPTH)
        return ExecutionResult(status=OrderStatus.CANCELLED, fills=[], remaining=D(order.quantity))

    def _execute_reference(self, order: OrderRequest, ref: Decimal, ts: int) -> ExecutionResult:
        """Conservative full fill at a reference price. Flagged SIMULATED."""
        fill_price = self.slippage.adjust(ref, order.side, spread=None)
        fills = [self._mk_fill(order, fill_price, D(order.quantity), LiquidityFlag.SIMULATED, ts)]
        return ExecutionResult(status=OrderStatus.FILLED, fills=fills, remaining=Decimal(0))

    # ------------------------------------------------------------------ #
    def check_resting(self, order: OrderRequest, book) -> ExecutionResult:
        """Re-evaluate a resting OPEN order against a fresh book (cross fill)."""
        if not self.resting_fill_on_cross:
            return ExecutionResult(status=OrderStatus.OPEN, fills=[], resting=True,
                                   remaining=D(order.quantity))
        if book is None or not (getattr(book, "bids", None) or getattr(book, "asks", None)):
            return ExecutionResult(status=OrderStatus.OPEN, fills=[], resting=True,
                                   remaining=D(order.quantity))
        if self.reject_on_stale and book.is_stale(self.stale_ms):
            return ExecutionResult(status=OrderStatus.OPEN, fills=[], resting=True,
                                   remaining=D(order.quantity))
        # treat the remaining quantity as a fresh marketable attempt
        res = self._execute_clob(order, book, now_ms() + self.latency_ms)
        if res.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            # still not crossable -> keep resting
            return ExecutionResult(status=OrderStatus.OPEN, fills=[], resting=True,
                                   remaining=D(order.quantity))
        return res

    def config(self) -> dict:
        return {
            "max_fill_depth_fraction": str(self.depth_fraction),
            "latency_ms": self.latency_ms,
            "allow_reference_price_fills": self.allow_reference,
            "allow_pm_reference_price_fills": self.allow_pm_reference,
            "resting_order_fill_on_cross": self.resting_fill_on_cross,
            "reject_on_stale_book": self.reject_on_stale,
            "stale_ms": self.stale_ms,
            "fees": self.fees.as_dict(), "slippage": self.slippage.as_dict(),
        }
