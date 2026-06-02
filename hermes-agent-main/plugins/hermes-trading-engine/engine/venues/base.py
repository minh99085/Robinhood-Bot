"""Venue-neutral provider interfaces (Phase 6).

These ABCs define what a venue adapter (Polymarket, Kalshi) exposes. They are all
READ-ONLY: there is no order-placement or cancellation surface anywhere in this
package. Every paper/shadow order still flows through RiskEngine + OMS + PaperBroker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .metadata import (
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


class MarketMetadataProvider(ABC):
    @abstractmethod
    def list_markets(self, filters: MarketFilter) -> list[VenueMarketMetadata]: ...

    @abstractmethod
    def get_market(self, market_ref: MarketRef) -> Optional[VenueMarketMetadata]: ...

    @abstractmethod
    def get_series(self, series_ref: str) -> Optional[VenueSeriesMetadata]: ...

    @abstractmethod
    def sync_metadata(self, filters: MarketFilter) -> MetadataSyncResult: ...


class MarketDataProvider(ABC):
    @abstractmethod
    def subscribe_orderbooks(self, market_refs: list[MarketRef]) -> None: ...

    @abstractmethod
    def unsubscribe_orderbooks(self, market_refs: list[MarketRef]) -> None: ...

    @abstractmethod
    def get_orderbook(self, market_ref: MarketRef,
                      outcome: Optional[str] = None) -> Optional[NormalizedBinaryOrderbook]: ...

    @abstractmethod
    def get_bbo(self, market_ref: MarketRef,
                outcome: Optional[str] = None) -> Optional[BBO]: ...

    @abstractmethod
    def get_status(self) -> MarketDataStatus: ...


class LifecycleProvider(ABC):
    @abstractmethod
    def subscribe_lifecycle(self) -> None: ...

    @abstractmethod
    def get_recent_lifecycle_events(self, limit: int = 100) -> list[dict]: ...

    @abstractmethod
    def get_market_status(self, market_ref: MarketRef) -> Optional[MarketLifecycleStatus]: ...


class ResolutionProvider(ABC):
    @abstractmethod
    def get_resolution_rules(self, market_ref: MarketRef) -> Optional[ResolutionRuleSet]: ...

    @abstractmethod
    def get_resolution_status(self, market_ref: MarketRef): ...

    @abstractmethod
    def normalize_resolution_event(self, raw_event: dict) -> Optional[MarketResolutionEvent]: ...


class VenueAdapter(ABC):
    """A venue adapter aggregates the read-only providers above."""

    venue_name: str = "unknown"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def get_status(self) -> VenueStatus: ...

    def supports_market_data(self) -> bool:
        return False

    def supports_metadata(self) -> bool:
        return False

    def supports_replay(self) -> bool:
        return True
