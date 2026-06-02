"""Live stock/ETF quote feed via Yahoo Finance (public, read-only, no key).

Yahoo's endpoint is unofficial and may rate-limit; we cache aggressively and
degrade gracefully. Read-only market data only — no order placement.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (HermesTradingEngine paper)"}

_cache: dict[str, tuple[float, object]] = {}
_TTL = 10.0


def _cached(key: str):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    return None


def get_quote(symbol: str, rng: str = "1d", interval: str = "5m",
              client: Optional[httpx.Client] = None) -> Optional[dict]:
    """Return {symbol, price, prev_close, change_pct, closes[]} or None."""
    key = f"q:{symbol}:{rng}:{interval}"
    cached = _cached(key)
    if cached is not None:
        return cached
    own = client is None
    client = client or httpx.Client(timeout=8.0, headers=_HEADERS)
    try:
        r = client.get(_QUOTE.format(symbol=symbol), params={"range": rng, "interval": interval})
        if r.status_code != 200:
            return None
        data = r.json()["chart"]["result"][0]
        meta = data.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        closes = []
        try:
            raw = data["indicators"]["quote"][0]["close"]
            closes = [c for c in raw if c is not None]
        except Exception:
            closes = []
        if price is None and closes:
            price = closes[-1]
        if price is None:
            return None
        change_pct = ((price - prev) / prev * 100.0) if prev else 0.0
        out = {
            "symbol": symbol,
            "price": float(price),
            "prev_close": float(prev) if prev else float(price),
            "change_pct": float(change_pct),
            "closes": [float(c) for c in closes][-300:],
        }
        _cache[key] = (time.time(), out)
        return out
    except Exception:
        return None
    finally:
        if own:
            client.close()
