"""Venue-neutral prediction-market schemas (Phase 6).

Common metadata, lifecycle, resolution, and normalized-orderbook models shared by
Polymarket and Kalshi so Research, RiskEngine, OMS, PaperBroker, and Replay can
consume one shape. All read-only. Secrets are never represented here.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VenueName = Literal["polymarket", "kalshi"]
Outcome = Literal["YES", "NO", "yes", "no"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def payload_hash(obj: Any) -> str:
    try:
        blob = json.dumps(obj, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = str(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


class MarketRef(BaseModel):
    """A venue-neutral reference to a market (or one outcome of it)."""

    model_config = ConfigDict(extra="ignore")

    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    event_ticker: Optional[str] = None
    series_ticker: Optional[str] = None
    outcome: Optional[str] = None

    def key(self) -> str:
        return "|".join([self.venue, self.market_ticker or "", self.market_id or "",
                         self.asset_id or "", (self.outcome or "").upper()])

    @classmethod
    def encode(cls, venue: str, ident: str, outcome: Optional[str] = None) -> str:
        """Compact, URL-safe ref string for API paths: venue:ident[:outcome]."""
        out = f"{venue}:{ident}"
        return f"{out}:{outcome}" if outcome else out

    @classmethod
    def parse(cls, venue: str, ref: str) -> "MarketRef":
        """Parse an API path ref. Kalshi uses tickers; Polymarket uses ids/asset ids."""
        parts = ref.split(":")
        ident = parts[0]
        outcome = parts[1] if len(parts) > 1 else None
        if venue == "kalshi":
            return cls(venue="kalshi", market_ticker=ident, outcome=outcome)
        return cls(venue="polymarket", market_id=ident, asset_id=ident, outcome=outcome)


class MarketFilter(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: Optional[VenueName] = None
    status: Optional[str] = None
    series_ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    limit: int = 100


class SettlementSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    url: Optional[str] = None
    source_type: Optional[str] = None
    credibility_score: Optional[float] = None


class VenueMarketMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    event_ticker: Optional[str] = None
    series_ticker: Optional[str] = None
    question: str = ""
    title: Optional[str] = None
    yes_title: Optional[str] = None
    no_title: Optional[str] = None
    outcomes: list[str] = Field(default_factory=lambda: ["YES", "NO"])
    category: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    status: str = "unknown"
    open_ts_ms: Optional[int] = None
    close_ts_ms: Optional[int] = None
    latest_expiration_ts_ms: Optional[int] = None
    settlement_timer_seconds: Optional[int] = None
    can_close_early: Optional[bool] = None
    fractional_trading_enabled: Optional[bool] = None
    volume: Optional[Decimal] = None
    volume_24h: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    price_level_structure: Optional[str] = None
    min_tick_size: Optional[Decimal] = None
    fee_metadata: dict = Field(default_factory=dict)
    settlement_sources: list[SettlementSource] = Field(default_factory=list)
    contract_url: Optional[str] = None
    contract_terms_url: Optional[str] = None
    raw_payload_hash: Optional[str] = None
    updated_ts_ms: int = Field(default_factory=_now_ms)

    @field_validator("volume", "volume_24h", "open_interest", "last_price", "yes_bid",
                     "yes_ask", "no_bid", "no_ask", "min_tick_size", mode="before")
    @classmethod
    def _coerce_dec(cls, v):
        return _dec(v)

    def is_tradable(self) -> bool:
        return str(self.status).lower() in ("open", "active", "trading")

    def record(self) -> dict:
        def s(x):
            return None if x is None else str(x)
        return {
            "venue": self.venue, "market_id": self.market_id, "market_ticker": self.market_ticker,
            "asset_id": self.asset_id, "event_ticker": self.event_ticker,
            "series_ticker": self.series_ticker, "question": self.question, "title": self.title,
            "yes_title": self.yes_title, "no_title": self.no_title,
            "outcomes_json": json.dumps(self.outcomes), "category": self.category,
            "tags_json": json.dumps(self.tags), "status": self.status,
            "open_ts_ms": self.open_ts_ms, "close_ts_ms": self.close_ts_ms,
            "latest_expiration_ts_ms": self.latest_expiration_ts_ms,
            "settlement_timer_seconds": self.settlement_timer_seconds,
            "can_close_early": None if self.can_close_early is None else int(self.can_close_early),
            "fractional_trading_enabled": None if self.fractional_trading_enabled is None
            else int(self.fractional_trading_enabled),
            "volume": s(self.volume), "volume_24h": s(self.volume_24h),
            "open_interest": s(self.open_interest), "last_price": s(self.last_price),
            "yes_bid": s(self.yes_bid), "yes_ask": s(self.yes_ask), "no_bid": s(self.no_bid),
            "no_ask": s(self.no_ask), "price_level_structure": self.price_level_structure,
            "min_tick_size": s(self.min_tick_size),
            "fee_metadata_json": json.dumps(self.fee_metadata, default=str),
            "settlement_sources_json": json.dumps([s_.model_dump() for s_ in self.settlement_sources]),
            "contract_url": self.contract_url, "contract_terms_url": self.contract_terms_url,
            "raw_payload_hash": self.raw_payload_hash, "updated_ts_ms": self.updated_ts_ms,
        }


class VenueSeriesMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    series_ticker: str
    title: str = ""
    category: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    frequency: Optional[str] = None
    settlement_sources: list[SettlementSource] = Field(default_factory=list)
    contract_url: Optional[str] = None
    contract_terms_url: Optional[str] = None
    fee_multiplier: Optional[Decimal] = None
    additional_prohibitions: list[str] = Field(default_factory=list)
    raw_payload_hash: Optional[str] = None
    updated_ts_ms: int = Field(default_factory=_now_ms)

    @field_validator("fee_multiplier", mode="before")
    @classmethod
    def _coerce_dec(cls, v):
        return _dec(v)

    def record(self) -> dict:
        return {
            "venue": self.venue, "series_ticker": self.series_ticker, "title": self.title,
            "category": self.category, "tags_json": json.dumps(self.tags),
            "frequency": self.frequency,
            "settlement_sources_json": json.dumps([s.model_dump() for s in self.settlement_sources]),
            "contract_url": self.contract_url, "contract_terms_url": self.contract_terms_url,
            "fee_multiplier": None if self.fee_multiplier is None else str(self.fee_multiplier),
            "additional_prohibitions_json": json.dumps(self.additional_prohibitions),
            "raw_payload_hash": self.raw_payload_hash, "updated_ts_ms": self.updated_ts_ms,
        }


class ResolutionRuleSet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    event_ticker: Optional[str] = None
    series_ticker: Optional[str] = None
    question: str = ""
    outcome: Optional[str] = None
    rules_primary: Optional[str] = None
    rules_secondary: Optional[str] = None
    settlement_sources: list[SettlementSource] = Field(default_factory=list)
    contract_url: Optional[str] = None
    contract_terms_url: Optional[str] = None
    close_ts_ms: Optional[int] = None
    latest_expiration_ts_ms: Optional[int] = None
    can_close_early: Optional[bool] = None
    ambiguity_categories: list[str] = Field(default_factory=list)
    ambiguity_score: float = 0.0
    parsed_ts_ms: int = Field(default_factory=_now_ms)
    raw_text_hash: Optional[str] = None

    def record(self) -> dict:
        return {
            "venue": self.venue, "market_id": self.market_id, "market_ticker": self.market_ticker,
            "asset_id": self.asset_id, "event_ticker": self.event_ticker,
            "series_ticker": self.series_ticker, "question": self.question, "outcome": self.outcome,
            "rules_primary": self.rules_primary, "rules_secondary": self.rules_secondary,
            "settlement_sources_json": json.dumps([s.model_dump() for s in self.settlement_sources]),
            "contract_url": self.contract_url, "contract_terms_url": self.contract_terms_url,
            "close_ts_ms": self.close_ts_ms, "latest_expiration_ts_ms": self.latest_expiration_ts_ms,
            "can_close_early": None if self.can_close_early is None else int(self.can_close_early),
            "ambiguity_categories_json": json.dumps(self.ambiguity_categories),
            "ambiguity_score": str(self.ambiguity_score), "parsed_ts_ms": self.parsed_ts_ms,
            "raw_text_hash": self.raw_text_hash,
        }


class MarketLifecycleStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    status: str = "unknown"
    event_type: Optional[str] = None
    open_ts_ms: Optional[int] = None
    close_ts_ms: Optional[int] = None
    expected_expiration_ts_ms: Optional[int] = None
    determined_ts_ms: Optional[int] = None
    settled_ts_ms: Optional[int] = None
    updated_ts_ms: int = Field(default_factory=_now_ms)
    raw_payload_hash: Optional[str] = None

    def is_terminal(self) -> bool:
        return str(self.status).lower() in ("closed", "settled", "determined", "finalized",
                                            "resolved", "expired")


class MarketResolutionEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    outcome: Optional[str] = None
    yes_settlement_value: Optional[Decimal] = None
    no_settlement_value: Optional[Decimal] = None
    resolved_ts_ms: Optional[int] = None
    settled_ts_ms: Optional[int] = None
    source: str = ""
    raw_payload_hash: Optional[str] = None

    @field_validator("yes_settlement_value", "no_settlement_value", mode="before")
    @classmethod
    def _coerce_dec(cls, v):
        return _dec(v)


# --------------------------------------------------------------------------- #
# Orderbook (binary YES/NO) models
# --------------------------------------------------------------------------- #
class OrderbookLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    price: Decimal
    size: Decimal

    @field_validator("price", "size", mode="before")
    @classmethod
    def _coerce_dec(cls, v):
        return Decimal(str(v))


class BBO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    market_ticker: Optional[str] = None
    market_id: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    midpoint: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    ts_ms: int = Field(default_factory=_now_ms)


class NormalizedBinaryOrderbook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    midpoint: Optional[Decimal] = None
    last_update_ms: int = Field(default_factory=_now_ms)
    seq: Optional[int] = None
    stale: bool = False
    gap_detected: bool = False
    needs_snapshot: bool = False
    crossed: bool = False
    valid: bool = True


class KalshiOrderbookSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    market_ticker: str
    market_id: Optional[str] = None
    seq: Optional[int] = None
    yes_bids: list[OrderbookLevel] = Field(default_factory=list)
    no_bids: list[OrderbookLevel] = Field(default_factory=list)
    ts_ms: Optional[int] = None


class KalshiOrderbookDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    market_ticker: str
    market_id: Optional[str] = None
    seq: Optional[int] = None
    side: Literal["yes", "no"]
    price: Decimal
    delta: Decimal
    ts_ms: int = Field(default_factory=_now_ms)

    @field_validator("price", "delta", mode="before")
    @classmethod
    def _coerce_dec(cls, v):
        return Decimal(str(v))


class KalshiAuthConfig(BaseModel):
    """NEVER contains secret values — only presence flags + non-secret config."""

    model_config = ConfigDict(extra="ignore")
    environment: str = "demo"
    rest_base_url: str = ""
    ws_url: str = ""
    access_key_id_present: bool = False
    private_key_present: bool = False
    enabled: bool = False


class VenueStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    enabled: bool = False
    status: str = "disabled"
    supports_market_data: bool = False
    supports_metadata: bool = False
    supports_replay: bool = True
    detail: Optional[str] = None


class MarketDataStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    status: str = "disabled"
    last_message_ts_ms: Optional[int] = None
    messages_received: int = 0
    parse_errors: int = 0
    reconnect_count: int = 0
    subscribed_count: int = 0
    stale_count: int = 0
    seq_gap_count: int = 0
    resnapshot_count: int = 0


class MetadataSyncResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: VenueName
    markets_synced: int = 0
    series_synced: int = 0
    resolution_rules_synced: int = 0
    errors: int = 0
    detail: Optional[str] = None
