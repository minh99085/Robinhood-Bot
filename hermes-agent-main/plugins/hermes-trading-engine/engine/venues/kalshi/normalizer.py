"""Normalize raw Kalshi REST/WS payloads into venue-neutral schemas (Phase 6).

Best-effort and defensive: a missing field never crashes. Kalshi quotes prices in
integer cents (1..99); we convert to dollars/probability in [0,1].
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..metadata import (
    KalshiOrderbookSnapshot,
    OrderbookLevel,
    SettlementSource,
    VenueMarketMetadata,
    VenueSeriesMetadata,
    payload_hash,
)

CENTS = Decimal("100")


def cents_to_dollars(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return (Decimal(str(v)) / CENTS)
    except Exception:  # noqa: BLE001
        return None


def _ts_ms(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # already epoch (seconds or ms)
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _settlement_sources(raw) -> list[SettlementSource]:
    out = []
    for s in raw or []:
        if isinstance(s, dict):
            out.append(SettlementSource(name=s.get("name") or s.get("source") or "",
                                        url=s.get("url"), source_type=s.get("type")))
        elif isinstance(s, str):
            out.append(SettlementSource(name=s))
    return out


def normalize_market(raw: dict, series_ticker: Optional[str] = None) -> VenueMarketMetadata:
    raw = raw or {}
    ticker = raw.get("ticker") or raw.get("market_ticker")
    fee_meta = {}
    if raw.get("rules_primary"):
        fee_meta["rules_primary"] = raw.get("rules_primary")
    if raw.get("rules_secondary"):
        fee_meta["rules_secondary"] = raw.get("rules_secondary")
    for fk in ("maker_fee", "taker_fee", "fee_multiplier", "notional_value"):
        if raw.get(fk) is not None:
            fee_meta[fk] = raw.get(fk)
    return VenueMarketMetadata(
        venue="kalshi", market_ticker=ticker, market_id=raw.get("market_id"),
        event_ticker=raw.get("event_ticker"),
        series_ticker=series_ticker or raw.get("series_ticker"),
        question=raw.get("title") or raw.get("subtitle") or "",
        title=raw.get("title"), yes_title=raw.get("yes_sub_title") or raw.get("yes_subtitle"),
        no_title=raw.get("no_sub_title") or raw.get("no_subtitle"),
        outcomes=["YES", "NO"], category=raw.get("category"),
        tags=list(raw.get("tags") or []), status=raw.get("status") or "unknown",
        open_ts_ms=_ts_ms(raw.get("open_time")), close_ts_ms=_ts_ms(raw.get("close_time")),
        latest_expiration_ts_ms=_ts_ms(raw.get("latest_expiration_time")
                                       or raw.get("expiration_time")),
        settlement_timer_seconds=raw.get("settlement_timer_seconds"),
        can_close_early=raw.get("can_close_early"),
        fractional_trading_enabled=raw.get("fractional_trading_enabled"),
        volume=raw.get("volume"), volume_24h=raw.get("volume_24h"),
        open_interest=raw.get("open_interest"),
        last_price=cents_to_dollars(raw.get("last_price")),
        yes_bid=cents_to_dollars(raw.get("yes_bid")), yes_ask=cents_to_dollars(raw.get("yes_ask")),
        no_bid=cents_to_dollars(raw.get("no_bid")), no_ask=cents_to_dollars(raw.get("no_ask")),
        price_level_structure=raw.get("price_level_structure") or "cents",
        min_tick_size=cents_to_dollars(raw.get("tick_size") or 1),
        fee_metadata=fee_meta,
        settlement_sources=_settlement_sources(raw.get("settlement_sources")),
        contract_url=raw.get("contract_url"),
        contract_terms_url=raw.get("contract_terms_url") or raw.get("rules_url"),
        raw_payload_hash=payload_hash(raw))


def normalize_series(raw: dict) -> VenueSeriesMetadata:
    raw = raw or {}
    return VenueSeriesMetadata(
        venue="kalshi", series_ticker=raw.get("ticker") or raw.get("series_ticker") or "",
        title=raw.get("title") or "", category=raw.get("category"),
        tags=list(raw.get("tags") or []), frequency=raw.get("frequency"),
        settlement_sources=_settlement_sources(raw.get("settlement_sources")),
        contract_url=raw.get("contract_url"),
        contract_terms_url=raw.get("contract_terms_url") or raw.get("rules_url"),
        fee_multiplier=raw.get("fee_multiplier"),
        additional_prohibitions=list(raw.get("additional_prohibitions") or []),
        raw_payload_hash=payload_hash(raw))


def _levels(raw_levels) -> list[OrderbookLevel]:
    out = []
    for lvl in raw_levels or []:
        try:
            price_cents, size = lvl[0], lvl[1]
        except (TypeError, IndexError, KeyError):
            continue
        price = cents_to_dollars(price_cents)
        if price is None:
            continue
        out.append(OrderbookLevel(price=price, size=Decimal(str(size))))
    return out


def normalize_orderbook_rest(market_ticker: str, raw: dict,
                             seq: Optional[int] = None) -> KalshiOrderbookSnapshot:
    book = (raw or {}).get("orderbook") or raw or {}
    return KalshiOrderbookSnapshot(
        market_ticker=market_ticker, seq=seq,
        yes_bids=_levels(book.get("yes")), no_bids=_levels(book.get("no")))
