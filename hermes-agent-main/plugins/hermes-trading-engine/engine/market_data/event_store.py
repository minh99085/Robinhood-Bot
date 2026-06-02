"""RawEventStore — durable, best-effort persistence of market-data events.

Thin facade over the SQLite :class:`~engine.storage.Store`. Every method is
best-effort and never raises into the caller (market-data persistence must not
crash the feed or the dashboard). Secrets are never persisted — only public
market data.
"""

from __future__ import annotations

import time
from typing import Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


class RawEventStore:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ #
    def append_raw_event(self, source: str, event_type: str,
                         market_id: Optional[str], asset_id: Optional[str],
                         payload: dict, ts_ms: Optional[int] = None) -> None:
        self.store.append_raw_market_event(
            ts_ms=ts_ms or _now_ms(), source=source, venue="polymarket",
            event_type=event_type, market_id=market_id, asset_id=asset_id,
            payload=payload)

    def append_orderbook_snapshot(self, *, venue: str, market_id: str, asset_id: str,
                                  bids, asks, best_bid=None, best_ask=None,
                                  spread=None, midpoint=None, tick_size=None,
                                  ts_ms: Optional[int] = None) -> None:
        self.store.append_orderbook_snapshot(
            ts_ms=ts_ms or _now_ms(), venue=venue, market_id=market_id,
            asset_id=asset_id, bids=bids, asks=asks, best_bid=best_bid,
            best_ask=best_ask, spread=spread, midpoint=midpoint, tick_size=tick_size)

    def append_orderbook_delta(self, *, venue: str, market_id: str, asset_id: str,
                               side: str, price: str, size: str, action: str,
                               best_bid=None, best_ask=None,
                               ts_ms: Optional[int] = None) -> None:
        self.store.append_orderbook_delta(
            ts_ms=ts_ms or _now_ms(), venue=venue, market_id=market_id,
            asset_id=asset_id, side=side, price=price, size=size, action=action,
            best_bid=best_bid, best_ask=best_ask)

    def append_market_event(self, *, venue: str, market_id: str,
                            asset_id: Optional[str], event_type: str,
                            payload: dict, ts_ms: Optional[int] = None) -> None:
        self.store.append_market_event(
            ts_ms=ts_ms or _now_ms(), venue=venue, market_id=market_id,
            asset_id=asset_id, event_type=event_type, payload=payload)

    def update_health(self, **kw) -> None:
        self.store.upsert_market_data_health(**kw)

    def get_recent_events(self, limit: int = 100) -> list[dict]:
        return self.store.get_recent_raw_market_events(limit)

    def get_market_event_count(self, market_id: Optional[str] = None,
                               event_type: Optional[str] = None) -> int:
        return self.store.get_market_event_count(market_id=market_id, event_type=event_type)

    def prune_old_events(self, keep: int = 50000) -> None:
        self.store.prune_market_events(keep=keep)
