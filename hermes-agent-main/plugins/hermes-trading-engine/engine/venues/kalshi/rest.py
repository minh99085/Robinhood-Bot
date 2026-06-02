"""Kalshi READ-ONLY REST client (Phase 6).

GET-only market-data / metadata endpoints. There are intentionally NO order,
cancel, or portfolio methods on this class — see ``test_kalshi_rest_never_exposes
_order_endpoints``. Rate-limited with simple exponential backoff; supports cursor
pagination. Never logs key material.
"""

from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import urlsplit

from .auth import ReadOnlyKalshiSigner, redact

# Methods that must NEVER exist on a read-only client (asserted by tests).
_FORBIDDEN = ("place_order", "create_order", "submit_order", "cancel_order",
              "amend_order", "post", "put", "delete", "portfolio", "positions_private")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class KalshiRestClient:
    READ_ONLY = True

    def __init__(self, base_url: str, signer: Optional[ReadOnlyKalshiSigner] = None,
                 timeout_s: Optional[float] = None, max_retries: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self.base_url).path.rstrip("/")
        self.signer = signer
        self.timeout_s = timeout_s if timeout_s is not None else float(
            _env_int("KALSHI_REQUEST_TIMEOUT_SECONDS", 15))
        self.max_retries = max_retries if max_retries is not None else _env_int(
            "KALSHI_MAX_RETRIES", 2)

    # -- low level ------------------------------------------------------ #
    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        import httpx
        endpoint = "/" + endpoint.lstrip("/")
        url = self.base_url + endpoint
        sign_path = self._base_path + endpoint
        last_err: Optional[Exception] = None
        for attempt in range(max(1, self.max_retries + 1)):
            headers = {}
            if self.signer is not None:
                headers.update(self.signer.rest_headers("GET", sign_path))
            try:
                resp = httpx.get(url, params=params or {}, headers=headers, timeout=self.timeout_s)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001 — never leak secrets in the error
                last_err = e
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        raise RuntimeError(redact(f"kalshi GET {endpoint} failed: {last_err}"))

    # -- read-only endpoints -------------------------------------------- #
    def list_markets(self, *, status: Optional[str] = None, limit: int = 100,
                     cursor: Optional[str] = None, series_ticker: Optional[str] = None,
                     event_ticker: Optional[str] = None) -> dict:
        params = {"limit": min(1000, limit)}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/markets", params)

    def iter_markets(self, *, status: Optional[str] = None, max_markets: int = 100,
                     series_ticker: Optional[str] = None) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while len(out) < max_markets:
            page = self.list_markets(status=status, limit=min(1000, max_markets - len(out)),
                                     cursor=cursor, series_ticker=series_ticker)
            markets = page.get("markets") or []
            out.extend(markets)
            cursor = page.get("cursor")
            if not cursor or not markets:
                break
        return out[:max_markets]

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def get_market_orderbook(self, ticker: str, depth: Optional[int] = None) -> dict:
        params = {"depth": depth} if depth else None
        return self._get(f"/markets/{ticker}/orderbook", params)

    def get_orderbooks(self, tickers: list[str]) -> dict:
        return self._get("/markets/orderbooks", {"tickers": ",".join(tickers)})

    def get_series(self, series_ticker: str) -> dict:
        return self._get(f"/series/{series_ticker}").get("series", {})

    def list_series(self, *, category: Optional[str] = None, limit: int = 100) -> dict:
        params = {"limit": limit}
        if category:
            params["category"] = category
        return self._get("/series", params)

    def get_trades(self, *, ticker: Optional[str] = None, limit: int = 100,
                   cursor: Optional[str] = None) -> dict:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets/trades", params)
