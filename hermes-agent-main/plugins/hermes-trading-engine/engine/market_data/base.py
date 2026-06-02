"""MarketDataAdapter interface + connection-status constants.

A MarketDataAdapter is a *read-only* source of normalized market data. It can
never place or route an order; it only exposes books, BBOs, and health.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..schemas import BBO
from .orderbook import OrderbookState

CONN_DISCONNECTED = "disconnected"
CONN_CONNECTING = "connecting"
CONN_CONNECTED = "connected"
CONN_RECONNECTING = "reconnecting"
CONN_DEGRADED = "degraded"

# Statuses that mean "do not trust this feed for trading decisions".
UNHEALTHY_STATUSES = frozenset(
    {CONN_DISCONNECTED, CONN_CONNECTING, CONN_RECONNECTING, CONN_DEGRADED}
)


class MarketDataAdapter(ABC):
    """Abstract read-only market-data source."""

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def subscribe(self, asset_ids: list[str]) -> None:
        ...

    @abstractmethod
    async def unsubscribe(self, asset_ids: list[str]) -> None:
        ...

    @abstractmethod
    def get_status(self) -> dict:
        ...

    @abstractmethod
    def get_bbo(self, asset_id: str) -> Optional[BBO]:
        ...

    @abstractmethod
    def get_orderbook(self, asset_id: str) -> Optional[OrderbookState]:
        ...
