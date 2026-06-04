"""Read-only fast BTC spot price feed for BTC Pulse short-horizon features.

Separate from the Chainlink BTC/USD *anchor*: Chainlink is a slow, trusted
reference (hourly heartbeat), while this provides a fast (seconds-fresh) spot
price for 30s/60s/300s return features. Public, key-less, read-only — it never
touches a wallet, account, or order endpoint, and degrades gracefully when the
provider is unreachable.

Deterministic for tests via an injected ``fetch`` callable + ``clock``.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Optional

logger = logging.getLogger("hte.feeds.btc_fast_price")

_COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{symbol}/spot"


@dataclass
class BtcFastPriceStatus:
    """Validated snapshot of the fast BTC spot price (read-only)."""

    enabled: bool
    provider: str = "coinbase_readonly"
    symbol: str = "BTC-USD"
    price: Optional[float] = None
    observed_at: float = 0.0
    age_seconds: Optional[float] = None
    stale: bool = True
    valid: bool = False
    error: Optional[str] = None
    consecutive_failures: int = 0
    disagreement_vs_chainlink_bps: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def coinbase_spot_fetch(symbol: str = "BTC-USD", *, timeout: float = 5.0) -> Optional[float]:
    """Fetch the latest BTC spot price from Coinbase's public read-only endpoint.

    Returns a positive float, or None on any error (best-effort; never raises)."""
    try:
        import httpx
        url = _COINBASE_SPOT_URL.format(symbol=symbol)
        resp = httpx.get(url, timeout=timeout, headers={"User-Agent": "hermes-btc-fast/1.0"})
        resp.raise_for_status()
        amount = (resp.json() or {}).get("data", {}).get("amount")
        price = float(amount)
        return price if price > 0 else None
    except Exception:  # noqa: BLE001 — read-only + best-effort
        return None


class BtcFastPriceFeed:
    """Read-only fast BTC spot feed with a rolling history for short returns."""

    def __init__(self, *, enabled: bool = True, provider: str = "coinbase_readonly",
                 symbol: str = "BTC-USD", max_age_seconds: int = 10,
                 timeout_seconds: float = 5.0, max_retries: int = 2,
                 fetch: Optional[Callable[[], Optional[float]]] = None,
                 clock: Optional[Callable[[], float]] = None, log_enabled: bool = False,
                 history_limit: int = 600):
        self.enabled = bool(enabled)
        self.provider = provider
        self.symbol = symbol
        self.max_age_seconds = max(1, int(max_age_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._fetch = fetch
        import time as _t
        self._clock = clock or _t.time
        self.log_enabled = bool(log_enabled)
        self._hist: deque = deque(maxlen=max(2, int(history_limit)))
        self.consecutive_failures = 0
        self.last_observed_at: Optional[float] = None
        self.last_price: Optional[float] = None
        self._last: Optional[BtcFastPriceStatus] = None
        if self.enabled:
            logger.info("BTC fast price provider initialized provider=%s symbol=%s "
                        "max_age_seconds=%d", provider, symbol, self.max_age_seconds)

    def _do_fetch(self) -> Optional[float]:
        if self._fetch is not None:
            return self._fetch()
        last = None
        for _ in range(self.max_retries + 1):
            last = coinbase_spot_fetch(self.symbol, timeout=self.timeout_seconds)
            if last is not None:
                return last
        return last

    def read(self, now: Optional[float] = None,
             anchor_price: Optional[float] = None) -> BtcFastPriceStatus:
        now = float(now) if now is not None else float(self._clock())
        st = BtcFastPriceStatus(enabled=self.enabled, provider=self.provider,
                                symbol=self.symbol, observed_at=now)
        if not self.enabled:
            self._last = st
            return st
        price = None
        try:
            price = self._do_fetch()
        except Exception as exc:  # noqa: BLE001 — never raise from a read
            st.error = f"provider_error:{type(exc).__name__}"
        if price is not None and price > 0:
            self.last_price = float(price)
            self.last_observed_at = now
            self.consecutive_failures = 0
            self._hist.append((now, float(price)))
            st.price = float(price)
            st.observed_at = now
            st.age_seconds = 0.0
            st.stale = False
            st.valid = True
        else:
            self.consecutive_failures += 1
            st.error = st.error or "missing_price"
            st.price = self.last_price
            if self.last_observed_at is not None:
                st.observed_at = self.last_observed_at
                st.age_seconds = round(max(0.0, now - self.last_observed_at), 3)
                st.stale = st.age_seconds > self.max_age_seconds
            else:
                st.age_seconds = None
                st.stale = True
            st.valid = False
        st.consecutive_failures = self.consecutive_failures
        if anchor_price and st.price:
            st.disagreement_vs_chainlink_bps = disagreement_bps(st.price, anchor_price)
        self._last = st
        if self.log_enabled:
            logger.info("BTC fast price latest price=%s", st.price)
            logger.info("BTC fast price age_seconds=%s stale=%s", st.age_seconds, st.stale)
            if st.disagreement_vs_chainlink_bps is not None:
                logger.info("BTC fast price disagreement_vs_chainlink_bps=%s",
                            st.disagreement_vs_chainlink_bps)
        return st

    # -- short-horizon returns ----------------------------------------- #
    def return_over(self, seconds: float, now: Optional[float] = None) -> Optional[float]:
        """Return over the last ``seconds`` from the rolling history, or None."""
        if not self._hist:
            return None
        now = float(now) if now is not None else float(self._clock())
        cur = self._hist[-1][1]
        cutoff = now - float(seconds)
        old = None
        for ts, px in self._hist:
            if ts <= cutoff:
                old = px
            else:
                break
        if old is None or old <= 0:
            return None
        return round(cur / old - 1.0, 8)

    def last_status(self) -> BtcFastPriceStatus:
        if self._last is not None:
            return self._last
        return BtcFastPriceStatus(enabled=self.enabled, provider=self.provider,
                                  symbol=self.symbol, observed_at=float(self._clock()))

    def status(self) -> dict:
        return self.last_status().to_dict()


def disagreement_bps(price_a: float, price_b: float) -> Optional[float]:
    """Absolute disagreement in basis points between two prices, or None."""
    try:
        a, b = float(price_a), float(price_b)
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0:
        return None
    return round(abs(a - b) / b * 10_000.0, 3)
