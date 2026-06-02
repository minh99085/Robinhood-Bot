"""FeedAggregator — per-exchange best bid/ask (public REST, no API key).

getLatestTick(exchange, symbol) -> {"bid","ask","mid","ts"} or None.
Coinbase / Kraken / Bitstamp public ticker endpoints; cached briefly.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

_CB = "https://api.exchange.coinbase.com"
_KR = "https://api.kraken.com/0/public"
_BS = "https://www.bitstamp.net/api/v2"


class FeedAggregator:
    def __init__(self, mapper, ttl: float = 1.5):
        self.mapper = mapper
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._client = httpx.Client(timeout=6.0, headers={"User-Agent": "hte-arb"})

    def _cached(self, key):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        return None

    def get_latest_tick(self, exchange: str, symbol: str) -> Optional[dict]:
        key = f"{exchange}:{symbol}"
        c = self._cached(key)
        if c is not None:
            return c
        pair = self.mapper.pair(symbol, exchange)
        tick = None
        try:
            if exchange == "coinbase":
                r = self._client.get(f"{_CB}/products/{pair}/ticker")
                if r.status_code == 200:
                    d = r.json()
                    tick = {"bid": float(d["bid"]), "ask": float(d["ask"])}
            elif exchange == "kraken":
                r = self._client.get(f"{_KR}/Ticker", params={"pair": pair})
                if r.status_code == 200:
                    for _k, v in r.json().get("result", {}).items():
                        tick = {"bid": float(v["b"][0]), "ask": float(v["a"][0])}
                        break
            elif exchange == "bitstamp":
                r = self._client.get(f"{_BS}/ticker/{pair}/")
                if r.status_code == 200:
                    d = r.json()
                    tick = {"bid": float(d["bid"]), "ask": float(d["ask"])}
        except Exception:
            tick = None
        if tick and tick["ask"] > 0 and tick["bid"] > 0:
            tick["mid"] = round((tick["bid"] + tick["ask"]) / 2, 8)
            tick["ts"] = time.time()
        else:
            tick = None
        self._cache[key] = (time.time(), tick)
        return tick
