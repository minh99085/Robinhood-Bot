"""Polymarket CLOB market-data WebSocket client (READ-ONLY).

Connects to the public market channel, subscribes to asset (token) ids, and
maintains normalized :class:`OrderbookState` per asset. It NEVER authenticates,
signs, or submits anything — it only consumes public market data.

Endpoint (configurable via POLYMARKET_WS_URL):
    wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscription payload:
    {"assets_ids": [...], "type": "market", "custom_feature_enabled": true}

Robustness:
  * auto-reconnect with exponential backoff
  * malformed messages increment ``parse_errors`` and never escape
  * every raw message is persisted before it is transformed
  * connection status: disconnected / connecting / connected / reconnecting / degraded

The async client is hosted in a background thread by :class:`MarketDataManager`
so it never blocks the main trading loop or the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Optional

from ..schemas import BBO
from .base import (
    CONN_CONNECTED,
    CONN_CONNECTING,
    CONN_DEGRADED,
    CONN_DISCONNECTED,
    CONN_RECONNECTING,
    MarketDataAdapter,
)
from .orderbook import OrderbookState

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

_EVENT_BOOK = "book"
_EVENT_PRICE_CHANGE = "price_change"
_EVENT_TICK_SIZE = "tick_size_change"
_EVENT_LAST_TRADE = "last_trade_price"
_EVENT_BEST_BID_ASK = "best_bid_ask"
_EVENT_NEW_MARKET = "new_market"
_EVENT_RESOLVED = "market_resolved"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _coerce_ts(v) -> Optional[int]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Polymarket timestamps may be seconds or milliseconds.
    return int(f if f > 1e12 else f * 1000)


class PolymarketWSClient(MarketDataAdapter):
    def __init__(self, *, event_store=None, url: Optional[str] = None,
                 stale_ms: int = 3000, persist_raw: bool = True,
                 recv_timeout: float = 1.0, backoff_base: float = 1.0,
                 backoff_max: float = 30.0):
        self.url = url or DEFAULT_WS_URL
        self.event_store = event_store
        self.stale_ms = stale_ms
        self.persist_raw = persist_raw
        self.recv_timeout = recv_timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

        self._lock = threading.Lock()
        self._books: dict[str, OrderbookState] = {}
        self._desired: set[str] = set()
        self._resolved_markets: set[str] = set()
        self._recent_markets: list[dict] = []

        self.status: str = CONN_DISCONNECTED
        self.last_message_ms: int = 0
        self.messages_received: int = 0
        self.parse_errors: int = 0
        self.reconnect_count: int = 0
        self.last_error: Optional[str] = None
        self.last_warning: Optional[str] = None

        self._stop = False
        self._resub_needed = False
        self._ws = None

    # ================================================================== #
    # subscription
    # ================================================================== #
    @staticmethod
    def _subscription_payload(asset_ids: list[str]) -> dict:
        return {
            "assets_ids": list(asset_ids),
            "type": "market",
            "custom_feature_enabled": True,
        }

    def set_desired_assets(self, asset_ids) -> None:
        """Thread-safe: declare the full desired subscription set."""
        with self._lock:
            new = {str(a) for a in (asset_ids or []) if a}
            if new != self._desired:
                self._desired = new
                self._resub_needed = True

    async def subscribe(self, asset_ids: list[str]) -> None:
        with self._lock:
            self._desired |= {str(a) for a in asset_ids if a}
            self._resub_needed = True

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        with self._lock:
            self._desired -= {str(a) for a in asset_ids if a}
            self._resub_needed = True

    # ================================================================== #
    # raw message handling (sync; unit-testable without a socket)
    # ================================================================== #
    def handle_raw_message(self, text: str) -> None:
        """Parse + dispatch one inbound message. Never raises."""
        self.messages_received += 1
        self.last_message_ms = _now_ms()
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            self.parse_errors += 1
            self._persist_raw("__unparsed__", None, None, {"raw": str(text)[:1000]})
            return
        events = data if isinstance(data, list) else [data]
        for ev in events:
            try:
                self._dispatch_event(ev)
            except Exception as exc:  # noqa: BLE001 — one bad event must not kill the feed
                self.parse_errors += 1
                self.last_error = f"dispatch: {str(exc)[:140]}"

    def _persist_raw(self, event_type, market_id, asset_id, payload) -> None:
        if not (self.persist_raw and self.event_store):
            return
        try:
            self.event_store.append_raw_event(
                "polymarket_clob", event_type, market_id, asset_id, payload)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    def _book(self, asset_id: str, market_id: str = "") -> OrderbookState:
        st = self._books.get(asset_id)
        if st is None:
            st = OrderbookState(asset_id=asset_id, market_id=market_id)
            self._books[asset_id] = st
        elif market_id and not st.market_id:
            st.market_id = market_id
        return st

    def _dispatch_event(self, ev: dict) -> None:
        if not isinstance(ev, dict):
            raise ValueError("event is not an object")
        et = str(ev.get("event_type") or ev.get("type") or "").strip()
        asset_id = ev.get("asset_id") or ev.get("asset") or None
        market_id = ev.get("market") or ev.get("market_id") or ev.get("condition_id") or None
        ts_ms = _coerce_ts(ev.get("timestamp")) or _now_ms()
        seq = ev.get("hash") or ev.get("seq") or ev.get("sequence")

        # Persist the raw event BEFORE transforming it (audit trail).
        self._persist_raw(et or "unknown", market_id, asset_id, ev)

        if et == _EVENT_BOOK and asset_id:
            self._on_book(asset_id, market_id, ev, ts_ms, seq)
        elif et == _EVENT_PRICE_CHANGE and asset_id:
            self._on_price_change(asset_id, market_id, ev, ts_ms, seq)
        elif et == _EVENT_BEST_BID_ASK and asset_id:
            self._on_best_bid_ask(asset_id, market_id, ev, ts_ms, seq)
        elif et == _EVENT_TICK_SIZE and asset_id:
            self._on_tick_size_change(asset_id, market_id, ev, ts_ms)
        elif et == _EVENT_LAST_TRADE and asset_id:
            self._on_last_trade(asset_id, market_id, ev, ts_ms)
        elif et == _EVENT_NEW_MARKET:
            self._on_new_market(market_id, ev, ts_ms)
        elif et == _EVENT_RESOLVED:
            self._on_market_resolved(market_id, asset_id, ev, ts_ms)
        # unknown event types are persisted (above) but otherwise ignored

    # ------------------------------------------------------------------ #
    def _on_book(self, asset_id, market_id, ev, ts_ms, seq) -> None:
        bids = ev.get("bids") or ev.get("buys") or []
        asks = ev.get("asks") or ev.get("sells") or []
        tick = ev.get("tick_size") or ev.get("minimum_tick_size")
        with self._lock:
            st = self._book(asset_id, market_id or "")
            st.apply_book_event(bids, asks, ts_ms=ts_ms, sequence=seq, tick_size=tick)
            snap = st.to_snapshot()
        if self.event_store:
            try:
                self.event_store.append_orderbook_snapshot(
                    venue="polymarket", market_id=snap.market_id, asset_id=asset_id,
                    bids=[(lvl.price, lvl.size) for lvl in snap.bids],
                    asks=[(lvl.price, lvl.size) for lvl in snap.asks],
                    best_bid=snap.best_bid, best_ask=snap.best_ask,
                    spread=snap.spread, midpoint=snap.midpoint, tick_size=snap.tick_size,
                    ts_ms=ts_ms)
            except Exception:  # noqa: BLE001
                pass

    def _on_price_change(self, asset_id, market_id, ev, ts_ms, seq) -> None:
        changes = ev.get("price_changes") or ev.get("changes") or []
        with self._lock:
            st = self._book(asset_id, market_id or "")
            deltas = st.apply_price_change(changes, ts_ms=ts_ms, sequence=seq)
            bb = str(st.best_bid) if st.best_bid is not None else None
            ba = str(st.best_ask) if st.best_ask is not None else None
        if self.event_store:
            for d in deltas:
                try:
                    self.event_store.append_orderbook_delta(
                        venue="polymarket", market_id=market_id or "", asset_id=asset_id,
                        side=d["side"], price=d["price"], size=d["size"],
                        action=d["action"], best_bid=bb, best_ask=ba, ts_ms=ts_ms)
                except Exception:  # noqa: BLE001
                    pass

    def _on_best_bid_ask(self, asset_id, market_id, ev, ts_ms, seq) -> None:
        bb = ev.get("best_bid") if ev.get("best_bid") is not None else ev.get("bid")
        ba = ev.get("best_ask") if ev.get("best_ask") is not None else ev.get("ask")
        with self._lock:
            st = self._book(asset_id, market_id or "")
            had_book = st.has_book
            book_bid, book_ask = st.best_bid, st.best_ask
            tick = st.tick_size
            st.apply_best_bid_ask(bb, ba, ts_ms=ts_ms, sequence=seq)
            new_bid, new_ask = st.best_bid, st.best_ask
        # consistency cross-check against the local book (warn only)
        if had_book and tick:
            try:
                if book_bid is not None and new_bid is not None and abs(new_bid - book_bid) > tick:
                    self.last_warning = f"{asset_id}: best_bid_ask bid diverges from book by >1 tick"
                if book_ask is not None and new_ask is not None and abs(new_ask - book_ask) > tick:
                    self.last_warning = f"{asset_id}: best_bid_ask ask diverges from book by >1 tick"
            except Exception:  # noqa: BLE001
                pass
        self._persist_market_event(market_id, asset_id, _EVENT_BEST_BID_ASK, ev, ts_ms)

    def _on_tick_size_change(self, asset_id, market_id, ev, ts_ms) -> None:
        new_tick = ev.get("new_tick_size") or ev.get("tick_size") or ev.get("minimum_tick_size")
        with self._lock:
            st = self._book(asset_id, market_id or "")
            st.apply_tick_size_change(new_tick, ts_ms=ts_ms)
        self._persist_market_event(market_id, asset_id, _EVENT_TICK_SIZE, ev, ts_ms)

    def _on_last_trade(self, asset_id, market_id, ev, ts_ms) -> None:
        with self._lock:
            st = self._book(asset_id, market_id or "")
            st.apply_last_trade(ev.get("price"), ev.get("size"), ev.get("side"), ts_ms=ts_ms)
        self._persist_market_event(market_id, asset_id, _EVENT_LAST_TRADE, ev, ts_ms)

    def _on_new_market(self, market_id, ev, ts_ms) -> None:
        with self._lock:
            self._recent_markets.append({"market_id": market_id, "ts_ms": ts_ms,
                                         "question": ev.get("question") or ev.get("title")})
            self._recent_markets = self._recent_markets[-50:]
        self._persist_market_event(market_id, None, _EVENT_NEW_MARKET, ev, ts_ms)

    def _on_market_resolved(self, market_id, asset_id, ev, ts_ms) -> None:
        with self._lock:
            if market_id:
                self._resolved_markets.add(str(market_id))
            if asset_id:
                self._book(asset_id, market_id or "").mark_resolved()
            else:
                for st in self._books.values():
                    if market_id and st.market_id == market_id:
                        st.mark_resolved()
        self._persist_market_event(market_id, asset_id, _EVENT_RESOLVED, ev, ts_ms)

    def _persist_market_event(self, market_id, asset_id, event_type, payload, ts_ms) -> None:
        if not self.event_store:
            return
        try:
            self.event_store.append_market_event(
                venue="polymarket", market_id=market_id or "", asset_id=asset_id,
                event_type=event_type, payload=payload, ts_ms=ts_ms)
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== #
    # read-only accessors (thread-safe)
    # ================================================================== #
    def get_orderbook(self, asset_id: str) -> Optional[OrderbookState]:
        with self._lock:
            return self._books.get(asset_id)

    def get_bbo(self, asset_id: str) -> Optional[BBO]:
        with self._lock:
            st = self._books.get(asset_id)
            return st.bbo() if st else None

    def _stale_asset_count(self) -> int:
        return sum(1 for st in self._books.values() if st.is_stale(self.stale_ms))

    def get_status(self) -> dict:
        with self._lock:
            age = (_now_ms() - self.last_message_ms) if self.last_message_ms else None
            return {
                "source": "polymarket_clob",
                "status": self.status,
                "url": self.url,
                "last_message_ms": self.last_message_ms,
                "last_message_age_ms": age,
                "messages_received": self.messages_received,
                "parse_errors": self.parse_errors,
                "reconnect_count": self.reconnect_count,
                "subscribed_asset_count": len(self._desired),
                "tracked_asset_count": len(self._books),
                "stale_asset_count": self._stale_asset_count(),
                "resolved_market_count": len(self._resolved_markets),
                "last_error": self.last_error,
                "last_warning": self.last_warning,
            }

    def health(self) -> dict:
        status = self.get_status()
        with self._lock:
            assets = []
            for aid, st in list(self._books.items())[:50]:
                assets.append({
                    "asset_id": aid, "market_id": st.market_id,
                    "best_bid": str(st.best_bid) if st.best_bid is not None else None,
                    "best_ask": str(st.best_ask) if st.best_ask is not None else None,
                    "spread": str(st.spread) if st.spread is not None else None,
                    "age_ms": st.age_ms(), "stale": st.is_stale(self.stale_ms),
                    "tick_size_dirty": st.tick_size_dirty, "resolved": st.resolved,
                })
            recent = list(self._recent_markets)[-10:]
        return {"status": status, "assets": assets, "recent_markets": recent}

    def freshness_for_risk(self, asset_id: str, max_spread: Optional[float] = None) -> dict:
        """Return the market-data fields the RiskEngine gates on for an asset."""
        with self._lock:
            st = self._books.get(asset_id)
            unhealthy = self.status != CONN_CONNECTED
            if st is None:
                return {"required": True, "status": self.status, "bbo_present": False,
                        "stale": True, "resolved": False, "tick_size_dirty": False,
                        "unreliable": True, "spread": None}
            spread = None
            sp = st.spread_pct
            if sp is not None:
                spread = float(sp)
            return {
                "required": True, "status": self.status,
                "bbo_present": st.best_bid is not None and st.best_ask is not None,
                "stale": st.is_stale(self.stale_ms),
                "resolved": st.resolved or (st.market_id in self._resolved_markets),
                "tick_size_dirty": st.tick_size_dirty,
                "unreliable": st.unreliable, "spread": spread,
            }

    # ================================================================== #
    # async run loop (hosted by MarketDataManager in a background thread)
    # ================================================================== #
    def request_stop(self) -> None:
        self._stop = True

    async def start(self) -> None:
        await self.run_forever()

    async def stop(self) -> None:
        self.request_stop()

    def _set_status(self, status: str) -> None:
        self.status = status
        self._publish_health()

    def _publish_health(self) -> None:
        if not self.event_store:
            return
        s = self.get_status()
        try:
            self.event_store.update_health(
                source="polymarket_clob", status=s["status"],
                last_message_ts_ms=s["last_message_ms"], reconnect_count=s["reconnect_count"],
                parse_errors=s["parse_errors"], subscribed_asset_count=s["subscribed_asset_count"],
                stale_asset_count=s["stale_asset_count"])
        except Exception:  # noqa: BLE001
            pass

    def _check_liveness(self) -> None:
        if self.status == CONN_CONNECTED and self.last_message_ms:
            degraded_after = max(self.stale_ms * 3, 10000)
            if (_now_ms() - self.last_message_ms) > degraded_after:
                self._set_status(CONN_DEGRADED)

    async def _send_subscription(self, ws) -> None:
        with self._lock:
            assets = sorted(self._desired)
        if not assets:
            return
        await ws.send(json.dumps(self._subscription_payload(assets)))

    async def run_forever(self) -> None:
        try:
            import websockets
        except Exception:  # noqa: BLE001
            self.last_error = "websockets package not available"
            self._set_status(CONN_DISCONNECTED)
            return
        backoff = self.backoff_base
        while not self._stop:
            self._set_status(CONN_CONNECTING)
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, close_timeout=5,
                    max_size=2 ** 22,
                ) as ws:
                    self._ws = ws
                    self._set_status(CONN_CONNECTED)
                    backoff = self.backoff_base
                    await self._send_subscription(ws)
                    self._resub_needed = False
                    while not self._stop:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                        except asyncio.TimeoutError:
                            if self._resub_needed:
                                await self._send_subscription(ws)
                                self._resub_needed = False
                            self._check_liveness()
                            continue
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", "replace")
                        self.handle_raw_message(msg)
                        if self.status != CONN_CONNECTED:
                            self._set_status(CONN_CONNECTED)
                        if self._resub_needed:
                            await self._send_subscription(ws)
                            self._resub_needed = False
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — reconnect on any failure
                self.last_error = f"ws: {str(exc)[:140]}"
            self._ws = None
            if self._stop:
                break
            self.reconnect_count += 1
            self._set_status(CONN_RECONNECTING)
            await asyncio.sleep(backoff)
            backoff = min(self.backoff_max, backoff * 2)
        self._set_status(CONN_DISCONNECTED)


class MarketDataManager:
    """Hosts a PolymarketWSClient in a background thread (non-blocking).

    Thread-safe facade used by the trading engine + dashboard. Read-only.
    """

    def __init__(self, *, event_store=None, url: Optional[str] = None,
                 stale_ms: int = 3000, persist_raw: bool = True,
                 max_assets: int = 20):
        self.client = PolymarketWSClient(
            event_store=event_store, url=url, stale_ms=stale_ms, persist_raw=persist_raw)
        self.max_assets = max_assets
        self.stale_ms = stale_ms
        self._thread: Optional[threading.Thread] = None
        self._market_assets: dict[str, list[str]] = {}  # gamma market_id -> [token_ids]
        self._asset_market: dict[str, str] = {}          # token_id -> gamma market_id

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hte-md-clob", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self.client.run_forever())
        except Exception:  # noqa: BLE001 — never crash the host process
            pass

    def stop(self) -> None:
        self.client.request_stop()

    # ------------------------------------------------------------------ #
    def ensure_subscribed(self, market_assets: dict[str, list[str]]) -> None:
        """Declare the desired subscription set from {market_id: [token_ids]}.

        Capped at ``max_assets`` token ids total. Read-only subscription.
        """
        asset_market: dict[str, str] = {}
        flat: list[str] = []
        for mid, tokens in (market_assets or {}).items():
            for t in tokens or []:
                if t and t not in asset_market:
                    asset_market[t] = mid
                    flat.append(t)
        flat = flat[: self.max_assets]
        keep = set(flat)
        self._market_assets = {m: [t for t in ts if t in keep]
                               for m, ts in (market_assets or {}).items()}
        self._asset_market = {t: m for t, m in asset_market.items() if t in keep}
        self.client.set_desired_assets(flat)

    def asset_for_market(self, market_id: str) -> Optional[str]:
        toks = self._market_assets.get(market_id) or []
        return toks[0] if toks else None

    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        return self.client.get_status()

    def health(self) -> dict:
        h = self.client.health()
        h["enabled"] = True
        return h

    def get_bbo(self, asset_id: str):
        return self.client.get_bbo(asset_id)

    def get_orderbook(self, asset_id: str):
        return self.client.get_orderbook(asset_id)

    def freshness_for_market(self, market_id: str, max_spread: Optional[float] = None) -> Optional[dict]:
        asset_id = self.asset_for_market(market_id)
        if not asset_id:
            return None
        return self.client.freshness_for_risk(asset_id, max_spread=max_spread)
