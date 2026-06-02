"""Live crypto data feed (public, read-only, no API key).

Spot price, candle history, and order-book imbalance, each with multiple
public-source fallbacks so the engine keeps working when any single venue is
geo-blocked (notably Binance, HTTP 451 in the US):

  spot:    Binance ticker  -> Coinbase spot      -> Kraken ticker
  klines:  Binance klines  -> Coinbase candles   -> Kraken OHLC  -> synth
  book:    Coinbase L2      -> Kraken Depth

All read-only market data. No signing, no orders.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

_BINANCE = "https://api.binance.com"
_COINBASE = "https://api.coinbase.com/v2/prices"
_COINBASE_EX = "https://api.exchange.coinbase.com"
_KRAKEN = "https://api.kraken.com/0/public"

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 2.0
_KLINES_TTL = 20.0
_BOOK_TTL = 3.0


def _cached(key: str, ttl: float = _CACHE_TTL):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


def _base_asset(symbol: str) -> str:
    return symbol.replace("USDT", "").replace("USD", "")


def _coinbase_product(symbol: str) -> str:
    return f"{_base_asset(symbol)}-USD"


def _kraken_pair(symbol: str) -> str:
    base = _base_asset(symbol)
    if base == "BTC":
        base = "XBT"
    return f"{base}USDT"


# --------------------------------------------------------------------------
# spot
# --------------------------------------------------------------------------
def get_spot(symbol: str, client: Optional[httpx.Client] = None) -> Optional[float]:
    key = f"spot:{symbol}"
    cached = _cached(key)
    if cached is not None:
        return cached
    own = client is None
    client = client or httpx.Client(timeout=6.0)
    try:
        try:
            r = client.get(f"{_BINANCE}/api/v3/ticker/price", params={"symbol": symbol})
            if r.status_code == 200:
                return _store(key, float(r.json()["price"]))
        except Exception:
            pass
        try:
            r = client.get(f"{_COINBASE}/{_coinbase_product(symbol)}/spot")
            if r.status_code == 200:
                return _store(key, float(r.json()["data"]["amount"]))
        except Exception:
            pass
        try:
            r = client.get(f"{_KRAKEN}/Ticker", params={"pair": _kraken_pair(symbol)})
            if r.status_code == 200:
                for _k, v in r.json().get("result", {}).items():
                    return _store(key, float(v["c"][0]))
        except Exception:
            pass
        return None
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------
# candles
# --------------------------------------------------------------------------
def _binance_klines(symbol, interval, limit, client) -> list[dict]:
    r = client.get(f"{_BINANCE}/api/v3/klines",
                   params={"symbol": symbol, "interval": interval, "limit": limit})
    if r.status_code != 200:
        return []
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in r.json()]


def _coinbase_candles(symbol, client) -> list[dict]:
    r = client.get(f"{_COINBASE_EX}/products/{_coinbase_product(symbol)}/candles",
                   params={"granularity": 60})
    if r.status_code != 200:
        return []
    rows = r.json()
    if not isinstance(rows, list):
        return []
    out = [{"t": int(c[0]) * 1000, "l": float(c[1]), "h": float(c[2]),
            "o": float(c[3]), "c": float(c[4]), "v": float(c[5])}
           for c in rows if isinstance(c, list) and len(c) >= 6]
    out.sort(key=lambda d: d["t"])
    return out


def _kraken_ohlc(symbol, client) -> list[dict]:
    r = client.get(f"{_KRAKEN}/OHLC", params={"pair": _kraken_pair(symbol), "interval": 1})
    if r.status_code != 200:
        return []
    for key, rows in r.json().get("result", {}).items():
        if key == "last" or not isinstance(rows, list):
            continue
        return [{"t": int(c[0]) * 1000, "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4]), "v": float(c[6])}
                for c in rows if isinstance(c, list) and len(c) >= 7]
    return []


def get_klines(symbol: str, interval: str = "1m", limit: int = 500,
               client: Optional[httpx.Client] = None) -> list[dict]:
    key = f"klines:{symbol}:{interval}:{limit}"
    cached = _cached(key, _KLINES_TTL)
    if cached is not None:
        return cached
    own = client is None
    client = client or httpx.Client(timeout=8.0)
    try:
        for source in (lambda: _binance_klines(symbol, interval, limit, client),
                       lambda: _coinbase_candles(symbol, client),
                       lambda: _kraken_ohlc(symbol, client)):
            try:
                out = source()
            except Exception:
                out = []
            if out and len(out) >= 2:
                return _store(key, out[-limit:])
        spot = get_spot(symbol, client=client)
        if spot is None:
            return []
        now = int(time.time() * 1000)
        return _store(key, [{"t": now, "o": spot, "h": spot, "l": spot, "c": spot, "v": 0.0}])
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------
# order-book imbalance  (a real microstructure signal; live-only, not backtestable)
# --------------------------------------------------------------------------
def order_book_imbalance(symbol: str, depth: int = 20,
                         client: Optional[httpx.Client] = None) -> Optional[float]:
    """Return (bidVol - askVol) / (bidVol + askVol) over the top `depth` levels,
    in [-1, 1]. Positive = more bids (buy pressure). None if unavailable."""
    key = f"obi:{symbol}:{depth}"
    cached = _cached(key, _BOOK_TTL)
    if cached is not None:
        return cached
    own = client is None
    client = client or httpx.Client(timeout=6.0)
    try:
        # Coinbase L2
        try:
            r = client.get(f"{_COINBASE_EX}/products/{_coinbase_product(symbol)}/book",
                           params={"level": 2})
            if r.status_code == 200:
                d = r.json()
                bids = sum(float(x[1]) for x in d.get("bids", [])[:depth])
                asks = sum(float(x[1]) for x in d.get("asks", [])[:depth])
                if bids + asks > 0:
                    return _store(key, round((bids - asks) / (bids + asks), 4))
        except Exception:
            pass
        # Kraken Depth
        try:
            r = client.get(f"{_KRAKEN}/Depth", params={"pair": _kraken_pair(symbol), "count": depth})
            if r.status_code == 200:
                for _k, ob in r.json().get("result", {}).items():
                    bids = sum(float(x[1]) for x in ob.get("bids", [])[:depth])
                    asks = sum(float(x[1]) for x in ob.get("asks", [])[:depth])
                    if bids + asks > 0:
                        return _store(key, round((bids - asks) / (bids + asks), 4))
        except Exception:
            pass
        return None
    finally:
        if own:
            client.close()
