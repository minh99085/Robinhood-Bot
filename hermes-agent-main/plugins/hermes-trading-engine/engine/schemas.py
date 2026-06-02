"""Typed Pydantic v2 schemas for the Hermes Trading Engine.

These are the data contracts that the serious prediction-market engine is being
built around: research evidence, probability estimates, trade proposals, the
deterministic risk decision, market metadata, and the order/fill/position
lifecycle types used by paper / shadow / (future) guarded-live execution.

NOTHING here places an order. These are pure data models. The only execution
path in the codebase is PAPER simulation, and every simulated order must pass
the deterministic :class:`~engine.risk.RiskEngine` (which consumes
:class:`TradeProposal` and returns :class:`RiskDecision`).

The `GrokAction` model + `parse_grok_action()` enforce a hard rule: Grok may
research and propose, but invalid / unparseable model output collapses to a
WAIT action — never a best-effort trade. Grok output also never sets order
size (the `suggestedSizePct` field is advisory metadata only).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sides seen across every market the engine touches (crypto/stock = BUY/SELL,
# Polymarket = YES/NO, BTC pulse = UP/DOWN).
Side = Literal["BUY", "SELL", "YES", "NO", "UP", "DOWN"]
MarketKind = Literal["crypto", "stock", "polymarket", "pulse", "arb"]
RunMode = Literal["paper", "shadow", "live"]
GrokVerb = Literal["BUY", "SELL", "HOLD", "WAIT"]

# Per-field clamp bounds for GrokAction numeric fields.
_GROK_FLOAT_BOUNDS = {
    "confidence": (0.0, 1.0),
    "suggestedSizePct": (0.0, 100.0),
    "stopLossPct": (0.0, 100.0),
    "takeProfitPct": (0.0, 1000.0),
}


def _now() -> float:
    return round(time.time(), 3)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Research / probability
# --------------------------------------------------------------------------- #
class EvidenceItem(BaseModel):
    """A single piece of research evidence backing a probability estimate."""

    model_config = ConfigDict(extra="ignore")

    source: str = ""
    summary: str = ""
    url: Optional[str] = None
    stance: Literal["supports", "refutes", "neutral"] = "neutral"
    weight: float = Field(default=0.0, ge=0.0, le=1.0)
    ts: float = Field(default_factory=_now)

    @field_validator("summary")
    @classmethod
    def _truncate_summary(cls, v: str) -> str:
        return (v or "")[:500]


class ProbabilityEstimate(BaseModel):
    """A calibrated probability for a binary market outcome."""

    model_config = ConfigDict(extra="ignore")

    market_id: str = ""
    p: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str = "unknown"
    evidence: list[EvidenceItem] = Field(default_factory=list)
    ts: float = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Market metadata + book
# --------------------------------------------------------------------------- #
class MarketMetadata(BaseModel):
    """Static-ish descriptor for a tradable market."""

    model_config = ConfigDict(extra="ignore")

    market_id: str
    kind: MarketKind
    symbol: str
    venue: str = ""
    question: Optional[str] = None
    tick_size: float = 0.0
    min_size: float = 0.0
    expiry_ts: Optional[float] = None
    resolution_source: Optional[str] = None
    # 0 = perfectly crisp resolution criteria, 1 = highly ambiguous / subjective.
    ambiguity_score: float = Field(default=0.0, ge=0.0, le=1.0)


class BBO(BaseModel):
    """Best bid / offer snapshot for one symbol on one venue."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    venue: str = ""
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts: float = Field(default_factory=_now)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ask or self.bid

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.spread / m) if m > 0 else 0.0


class OrderbookSnapshot(BaseModel):
    """Top-N order book snapshot. Levels are [price, size] pairs."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    venue: str = ""
    bids: list[tuple[float, float]] = Field(default_factory=list)
    asks: list[tuple[float, float]] = Field(default_factory=list)
    ts: float = Field(default_factory=_now)

    def best_bbo(self) -> BBO:
        bid = self.bids[0] if self.bids else (0.0, 0.0)
        ask = self.asks[0] if self.asks else (0.0, 0.0)
        return BBO(symbol=self.symbol, venue=self.venue, bid=bid[0], ask=ask[0],
                   bid_size=bid[1], ask_size=ask[1], ts=self.ts)


class OrderbookDelta(BaseModel):
    """Incremental order book update for one price level."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    venue: str = ""
    side: Literal["bid", "ask"]
    price: float
    size: float  # absolute new size at this level; 0 removes the level
    ts: float = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Order / fill / position lifecycle (PAPER today; same contract for shadow/live)
# --------------------------------------------------------------------------- #
class OrderRequest(BaseModel):
    """An intent to place an order. Building this NEVER sends anything."""

    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(default_factory=lambda: _new_id("ord"))
    market_id: str = ""
    venue: str = ""
    symbol: str
    side: Side
    order_type: Literal["market", "limit"] = "market"
    qty: float = Field(default=0.0, ge=0.0)
    limit_price: Optional[float] = None
    notional: Optional[float] = None
    mode: RunMode = "paper"
    reduce_only: bool = False
    rationale: str = ""
    ts: float = Field(default_factory=_now)


class OrderAck(BaseModel):
    """Acknowledgement for an order request (paper fills ack instantly)."""

    model_config = ConfigDict(extra="ignore")

    client_id: str
    order_id: Optional[str] = None
    accepted: bool = False
    reason: Optional[str] = None
    mode: RunMode = "paper"
    ts: float = Field(default_factory=_now)


class Fill(BaseModel):
    """A (partial) fill against an order."""

    model_config = ConfigDict(extra="ignore")

    order_id: str = ""
    client_id: str = ""
    symbol: str = ""
    venue: str = ""
    side: Side = "BUY"
    price: float = 0.0
    qty: float = 0.0
    fee: float = 0.0
    liquidity: Literal["maker", "taker", "unknown"] = "unknown"
    ts: float = Field(default_factory=_now)


class Position(BaseModel):
    """Net position in one market."""

    model_config = ConfigDict(extra="ignore")

    market_id: str = ""
    symbol: str = ""
    venue: str = ""
    side: Side = "BUY"
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    opened_ts: float = Field(default_factory=_now)
    mode: RunMode = "paper"


# --------------------------------------------------------------------------- #
# Risk contract
# --------------------------------------------------------------------------- #
class TradeProposal(BaseModel):
    """A proposed (simulated) trade, submitted to the RiskEngine for a verdict.

    No simulated order may be opened without an approved RiskDecision for the
    corresponding proposal. `edge_after_costs`, `spread`, `data_age_s` and
    `ambiguity_score` are the deterministic inputs the RiskEngine gates on.
    """

    model_config = ConfigDict(extra="ignore")

    proposal_id: str = Field(default_factory=lambda: _new_id("prop"))
    strategy: str = ""
    market: MarketKind = "crypto"
    symbol: str = ""
    side: Side = "BUY"
    notional: float = Field(default=0.0, ge=0.0)
    price: Optional[float] = None
    edge_after_costs: float = 0.0
    spread: float = Field(default=0.0, ge=0.0)
    data_age_s: float = Field(default=0.0, ge=0.0)
    ambiguity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    allow_duplicate: bool = False
    mode: RunMode = "paper"
    rationale: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=_now)


class RiskDecision(BaseModel):
    """The deterministic verdict for a TradeProposal."""

    model_config = ConfigDict(extra="ignore")

    proposal_id: str = ""
    approved: bool = False
    code: str = "OK"
    reasons: list[str] = Field(default_factory=list)
    adjusted_notional: Optional[float] = None
    limits_snapshot: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=_now)

    def as_record(self) -> dict[str, Any]:
        """Compact dict for logging / dashboard (no secrets, ever)."""
        return {
            "ts": self.ts,
            "proposal_id": self.proposal_id,
            "approved": self.approved,
            "code": self.code,
            "reasons": self.reasons,
            "adjusted_notional": self.adjusted_notional,
        }


# --------------------------------------------------------------------------- #
# Grok action (research/proposal only — never executes, never sets size)
# --------------------------------------------------------------------------- #
class GrokAction(BaseModel):
    """Validated Grok ActionSchema output.

    Grok is a research/advisory layer. `suggestedSizePct` is ADVISORY metadata
    only and must never be used to size an order. Any field that fails to parse
    is coerced to a safe default; a wholly invalid payload yields a WAIT action
    via :meth:`safe_parse`.
    """

    model_config = ConfigDict(extra="ignore")

    action: GrokVerb = "WAIT"
    confidence: float = 0.0
    reasoning: str = ""
    suggestedSizePct: float = 0.0  # advisory ONLY — never sizes an order
    stopLossPct: float = 0.0
    takeProfitPct: float = 0.0
    urgency: Literal["low", "medium", "high"] = "low"
    vetoReason: Optional[str] = None

    @field_validator("action", mode="before")
    @classmethod
    def _norm_action(cls, v: Any) -> str:
        s = str(v).strip().upper()
        return s if s in ("BUY", "SELL", "HOLD", "WAIT") else "WAIT"

    @field_validator("confidence", "suggestedSizePct", "stopLossPct", "takeProfitPct", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any, info) -> float:
        # Clamp (not reject) out-of-range / unparseable values so a stray field
        # never discards an otherwise-valid action.
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        lo, hi = _GROK_FLOAT_BOUNDS.get(info.field_name, (0.0, 1e18))
        return min(hi, max(lo, x))

    @field_validator("urgency", mode="before")
    @classmethod
    def _norm_urgency(cls, v: Any) -> str:
        s = str(v).strip().lower()
        return s if s in ("low", "medium", "high") else "low"

    @field_validator("reasoning", mode="before")
    @classmethod
    def _norm_reasoning(cls, v: Any) -> str:
        return str(v or "")[:120]

    @field_validator("vetoReason", mode="before")
    @classmethod
    def _norm_veto(cls, v: Any) -> Optional[str]:
        if v in (None, ""):
            return None
        return str(v)[:120]

    @classmethod
    def safe_parse(cls, raw: Any) -> "GrokAction":
        """Parse arbitrary Grok output into a GrokAction, defaulting to WAIT.

        Anything that is not a dict, or that fails validation outright, becomes
        a zero-confidence WAIT. This is the single enforcement point for
        "invalid Grok output must become WAIT, not a best-effort trade".
        """
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return cls()
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:  # noqa: BLE001 — any validation failure → safe WAIT
            return cls()

    def effective_verb(self, min_confidence: float = 0.4) -> GrokVerb:
        """BUY/SELL only survive above the confidence floor; else WAIT."""
        if self.action in ("BUY", "SELL") and self.confidence < min_confidence:
            return "WAIT"
        return self.action


def parse_grok_action(raw: Any, min_confidence: float = 0.4) -> GrokAction:
    """Module-level helper: validate raw Grok output, enforce the WAIT floor."""
    act = GrokAction.safe_parse(raw)
    verb = act.effective_verb(min_confidence)
    if verb != act.action:
        # rebuild with the demoted verb so callers see a consistent object
        return act.model_copy(update={"action": verb})
    return act


# --------------------------------------------------------------------------- #
# Phase 2: market-data layer (read-only Polymarket CLOB)
# --------------------------------------------------------------------------- #
# Connection lifecycle for a market-data source.
MarketConnStatus = Literal[
    "disconnected", "connecting", "connected", "reconnecting", "degraded"
]


class OrderbookLevel(BaseModel):
    """One price level. Prices/sizes are decimal strings to preserve precision."""

    model_config = ConfigDict(extra="ignore")

    price: str
    size: str


class OrderbookStateSnapshot(BaseModel):
    """Normalized point-in-time order book for one CLOB asset (token)."""

    model_config = ConfigDict(extra="ignore")

    asset_id: str
    market_id: str = ""
    venue: str = "polymarket"
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)
    best_bid: Optional[str] = None
    best_ask: Optional[str] = None
    spread: Optional[str] = None
    midpoint: Optional[str] = None
    tick_size: Optional[str] = None
    last_update_ms: int = 0
    sequence: Optional[str] = None
    resolved: bool = False
    tick_size_dirty: bool = False


class RawMarketEvent(BaseModel):
    """An untransformed inbound market-data message (audit trail)."""

    model_config = ConfigDict(extra="ignore")

    source: str = "polymarket_clob"
    event_type: str = ""
    market_id: Optional[str] = None
    asset_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts_ms: int = 0


class TradePrint(BaseModel):
    """A last-trade print for an asset."""

    model_config = ConfigDict(extra="ignore")

    asset_id: str = ""
    market_id: str = ""
    price: str = "0"
    size: str = "0"
    side: Optional[str] = None
    ts_ms: int = 0


class MarketResolvedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_id: str = ""
    asset_id: Optional[str] = None
    ts_ms: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class TickSizeChangeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str = ""
    market_id: str = ""
    old_tick_size: Optional[str] = None
    new_tick_size: Optional[str] = None
    ts_ms: int = 0


class MarketDataStatus(BaseModel):
    """Connection + counters for one market-data source."""

    model_config = ConfigDict(extra="ignore")

    source: str = "polymarket_clob"
    status: MarketConnStatus = "disconnected"
    url: Optional[str] = None
    last_message_ms: int = 0
    last_message_age_ms: Optional[int] = None
    messages_received: int = 0
    parse_errors: int = 0
    reconnect_count: int = 0
    subscribed_asset_count: int = 0
    stale_asset_count: int = 0


class MarketDataHealth(BaseModel):
    """Dashboard-facing health bundle for a market-data source."""

    model_config = ConfigDict(extra="ignore")

    status: MarketDataStatus = Field(default_factory=MarketDataStatus)
    enabled: bool = False
    assets: list[dict[str, Any]] = Field(default_factory=list)


class DataFreshnessDecision(BaseModel):
    """Verdict on whether an asset's market data is reliable enough to trade."""

    model_config = ConfigDict(extra="ignore")

    asset_id: str = ""
    fresh: bool = True
    code: str = "OK"
    reasons: list[str] = Field(default_factory=list)
    ts_ms: int = 0
