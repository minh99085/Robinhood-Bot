"""Helpers for venue-neutral market identifiers."""

from __future__ import annotations

from typing import Optional

from .metadata import MarketRef


def make_ref(venue: str, *, market_id: Optional[str] = None, market_ticker: Optional[str] = None,
             asset_id: Optional[str] = None, event_ticker: Optional[str] = None,
             series_ticker: Optional[str] = None, outcome: Optional[str] = None) -> MarketRef:
    return MarketRef(venue=venue, market_id=market_id, market_ticker=market_ticker,
                     asset_id=asset_id, event_ticker=event_ticker,
                     series_ticker=series_ticker, outcome=outcome)


def primary_ident(ref: MarketRef) -> str:
    """The natural primary identifier for a venue (ticker for Kalshi; id for Polymarket)."""
    if ref.venue == "kalshi":
        return ref.market_ticker or ref.market_id or ""
    return ref.market_id or ref.asset_id or ref.market_ticker or ""


def normalize_outcome(outcome: Optional[str]) -> str:
    o = (outcome or "YES").upper()
    return "YES" if o not in ("YES", "NO") else o
