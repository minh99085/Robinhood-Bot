"""Polymarket public market data via the Gamma API (read-only, no key).

We only READ markets and prices here. This engine never signs or submits
Polymarket orders — paper bets are simulated against the public midpoint
price. Docs: https://docs.polymarket.com (Gamma + CLOB public endpoints).
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

_GAMMA = "https://gamma-api.polymarket.com"

_cache: dict[str, tuple[float, object]] = {}
_TTL = 15.0


def _cached(key: str):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    return None


def _parse_clob_token_ids(m: dict) -> list[str]:
    """Extract CLOB token (asset) ids from a Gamma market record.

    Gamma exposes them as ``clobTokenIds`` (often a JSON-encoded string list).
    These are the asset ids the read-only CLOB WebSocket subscribes to.
    """
    raw = m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokens")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return []
    out: list[str] = []
    for t in raw or []:
        if isinstance(t, dict):
            t = t.get("token_id") or t.get("id")
        if t:
            out.append(str(t))
    return out


def clob_asset_map(markets: list[dict]) -> dict[str, list[str]]:
    """Build {market_id: [token_ids]} from a list of trending markets."""
    return {m["id"]: m.get("clob_token_ids") or [] for m in markets
            if m.get("id") and m.get("clob_token_ids")}


def get_trending_markets(limit: int = 8, client: Optional[httpx.Client] = None) -> list[dict]:
    """Return a list of active markets with yes/no prices.

    Each item: {id, question, yes_price, no_price, volume, end_date, slug}.
    """
    key = f"markets:{limit}"
    cached = _cached(key)
    if cached is not None:
        return cached
    own = client is None
    client = client or httpx.Client(timeout=8.0)
    try:
        r = client.get(
            f"{_GAMMA}/markets",
            params={"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"},
        )
        if r.status_code != 200:
            return []
        out = []
        for m in r.json():
            prices = m.get("outcomePrices")
            yes = no = None
            try:
                if isinstance(prices, str):
                    import json as _json
                    prices = _json.loads(prices)
                if prices and len(prices) >= 2:
                    yes = float(prices[0])
                    no = float(prices[1])
            except Exception:
                pass
            out.append({
                "id": str(m.get("id") or m.get("conditionId") or ""),
                "question": m.get("question") or m.get("title") or "",
                "yes_price": yes,
                "no_price": no,
                "volume": float(m.get("volume") or m.get("volume24hr") or 0.0),
                "end_date": m.get("endDate") or m.get("end_date_iso") or "",
                "slug": m.get("slug") or "",
                # CLOB token (asset) ids for the read-only market-data feed.
                "clob_token_ids": _parse_clob_token_ids(m),
            })
        out = [m for m in out if m["yes_price"] is not None][:limit]
        _cache[key] = (time.time(), out)
        return out
    except Exception:
        return []
    finally:
        if own:
            client.close()
