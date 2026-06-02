"""Kalshi READ-ONLY market-data WebSocket client (Phase 6).

Subscribes only to public market-data channels: ``orderbook_delta``, ``ticker``,
``trade``, ``market_lifecycle_v2``. It NEVER subscribes to fill / user_orders /
market_positions / order_group_updates, and it NEVER places or cancels orders.

Message processing (``process_message``) is decoupled from the network loop so it
can be unit-tested without a live connection. Malformed messages increment
``parse_errors`` and never crash.
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Optional

from ..metadata import BBO, MarketDataStatus, _now_ms
from .auth import redact
from .lifecycle import parse_lifecycle, parse_resolution
from .orderbook import KalshiBinaryOrderbook

logger = logging.getLogger("hte.kalshi.ws")

READONLY_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")
# Channels that must NEVER be subscribed (private user data / execution).
FORBIDDEN_CHANNELS = frozenset({
    "fill", "user_orders", "market_positions", "communications", "order_group_updates",
})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class KalshiWSClient:
    """Read-only Kalshi market-data client. ``process_message`` is pure/testable."""

    def __init__(self, ws_url: str, signer=None, store=None, *, persist_raw: bool = True,
                 stale_ms: Optional[int] = None, channels: Optional[list[str]] = None):
        self.ws_url = ws_url
        self.signer = signer
        self.store = store
        self.persist_raw = persist_raw
        self.stale_ms = stale_ms if stale_ms is not None else _env_int("KALSHI_WS_STALE_MS", 3000)
        self.channels = [c for c in (channels or list(READONLY_CHANNELS))
                         if c not in FORBIDDEN_CHANNELS]
        # state
        self.books: dict[str, KalshiBinaryOrderbook] = {}
        self.bbo: dict[str, dict] = {}            # ticker -> {yes_bid, yes_ask, no_bid, no_ask}
        self.lifecycle: dict[str, object] = {}     # ticker -> MarketLifecycleStatus
        self.trades: list[dict] = []
        self.subscribed: set[str] = set()
        # status + counters
        self.status = "disabled"
        self.last_message_ts_ms: Optional[int] = None
        self.messages_received = 0
        self.parse_errors = 0
        self.reconnect_count = 0
        self.stale_count = 0
        self.seq_gap_count = 0
        self.resnapshot_count = 0

    # -- assertions ----------------------------------------------------- #
    def _assert_readonly(self, channel: str) -> bool:
        if channel in FORBIDDEN_CHANNELS:
            logger.warning("kalshi: refusing forbidden channel %s", channel)
            return False
        return True

    # -- message processing (testable; no network) --------------------- #
    def process_message(self, msg: dict) -> None:
        try:
            self.messages_received += 1
            self.last_message_ts_ms = _now_ms()
            mtype = (msg.get("type") or msg.get("channel") or "").lower()
            payload = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
            if mtype in ("orderbook_snapshot",):
                self._on_snapshot(payload)
            elif mtype in ("orderbook_delta",):
                self._on_delta(payload)
            elif mtype in ("ticker", "ticker_v2"):
                self._on_ticker(payload)
            elif mtype in ("trade",):
                self._on_trade(payload)
            elif mtype in ("market_lifecycle_v2", "event_lifecycle", "market_lifecycle"):
                self._on_lifecycle(payload)
            # else: ignore unknown types silently (do not crash)
            self._persist_raw(mtype or "unknown", payload)
        except Exception:  # noqa: BLE001 — malformed msg must never crash the loop
            self.parse_errors += 1

    def _book(self, ticker: str) -> KalshiBinaryOrderbook:
        b = self.books.get(ticker)
        if b is None:
            b = KalshiBinaryOrderbook(ticker)
            self.books[ticker] = b
        return b

    def _on_snapshot(self, p: dict) -> None:
        ticker = p.get("market_ticker") or p.get("ticker")
        if not ticker:
            raise ValueError("snapshot without ticker")
        book = self._book(ticker)
        was_gap = book.needs_snapshot
        yes = [(Decimal(str(x[0])) / 100, Decimal(str(x[1]))) for x in (p.get("yes") or [])]
        no = [(Decimal(str(x[0])) / 100, Decimal(str(x[1]))) for x in (p.get("no") or [])]
        book.apply_snapshot(yes, no, seq=p.get("seq"))
        if was_gap:
            self.resnapshot_count += 1

    def _on_delta(self, p: dict) -> None:
        ticker = p.get("market_ticker") or p.get("ticker")
        if not ticker:
            raise ValueError("delta without ticker")
        book = self._book(ticker)
        before = book.gap_detected
        side = p.get("side", "yes")
        price = Decimal(str(p.get("price"))) / 100
        delta = Decimal(str(p.get("delta")))
        book.apply_delta(side, price, delta, seq=p.get("seq"))
        if book.gap_detected and not before:
            self.seq_gap_count += 1

    def _on_ticker(self, p: dict) -> None:
        ticker = p.get("market_ticker") or p.get("ticker")
        if not ticker:
            return
        def c(v):
            return None if v is None else Decimal(str(v)) / 100
        self.bbo[ticker] = {"yes_bid": c(p.get("yes_bid")), "yes_ask": c(p.get("yes_ask")),
                            "no_bid": c(p.get("no_bid")), "no_ask": c(p.get("no_ask")),
                            "ts_ms": _now_ms()}

    def _on_trade(self, p: dict) -> None:
        ticker = p.get("market_ticker") or p.get("ticker")
        price = p.get("yes_price", p.get("price"))
        self.trades.append({
            "venue": "kalshi", "market_ticker": ticker,
            "price": (Decimal(str(price)) / 100) if price is not None else None,
            "size": p.get("count", p.get("size")), "side": p.get("taker_side") or p.get("side"),
            "ts_ms": _now_ms()})
        self.trades = self.trades[-500:]

    def _on_lifecycle(self, p: dict) -> None:
        status = parse_lifecycle(p)
        if status.market_ticker:
            self.lifecycle[status.market_ticker] = status
        parse_resolution(p)  # normalized; persisted via raw + lifecycle tables upstream

    def _persist_raw(self, event_type: str, payload: dict) -> None:
        if not (self.store and self.persist_raw):
            return
        try:
            self.store.append_raw_market_event(
                ts_ms=_now_ms(), source="kalshi_ws", venue="kalshi",
                event_type=event_type,
                market_id=payload.get("market_ticker") or payload.get("ticker"),
                asset_id=None, payload=payload)
        except Exception:  # noqa: BLE001
            pass

    # -- views ---------------------------------------------------------- #
    def get_orderbook(self, ticker: str, outcome: str = "YES"):
        book = self.books.get(ticker)
        if book is None:
            return None
        stale = (_now_ms() - book.last_update_ms) > self.stale_ms
        return book.normalized(outcome, stale=stale)

    def get_bbo(self, ticker: str, outcome: str = "YES") -> Optional[BBO]:
        outcome = outcome.upper()
        b = self.bbo.get(ticker)
        if b is None:
            nb = self.get_orderbook(ticker, outcome)
            if nb is None:
                return None
            return BBO(venue="kalshi", market_ticker=ticker, outcome=outcome,
                       best_bid=nb.best_bid, best_ask=nb.best_ask, midpoint=nb.midpoint,
                       spread=nb.spread)
        if outcome == "YES":
            return BBO(venue="kalshi", market_ticker=ticker, outcome="YES",
                       best_bid=b["yes_bid"], best_ask=b["yes_ask"])
        return BBO(venue="kalshi", market_ticker=ticker, outcome="NO",
                   best_bid=b["no_bid"], best_ask=b["no_ask"])

    def status_snapshot(self) -> MarketDataStatus:
        stale = sum(1 for b in self.books.values()
                    if (_now_ms() - b.last_update_ms) > self.stale_ms)
        return MarketDataStatus(
            venue="kalshi", status=self.status, last_message_ts_ms=self.last_message_ts_ms,
            messages_received=self.messages_received, parse_errors=self.parse_errors,
            reconnect_count=self.reconnect_count, subscribed_count=len(self.subscribed),
            stale_count=stale, seq_gap_count=self.seq_gap_count,
            resnapshot_count=self.resnapshot_count)

    # -- subscription messages (read-only) ------------------------------ #
    def subscribe_message(self, channel: str, market_tickers: list[str], msg_id: int = 1) -> dict:
        assert self._assert_readonly(channel)
        params: dict = {"channels": [channel]}
        if channel != "market_lifecycle_v2":  # lifecycle does not support ticker filters
            params["market_tickers"] = market_tickers
        return {"id": msg_id, "cmd": "subscribe", "params": params}

    def get_snapshot_message(self, market_tickers: list[str], msg_id: int = 99) -> dict:
        """Request a fresh snapshot (resync) for the orderbook channel."""
        return {"id": msg_id, "cmd": "update_subscription",
                "params": {"channels": ["orderbook_delta"], "market_tickers": market_tickers,
                           "action": "get_snapshot"}}

    def __repr__(self) -> str:
        return redact(f"<KalshiWSClient status={self.status} subs={len(self.subscribed)}>")
