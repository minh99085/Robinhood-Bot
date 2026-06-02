"""Read-only market-data layer (Phase 2).

Normalized order book state, a raw-event audit store, and a Polymarket CLOB
WebSocket client. NOTHING here can submit an order — it is strictly read-only
market data. Stale / malformed / disconnected / tick-size-changed state is
surfaced to the deterministic RiskEngine, which rejects affected proposals.
"""

from .base import (
    CONN_CONNECTED,
    CONN_CONNECTING,
    CONN_DEGRADED,
    CONN_DISCONNECTED,
    CONN_RECONNECTING,
    MarketDataAdapter,
)
from .event_store import RawEventStore
from .orderbook import OrderbookState

__all__ = [
    "MarketDataAdapter",
    "OrderbookState",
    "RawEventStore",
    "CONN_DISCONNECTED",
    "CONN_CONNECTING",
    "CONN_CONNECTED",
    "CONN_RECONNECTING",
    "CONN_DEGRADED",
]
