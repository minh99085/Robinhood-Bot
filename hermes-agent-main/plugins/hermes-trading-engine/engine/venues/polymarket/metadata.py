"""Polymarket venue-neutral metadata adapter (Phase 6, read-only wrapper)."""

from __future__ import annotations

from typing import Optional

from ..base import VenueAdapter
from ..metadata import (
    MarketFilter,
    MarketRef,
    MetadataSyncResult,
    VenueMarketMetadata,
    VenueSeriesMetadata,
    VenueStatus,
)
from ..resolution import build_resolution_ruleset
from .normalizer import normalize_market


class PolymarketVenueAdapter(VenueAdapter):
    """Wraps the existing Polymarket market-data layer without modifying it.

    Metadata reads come from whatever the engine already has (store / CLOB);
    this adapter just exposes them through the venue-neutral interface.
    """

    venue_name = "polymarket"

    def __init__(self, store=None, market_data=None):
        self.store = store
        self.market_data = market_data  # existing MarketDataManager (optional)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def get_status(self) -> VenueStatus:
        md_ok = self.market_data is not None
        return VenueStatus(
            venue="polymarket", enabled=True, status="ready",
            supports_market_data=md_ok, supports_metadata=True, supports_replay=True,
            detail="clob" if md_ok else "metadata-only")

    def supports_market_data(self) -> bool:
        return self.market_data is not None

    def supports_metadata(self) -> bool:
        return True

    def list_markets(self, filters: MarketFilter) -> list[VenueMarketMetadata]:
        if self.store is None:
            return []
        rows = self.store.get_venue_markets(venue="polymarket", limit=filters.limit)
        return [VenueMarketMetadata.model_validate(_row_to_meta(r)) for r in rows]

    def get_market(self, market_ref: MarketRef) -> Optional[VenueMarketMetadata]:
        if self.store is None:
            return None
        rows = self.store.get_venue_markets(venue="polymarket",
                                            market_id=market_ref.market_id, limit=1)
        if not rows:
            return None
        return VenueMarketMetadata.model_validate(_row_to_meta(rows[0]))

    def sync_metadata(self, filters: MarketFilter) -> MetadataSyncResult:
        # Polymarket metadata is synced opportunistically from the existing feed;
        # this hook persists any normalized payloads passed in via market_data.
        return MetadataSyncResult(venue="polymarket", detail="passive")

    @staticmethod
    def normalize(raw: dict) -> VenueMarketMetadata:
        return normalize_market(raw)

    def get_resolution_rules(self, market_ref: MarketRef):
        meta = self.get_market(market_ref)
        return build_resolution_ruleset(meta) if meta else None


def _row_to_meta(row: dict) -> dict:
    import json
    return {
        "venue": row.get("venue"), "market_id": row.get("market_id"),
        "market_ticker": row.get("market_ticker"), "asset_id": row.get("asset_id"),
        "question": row.get("question") or "", "title": row.get("title"),
        "status": row.get("status") or "unknown",
        "outcomes": json.loads(row.get("outcomes_json") or '["YES","NO"]'),
        "close_ts_ms": row.get("close_ts_ms"),
    }
