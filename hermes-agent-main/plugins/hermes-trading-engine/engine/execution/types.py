"""Execution-layer types for the OMS + PaperBroker (Phase 3).

Decimal is used for every price / quantity / notional / fee. These are plain
dataclasses (not Pydantic) so we have full control over Decimal -> JSON-safe
serialization (every value goes out as a string). NOTHING here can place a real
order — these are paper-simulation data structures only.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


def D(x) -> Decimal:
    """Coerce to Decimal; non-numeric -> Decimal(0)."""
    if isinstance(x, Decimal):
        return x
    if x is None or x == "":
        return Decimal(0)
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _s(x) -> Optional[str]:
    return None if x is None else str(x)


def new_client_order_id(seed: Optional[str] = None) -> str:
    """Idempotent id when a deterministic seed is given, else random."""
    if seed:
        import hashlib
        return "co-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return "co-" + uuid.uuid4().hex[:20]


# --------------------------------------------------------------------------- #
# String enums (kept as constants for cheap JSON + sqlite storage)
# --------------------------------------------------------------------------- #
class OrderSide:
    BUY = "BUY"
    SELL = "SELL"
    ALL = frozenset({"BUY", "SELL"})


class OrderType:
    LIMIT = "LIMIT"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    MARKET_SIMULATED = "MARKET_SIMULATED"
    ALL = frozenset({"LIMIT", "MARKETABLE_LIMIT", "MARKET_SIMULATED"})


class TimeInForce:
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    DAY = "DAY"
    ALL = frozenset({"GTC", "IOC", "FOK", "DAY"})


class OrderStatus:
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    ACCEPTED = "ACCEPTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class LiquidityFlag:
    MAKER = "MAKER"
    TAKER = "TAKER"
    SIMULATED = "SIMULATED"


class OrderRejectReason:
    RISK_REJECTED = "risk_rejected"
    STALE_MARKET_DATA = "stale_market_data"
    MISSING_ORDERBOOK = "missing_orderbook"
    MISSING_BBO = "missing_bbo"
    INVALID_TICK_SIZE = "invalid_tick_size"
    EXCESSIVE_SPREAD = "excessive_spread"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
    MARKET_RESOLVED = "market_resolved"
    MODE_NOT_ALLOWED = "mode_not_allowed"
    BROKER_UNAVAILABLE = "broker_unavailable"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class OrderRequest:
    client_order_id: str = ""
    venue: str = ""
    market_id: str = ""
    asset_id: Optional[str] = None
    outcome: Optional[str] = None
    side: str = OrderSide.BUY
    order_type: str = OrderType.MARKETABLE_LIMIT
    limit_price: Optional[Decimal] = None
    quantity: Decimal = Decimal(0)
    notional: Optional[Decimal] = None
    time_in_force: str = TimeInForce.IOC
    created_ts_ms: int = field(default_factory=now_ms)
    source: str = ""
    proposal_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    # venue realism class: "pm" (CLOB-only by default) or "legacy" (reference ok)
    venue_kind: str = "legacy"
    parent_client_order_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.client_order_id:
            self.client_order_id = new_client_order_id()
        self.quantity = D(self.quantity)
        if self.limit_price is not None:
            self.limit_price = D(self.limit_price)
        if self.notional is None and self.limit_price is not None:
            self.notional = self.limit_price * self.quantity
        elif self.notional is not None:
            self.notional = D(self.notional)

    def record(self) -> dict:
        return {
            "client_order_id": self.client_order_id, "venue": self.venue,
            "market_id": self.market_id, "asset_id": self.asset_id,
            "outcome": self.outcome, "side": self.side, "order_type": self.order_type,
            "limit_price": _s(self.limit_price), "quantity": _s(self.quantity),
            "notional": _s(self.notional), "time_in_force": self.time_in_force,
            "created_ts_ms": self.created_ts_ms, "source": self.source,
            "proposal_id": self.proposal_id, "risk_decision_id": self.risk_decision_id,
            "venue_kind": self.venue_kind, "parent_client_order_id": self.parent_client_order_id,
        }


@dataclass
class Fill:
    client_order_id: str
    venue: str
    market_id: str
    side: str
    price: Decimal
    quantity: Decimal
    liquidity_flag: str = LiquidityFlag.SIMULATED
    fee: Decimal = Decimal(0)
    asset_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    fill_id: str = ""
    ts_ms: int = field(default_factory=now_ms)

    def __post_init__(self) -> None:
        self.price = D(self.price)
        self.quantity = D(self.quantity)
        self.fee = D(self.fee)
        if not self.fill_id:
            self.fill_id = "fl-" + uuid.uuid4().hex[:20]

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    def record(self) -> dict:
        return {
            "fill_id": self.fill_id, "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id, "venue": self.venue,
            "market_id": self.market_id, "asset_id": self.asset_id, "side": self.side,
            "price": _s(self.price), "quantity": _s(self.quantity),
            "notional": _s(self.notional), "fee": _s(self.fee),
            "liquidity_flag": self.liquidity_flag, "ts_ms": self.ts_ms,
        }


@dataclass
class OrderAck:
    client_order_id: str
    accepted: bool
    status: str
    reason: Optional[str] = None
    broker_order_id: Optional[str] = None
    ts_ms: int = field(default_factory=now_ms)


@dataclass
class Position:
    venue: str
    market_id: str
    asset_id: Optional[str] = None
    outcome: Optional[str] = None
    quantity: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    updated_ts_ms: int = field(default_factory=now_ms)

    def record(self) -> dict:
        return {
            "venue": self.venue, "market_id": self.market_id, "asset_id": self.asset_id,
            "outcome": self.outcome, "quantity": _s(self.quantity),
            "avg_price": _s(self.avg_price), "realized_pnl": _s(self.realized_pnl),
            "unrealized_pnl": _s(self.unrealized_pnl), "fees_paid": _s(self.fees_paid),
            "updated_ts_ms": self.updated_ts_ms,
        }


@dataclass
class ExecutionResult:
    """What the PaperBroker decided for one order."""

    status: str
    fills: list[Fill] = field(default_factory=list)
    reject_reason: Optional[str] = None
    resting: bool = False
    remaining: Decimal = Decimal(0)
    # ---- CLOB v2 realistic-fill diagnostics (additive; default = neutral) ----
    # Populated only by the realistic fill path; the deterministic path leaves
    # these at their defaults so existing behaviour/serialization is unchanged.
    realistic: bool = False
    fill_probability: Optional[float] = None
    fill_fraction: Optional[float] = None
    partial_fill: bool = False
    queue_position: Optional[float] = None
    adverse_selection_bps: Optional[Decimal] = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), Decimal(0))

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        q = self.filled_quantity
        if q <= 0:
            return None
        return sum((f.price * f.quantity for f in self.fills), Decimal(0)) / q

    @property
    def total_fee(self) -> Decimal:
        return sum((f.fee for f in self.fills), Decimal(0))


@dataclass
class OrderResult:
    """OMS-level outcome returned to callers."""

    order: OrderRequest
    ack: OrderAck
    fills: list[Fill] = field(default_factory=list)
    status: str = OrderStatus.CREATED
    reject_reason: Optional[str] = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), Decimal(0))

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        q = self.filled_quantity
        if q <= 0:
            return None
        return sum((f.price * f.quantity for f in self.fills), Decimal(0)) / q

    @property
    def accepted(self) -> bool:
        return self.ack.accepted

    def to_api(self) -> dict:
        return {
            "order": self.order.record(),
            "status": self.status,
            "reject_reason": self.reject_reason,
            "fills": [f.record() for f in self.fills],
            "filled_quantity": _s(self.filled_quantity),
            "avg_fill_price": _s(self.avg_fill_price),
        }
