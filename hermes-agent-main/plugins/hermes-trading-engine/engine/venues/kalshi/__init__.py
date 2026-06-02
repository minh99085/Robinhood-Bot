"""Kalshi read-only venue adapter (Phase 6).

Wires the read-only signer + REST metadata client + market-data WS client +
normalizer into the venue-neutral provider interfaces. NO order placement,
cancellation, or private user channels exist anywhere here.
"""

from __future__ import annotations

import os
from typing import Optional

from ..base import VenueAdapter
from ..identifiers import normalize_outcome, primary_ident
from ..metadata import (
    BBO,
    MarketDataStatus,
    MarketFilter,
    MarketLifecycleStatus,
    MarketRef,
    MarketResolutionEvent,
    MetadataSyncResult,
    NormalizedBinaryOrderbook,
    ResolutionRuleSet,
    VenueMarketMetadata,
    VenueSeriesMetadata,
    VenueStatus,
)
from ..resolution import build_resolution_ruleset
from .auth import DISABLED, READY, load_kalshi_auth
from .lifecycle import parse_lifecycle, parse_resolution
from .normalizer import normalize_market, normalize_series
from .rest import KalshiRestClient
from .ws import KalshiWSClient


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


class KalshiVenueAdapter(VenueAdapter):
    venue_name = "kalshi"

    def __init__(self, store=None):
        self.store = store
        self.config, self.signer, self.auth_status = load_kalshi_auth()
        self.rest: Optional[KalshiRestClient] = None
        self.ws: Optional[KalshiWSClient] = None
        if self.auth_status == READY:
            self.rest = KalshiRestClient(self.config.rest_base_url, self.signer)
            self.ws = KalshiWSClient(self.config.ws_url, self.signer, store=store,
                                     persist_raw=_flag("KALSHI_WS_PERSIST_RAW", "1"))

    # -- VenueAdapter --------------------------------------------------- #
    async def start(self) -> None:  # network loop intentionally not auto-started here
        return None

    async def stop(self) -> None:
        return None

    def get_status(self) -> VenueStatus:
        status = self.auth_status if self.auth_status != READY else "ready"
        return VenueStatus(
            venue="kalshi", enabled=self.config.enabled,
            status=status if self.config.enabled else DISABLED,
            supports_market_data=self.auth_status == READY,
            supports_metadata=self.auth_status == READY, supports_replay=True,
            detail=f"env={self.config.environment}")

    def supports_market_data(self) -> bool:
        return self.auth_status == READY

    def supports_metadata(self) -> bool:
        return self.auth_status == READY

    # -- metadata provider ---------------------------------------------- #
    def list_markets(self, filters: MarketFilter) -> list[VenueMarketMetadata]:
        if self.rest is None:
            return []
        raws = self.rest.iter_markets(status=filters.status, max_markets=filters.limit,
                                      series_ticker=filters.series_ticker)
        return [normalize_market(r) for r in raws]

    def get_market(self, market_ref: MarketRef) -> Optional[VenueMarketMetadata]:
        if self.rest is None:
            return None
        raw = self.rest.get_market(primary_ident(market_ref))
        return normalize_market(raw) if raw else None

    def get_series(self, series_ref: str) -> Optional[VenueSeriesMetadata]:
        if self.rest is None:
            return None
        raw = self.rest.get_series(series_ref)
        return normalize_series(raw) if raw else None

    def sync_metadata(self, filters: MarketFilter) -> MetadataSyncResult:
        if self.rest is None or self.store is None:
            return MetadataSyncResult(venue="kalshi", detail=self.auth_status)
        markets = self.list_markets(filters)
        series_synced = 0
        rules_synced = 0
        seen_series: set[str] = set()
        for m in markets:
            self.store.upsert_venue_market(m.record())
            rules = build_resolution_ruleset(m)
            self.store.upsert_resolution_rules(rules.record())
            rules_synced += 1
            if _flag("KALSHI_SYNC_SERIES", "1") and m.series_ticker and m.series_ticker not in seen_series:
                seen_series.add(m.series_ticker)
                s = self.get_series(m.series_ticker)
                if s is not None:
                    self.store.upsert_venue_series(s.record())
                    series_synced += 1
        return MetadataSyncResult(venue="kalshi", markets_synced=len(markets),
                                  series_synced=series_synced, resolution_rules_synced=rules_synced)

    # -- market data provider ------------------------------------------- #
    def get_orderbook(self, market_ref: MarketRef,
                      outcome: Optional[str] = None) -> Optional[NormalizedBinaryOrderbook]:
        if self.ws is None:
            return None
        return self.ws.get_orderbook(primary_ident(market_ref), normalize_outcome(outcome))

    def get_bbo(self, market_ref: MarketRef, outcome: Optional[str] = None) -> Optional[BBO]:
        if self.ws is None:
            return None
        return self.ws.get_bbo(primary_ident(market_ref), normalize_outcome(outcome))

    def get_market_data_status(self) -> MarketDataStatus:
        if self.ws is None:
            return MarketDataStatus(venue="kalshi", status=self.auth_status)
        return self.ws.status_snapshot()

    # -- lifecycle / resolution ----------------------------------------- #
    def get_market_status(self, market_ref: MarketRef) -> Optional[MarketLifecycleStatus]:
        if self.ws is None:
            return None
        return self.ws.lifecycle.get(primary_ident(market_ref))

    def get_resolution_rules(self, market_ref: MarketRef) -> Optional[ResolutionRuleSet]:
        meta = self.get_market(market_ref)
        if meta is None:
            return None
        series = self.get_series(meta.series_ticker) if meta.series_ticker else None
        return build_resolution_ruleset(meta, series, outcome=market_ref.outcome)

    def normalize_resolution_event(self, raw_event: dict) -> Optional[MarketResolutionEvent]:
        return parse_resolution(raw_event)
