"""Venue-neutral prediction-market layer (Phase 6).

Read-only integration for Polymarket and Kalshi: normalized market metadata,
lifecycle, resolution rules, and binary YES/NO orderbooks. No order placement,
cancellation, live broker, or private user channels exist in this package.
"""

from __future__ import annotations

from .metadata import (  # noqa: F401
    BBO,
    KalshiAuthConfig,
    KalshiOrderbookDelta,
    KalshiOrderbookSnapshot,
    MarketDataStatus,
    MarketFilter,
    MarketLifecycleStatus,
    MarketRef,
    MarketResolutionEvent,
    MetadataSyncResult,
    NormalizedBinaryOrderbook,
    OrderbookLevel,
    ResolutionRuleSet,
    SettlementSource,
    VenueMarketMetadata,
    VenueName,
    VenueSeriesMetadata,
    VenueStatus,
)
from .registry import VenueRegistry, build_default_registry, enabled_venues  # noqa: F401
from .resolution import build_resolution_ruleset  # noqa: F401

__all__ = [
    "MarketRef", "VenueMarketMetadata", "VenueSeriesMetadata", "SettlementSource",
    "ResolutionRuleSet", "MarketLifecycleStatus", "MarketResolutionEvent",
    "KalshiAuthConfig", "KalshiOrderbookSnapshot", "KalshiOrderbookDelta",
    "NormalizedBinaryOrderbook", "OrderbookLevel", "BBO", "MarketFilter",
    "MarketDataStatus", "MetadataSyncResult", "VenueStatus", "VenueName",
    "VenueRegistry", "build_default_registry", "enabled_venues",
    "build_resolution_ruleset",
]
