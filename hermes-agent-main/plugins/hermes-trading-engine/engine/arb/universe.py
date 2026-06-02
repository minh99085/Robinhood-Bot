"""UniverseManager — eligible symbols (price < $120 filter, capped count).

Approximates "active CMC top-100 under $120" without a CMC key: takes the
configured symbol set, caps the count (to spare public API rate limits), and
keeps those whose current mid price is under the threshold.
"""

from __future__ import annotations

import os
import time


class UniverseManager:
    def __init__(self, feeds, mapper, max_price: float | None = None, refresh_s: float = 60.0):
        self.feeds = feeds
        self.mapper = mapper
        self.max_price = max_price if max_price is not None else float(os.getenv("ARB_MAX_PRICE", "120"))
        self.max_symbols = int(os.getenv("ARB_MAX_SYMBOLS", "6"))
        self.refresh_s = refresh_s
        self._active: dict[str, float] = {}
        self._ts = 0.0

    def _refresh(self) -> None:
        active = {}
        for sym in self.mapper.all_symbols()[: self.max_symbols]:
            tick = self.feeds.get_latest_tick("coinbase", sym)
            if tick and 0 < tick["mid"] < self.max_price:
                active[sym] = tick["mid"]
        self._active = active
        self._ts = time.time()

    def active_symbols(self) -> list[str]:
        if time.time() - self._ts > self.refresh_s or not self._active:
            self._refresh()
        return list(self._active.keys())

    def is_active(self, symbol: str) -> bool:
        if time.time() - self._ts > self.refresh_s:
            self._refresh()
        return symbol.upper() in self._active

    def price(self, symbol: str) -> float | None:
        return self._active.get(symbol.upper())
