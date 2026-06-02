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

import hashlib
import os
from decimal import Decimal
from typing import Optional

from .fees import FeeModel
from .slippage import SlippageModel, markout_bps
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


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class RealisticFillModel:
    """Probabilistic CLOB v2 fill model (PAPER ONLY).

    Estimates the probability a marketable paper order fills and the fraction
    that fills, from spread, top-of-book depth, order size, book age, mid
    volatility, a queue-position proxy, and price aggressiveness (how deep the
    limit crosses). Deterministic: the per-order roll is a hash of the client
    order id, so a given order always resolves the same way. Analytics only —
    never places, sizes, or submits an order."""

    def __init__(self, *, max_spread: float = 0.08,
                 max_depth_fraction: Decimal = Decimal("0.35"),
                 stale_ms: int = 3000, seed_salt: str = ""):
        self.max_spread = float(max_spread)
        self.max_depth_fraction = Decimal(str(max_depth_fraction))
        self.stale_ms = int(stale_ms)
        self.seed_salt = str(seed_salt)

    def fill_probability(self, *, spread: float, depth_usd: float, order_usd: float,
                         book_age_ms: float = 0.0, volatility: float = 0.0,
                         queue_proxy: float = 0.0, aggressiveness: float = 1.0,
                         stale: bool = False, max_spread: Optional[float] = None) -> float:
        if stale or depth_usd <= 0 or order_usd <= 0:
            return 0.0
        ms = float(max_spread if max_spread is not None else self.max_spread)
        spread_term = _clamp01(1.0 - max(0.0, float(spread)) / max(1e-9, ms))
        depth_term = _clamp01(float(depth_usd) / (float(depth_usd) + float(order_usd)))
        age_term = _clamp01(1.0 - max(0.0, float(book_age_ms)) / max(1e-9, float(self.stale_ms)))
        vol_term = _clamp01(1.0 - 2.0 * max(0.0, float(volatility)))
        queue_term = _clamp01(1.0 - max(0.0, float(queue_proxy)))
        aggr_term = _clamp01(0.5 + 0.25 * max(0.0, min(2.0, float(aggressiveness))))
        return round(_clamp01(spread_term * depth_term * age_term
                              * vol_term * queue_term * aggr_term), 6)

    def fill_fraction(self, *, order_usd: float, depth_usd: float,
                      queue_proxy: float = 0.0,
                      max_depth_fraction: Optional[Decimal] = None) -> float:
        frac = float(max_depth_fraction if max_depth_fraction is not None
                     else self.max_depth_fraction)
        fillable = max(0.0, float(depth_usd)) * frac * (1.0 - _clamp01(queue_proxy))
        order = max(1e-9, float(order_usd))
        if order <= fillable:
            return 1.0
        return round(_clamp01(fillable / order), 6)

    def roll(self, seed: str) -> float:
        """Deterministic pseudo-random draw in [0,1) from a seed (client order id)."""
        h = hashlib.sha256((str(seed) + "|" + self.seed_salt).encode("utf-8")).digest()
        return int.from_bytes(h[:8], "big") / float(1 << 64)


class PaperBroker:
    def __init__(self, *, fee_model: Optional[FeeModel] = None,
                 slippage_model: Optional[SlippageModel] = None,
                 max_fill_depth_fraction: Optional[Decimal] = None,
                 latency_ms: Optional[int] = None,
                 allow_reference: Optional[bool] = None,
                 allow_pm_reference: Optional[bool] = None,
                 resting_fill_on_cross: Optional[bool] = None,
                 reject_on_stale: Optional[bool] = None,
                 stale_ms: Optional[int] = None,
                 realistic: Optional[bool] = None,
                 fill_model: Optional[RealisticFillModel] = None):
        self.fees = fee_model or FeeModel()
        self.slippage = slippage_model or SlippageModel()
        # CLOB v2 realistic-fill mode (PAPER ONLY; opt-in). When ON, marketable
        # orders fill PROBABILISTICALLY (not guaranteed) with partial fills,
        # size/volatility-aware slippage, and an adverse-selection markout, so
        # aggressive paper feedback is realistic. Default OFF -> deterministic
        # depth-limited fills (unchanged behaviour for existing callers/tests).
        self.realistic = (realistic if realistic is not None
                          else _env_flag("PAPER_REALISTIC_FILLS", "0"))
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
        self.fill_model = fill_model or RealisticFillModel(
            max_depth_fraction=self.depth_fraction, stale_ms=self.stale_ms)

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
            if self.realistic:
                return self._execute_clob_realistic(order, book, ts)
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

    def _execute_clob_realistic(self, order: OrderRequest, book, ts: int) -> ExecutionResult:
        """Probabilistic CLOB v2 fill: NOT guaranteed. Models fill probability,
        partial fills, size/volatility-aware slippage, and an adverse-selection
        markout. Deterministic per client_order_id."""
        side = order.side
        limit = D(order.limit_price)
        spread = book.spread
        if side == OrderSide.BUY:
            best = book.best_ask
            levels = sorted(book.asks.items())
            marketable = best is not None and limit >= best
            within = lambda p: p <= limit  # noqa: E731
        else:
            best = book.best_bid
            levels = sorted(book.bids.items(), reverse=True)
            marketable = best is not None and limit <= best
            within = lambda p: p >= limit  # noqa: E731
        if not marketable:
            return self._non_marketable(order)

        qty = D(order.quantity)
        best_f = float(best)
        order_usd = float(limit) * float(qty)
        # executable top-of-book depth ($) within the limit
        depth_usd = 0.0
        for price, size in levels:
            if not within(price):
                break
            depth_usd += float(size) * float(price)
        spread_f = float(spread) if spread is not None else 0.0
        # price aggressiveness: how far the limit crosses, in spread units
        crossing = (float(limit) - best_f) if side == OrderSide.BUY else (best_f - float(limit))
        aggressiveness = 1.0 + max(0.0, crossing) / max(1e-9, spread_f or 1e-9)
        book_age = 0.0
        try:
            age = book.age_ms() if callable(getattr(book, "age_ms", None)) else None
            book_age = float(age) if age is not None else 0.0
        except Exception:  # noqa: BLE001
            book_age = 0.0
        volatility = 0.0
        try:
            rv = book.recent_volatility() if callable(getattr(book, "recent_volatility", None)) else None
            volatility = float(rv) if rv is not None else 0.0
        except Exception:  # noqa: BLE001
            volatility = 0.0

        p_fill = self.fill_model.fill_probability(
            spread=spread_f, depth_usd=depth_usd, order_usd=order_usd,
            book_age_ms=book_age, volatility=volatility, queue_proxy=0.0,
            aggressiveness=aggressiveness)
        fraction = self.fill_model.fill_fraction(order_usd=order_usd, depth_usd=depth_usd,
                                                 queue_proxy=0.0)
        roll = self.fill_model.roll(order.client_order_id)

        def _diag(res: ExecutionResult) -> ExecutionResult:
            res.realistic = True
            res.fill_probability = p_fill
            res.fill_fraction = fraction
            res.queue_position = 0.0
            avg = res.avg_fill_price
            mid = (float(book.best_bid) + float(book.best_ask)) / 2.0 \
                if (book.best_bid is not None and book.best_ask is not None) else best_f
            if avg is not None:
                res.adverse_selection_bps = markout_bps(avg, D(str(mid)), side)
            res.partial_fill = res.status == OrderStatus.PARTIALLY_FILLED
            return res

        # did not win the fill this draw -> no guaranteed fill
        if roll >= p_fill or p_fill <= 0.0:
            nm = self._non_marketable(order)
            return _diag(nm)

        fill_cap = qty * Decimal(str(fraction))
        fills: list[Fill] = []
        taken = Decimal(0)
        for price, size in levels:
            if not within(price):
                break
            avail = D(size) * self.depth_fraction
            take = min(fill_cap - taken, avail)
            if take <= 0:
                continue
            fill_price = self.slippage.impact_adjust(
                price, side, spread=spread, order_usd=order_usd, depth_usd=depth_usd,
                volatility=volatility)
            fills.append(self._mk_fill(order, fill_price, take, LiquidityFlag.TAKER, ts))
            taken += take
            if taken >= fill_cap:
                break

        filled = taken
        remaining = qty - filled
        if order.time_in_force == TimeInForce.FOK and remaining > 0:
            return self._reject(OrderRejectReason.INSUFFICIENT_DEPTH)
        if filled <= 0:
            return _diag(self._non_marketable(order))
        if remaining <= 0:
            return _diag(ExecutionResult(status=OrderStatus.FILLED, fills=fills,
                                         remaining=Decimal(0)))
        rests = order.time_in_force in (TimeInForce.GTC, TimeInForce.DAY)
        return _diag(ExecutionResult(status=OrderStatus.PARTIALLY_FILLED, fills=fills,
                                     resting=rests, remaining=remaining))

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
            "realistic_fills": self.realistic,
            "fees": self.fees.as_dict(), "slippage": self.slippage.as_dict(),
        }
