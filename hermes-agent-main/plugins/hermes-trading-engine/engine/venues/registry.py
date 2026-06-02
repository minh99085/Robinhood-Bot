"""VenueRegistry — registers venue adapters and routes calls by venue (Phase 6).

Venues are enabled via ``VENUES_ENABLED`` (comma-separated). Routing is read-only:
metadata, orderbook, lifecycle, and resolution. There is no order routing here —
orders always go through RiskEngine + OMS + PaperBroker.
"""

from __future__ import annotations

import os
from typing import Optional

from .base import VenueAdapter
from .metadata import MarketRef, VenueStatus


def enabled_venues() -> list[str]:
    raw = os.getenv("VENUES_ENABLED", "polymarket")
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


class VenueRegistry:
    def __init__(self):
        self._adapters: dict[str, VenueAdapter] = {}

    def register(self, adapter: VenueAdapter) -> None:
        self._adapters[adapter.venue_name] = adapter

    def get(self, venue: str) -> Optional[VenueAdapter]:
        return self._adapters.get(venue)

    def venues(self) -> list[str]:
        return sorted(self._adapters.keys())

    def is_enabled(self, venue: str) -> bool:
        return venue in enabled_venues() and venue in self._adapters

    def statuses(self) -> list[VenueStatus]:
        out = []
        enabled = enabled_venues()
        for name, adapter in sorted(self._adapters.items()):
            try:
                st = adapter.get_status()
            except Exception:  # noqa: BLE001
                st = VenueStatus(venue=name, status="error")
            st.enabled = st.enabled and (name in enabled)
            out.append(st)
        return out

    # -- read-only routing --------------------------------------------- #
    def get_market(self, ref: MarketRef):
        a = self.get(ref.venue)
        return a.get_market(ref) if a and hasattr(a, "get_market") else None

    def get_orderbook(self, ref: MarketRef, outcome: Optional[str] = None):
        a = self.get(ref.venue)
        return a.get_orderbook(ref, outcome) if a and hasattr(a, "get_orderbook") else None

    def get_bbo(self, ref: MarketRef, outcome: Optional[str] = None):
        a = self.get(ref.venue)
        return a.get_bbo(ref, outcome) if a and hasattr(a, "get_bbo") else None

    def get_market_status(self, ref: MarketRef):
        a = self.get(ref.venue)
        return a.get_market_status(ref) if a and hasattr(a, "get_market_status") else None

    def get_resolution_rules(self, ref: MarketRef):
        a = self.get(ref.venue)
        return a.get_resolution_rules(ref) if a and hasattr(a, "get_resolution_rules") else None


def build_default_registry(store=None, market_data=None) -> VenueRegistry:
    """Register Polymarket (always) + Kalshi (read-only, degrades if no creds)."""
    from .kalshi import KalshiVenueAdapter
    from .polymarket import PolymarketVenueAdapter
    reg = VenueRegistry()
    reg.register(PolymarketVenueAdapter(store=store, market_data=market_data))
    try:
        reg.register(KalshiVenueAdapter(store=store))
    except Exception:  # noqa: BLE001 — Kalshi must never break startup
        pass
    return reg
