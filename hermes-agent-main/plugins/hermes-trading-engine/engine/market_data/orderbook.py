"""Normalized order book state for a single CLOB asset (token).

Prices and sizes are kept as :class:`decimal.Decimal` internally — never float —
so level math stays exact. The state is fed by Polymarket CLOB events:

  * ``book``            -> full snapshot replace (clears tick-size-dirty flag)
  * ``price_change``    -> per-level deltas (size "0" removes the level)
  * ``best_bid_ask``    -> direct BBO update
  * ``tick_size_change``-> marks state risk-dirty until the next book snapshot

The state tracks freshness (``last_update_ms``), a ``resolved`` flag, a
``tick_size_dirty`` flag, and an ``unreliable`` flag (deltas applied with no
base snapshot), all of which the RiskEngine consults.

Quant scope — *Data Acquisition & Ingestion* + *Execution Engine CLOB v2
simulation*: the freshness, tick-size-dirty, and BBO/depth state surfaced here
are exactly the executability + stale-book signals the Bregman-arbitrage
certifier requires per leg before any opportunity can be certified.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..schemas import BBO, OrderbookLevel, OrderbookStateSnapshot


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_decimal(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _level_pair(level) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Accept {'price','size'} dicts or [price, size] pairs."""
    if isinstance(level, dict):
        return _to_decimal(level.get("price")), _to_decimal(level.get("size"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _to_decimal(level[0]), _to_decimal(level[1])
    return None, None


def _side_is_bid(side) -> Optional[bool]:
    s = str(side or "").strip().lower()
    if s in ("buy", "bid", "b", "yes"):
        return True
    if s in ("sell", "ask", "a", "no"):
        return False
    return None


class OrderbookState:
    def __init__(self, asset_id: str, market_id: str = "", venue: str = "polymarket"):
        self.asset_id = asset_id
        self.market_id = market_id
        self.venue = venue
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.tick_size: Optional[Decimal] = None
        self.last_update_ms: int = 0
        self.sequence: Optional[str] = None
        self.resolved: bool = False
        self.tick_size_dirty: bool = False
        self.unreliable: bool = False
        self.has_book: bool = False
        # last trade print
        self.last_trade_price: Optional[Decimal] = None
        self.last_trade_size: Optional[Decimal] = None
        self.last_trade_side: Optional[str] = None
        self.last_trade_ms: int = 0

    # ------------------------------------------------------------------ #
    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def midpoint(self) -> Optional[Decimal]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / Decimal(2)
        return self.best_ask if self.best_ask is not None else self.best_bid

    @property
    def spread_pct(self) -> Optional[Decimal]:
        mid = self.midpoint
        sp = self.spread
        if mid and sp is not None and mid != 0:
            return sp / mid
        return None

    def imbalance(self) -> Optional[float]:
        """Order-book imbalance in ``[-1, 1]`` using total resting size per side.

        ``+1`` = all size on the bid (buy pressure), ``-1`` = all on the ask.
        Returns ``None`` when neither side has sized levels.
        """
        bid_sz = float(sum(self.bids.values())) if self.bids else 0.0
        ask_sz = float(sum(self.asks.values())) if self.asks else 0.0
        denom = bid_sz + ask_sz
        if denom <= 0:
            return None
        return (bid_sz - ask_sz) / denom

    def microprice(self) -> Optional[Decimal]:
        """Depth-weighted microprice: the BBO weighted toward the side with more
        resting size. Returns ``None`` without a two-sided sized book."""
        if self.best_bid is None or self.best_ask is None:
            return None
        bid_sz = sum(self.bids.values()) if self.bids else Decimal(0)
        ask_sz = sum(self.asks.values()) if self.asks else Decimal(0)
        denom = bid_sz + ask_sz
        if denom <= 0:
            return self.midpoint
        return (self.best_bid * ask_sz + self.best_ask * bid_sz) / denom

    def is_stale(self, max_age_ms: int) -> bool:
        if self.last_update_ms <= 0:
            return True
        return (_now_ms() - self.last_update_ms) > max_age_ms

    def age_ms(self) -> Optional[int]:
        if self.last_update_ms <= 0:
            return None
        return _now_ms() - self.last_update_ms

    # ------------------------------------------------------------------ #
    def _recompute_best(self) -> None:
        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None

    def _touch(self, ts_ms: Optional[int] = None, sequence: Optional[str] = None) -> None:
        self.last_update_ms = int(ts_ms) if ts_ms else _now_ms()
        if sequence is not None:
            self.sequence = str(sequence)

    # ------------------------------------------------------------------ #
    def apply_book_event(self, bids, asks, *, ts_ms=None, sequence=None,
                         tick_size=None) -> None:
        """Full snapshot replace. Acknowledges any pending tick-size change."""
        self.bids = {}
        self.asks = {}
        for lvl in bids or []:
            p, s = _level_pair(lvl)
            if p is not None and s is not None and s > 0:
                self.bids[p] = s
        for lvl in asks or []:
            p, s = _level_pair(lvl)
            if p is not None and s is not None and s > 0:
                self.asks[p] = s
        ts = _to_decimal(tick_size)
        if ts is not None:
            self.tick_size = ts
        self._recompute_best()
        self.has_book = True
        self.unreliable = False
        self.tick_size_dirty = False  # a fresh snapshot acknowledges the change
        self._touch(ts_ms, sequence)

    def apply_price_change(self, changes: list, *, ts_ms=None, sequence=None) -> list[dict]:
        """Apply per-level deltas. size '0' removes the level.

        Returns a list of normalized delta dicts for persistence.
        """
        deltas: list[dict] = []
        if not self.has_book:
            # deltas with no base snapshot -> we cannot trust the book
            self.unreliable = True
        for item in changes or []:
            if not isinstance(item, dict):
                continue
            is_bid = _side_is_bid(item.get("side"))
            price = _to_decimal(item.get("price"))
            size = _to_decimal(item.get("size"))
            if is_bid is None or price is None or size is None:
                continue
            book = self.bids if is_bid else self.asks
            action = "remove" if size == 0 else "set"
            if size == 0:
                book.pop(price, None)
            else:
                book[price] = size
            # optional embedded BBO hints
            bb = _to_decimal(item.get("best_bid"))
            ba = _to_decimal(item.get("best_ask"))
            if bb is not None:
                self.best_bid = bb
            if ba is not None:
                self.best_ask = ba
            deltas.append({
                "side": "BUY" if is_bid else "SELL",
                "price": str(price), "size": str(size), "action": action,
            })
        # recompute from the book unless an explicit BBO override was supplied
        self._recompute_best()
        self._touch(ts_ms, sequence)
        return deltas

    def apply_best_bid_ask(self, best_bid=None, best_ask=None, *, ts_ms=None,
                           sequence=None) -> None:
        bb = _to_decimal(best_bid)
        ba = _to_decimal(best_ask)
        if bb is not None:
            self.best_bid = bb
        if ba is not None:
            self.best_ask = ba
        self._touch(ts_ms, sequence)

    def apply_tick_size_change(self, new_tick_size, *, ts_ms=None) -> Optional[Decimal]:
        """Mark the book risk-dirty until the next full snapshot refreshes it."""
        old = self.tick_size
        ts = _to_decimal(new_tick_size)
        if ts is not None:
            self.tick_size = ts
        self.tick_size_dirty = True
        self._touch(ts_ms)
        return old

    def apply_last_trade(self, price=None, size=None, side=None, *, ts_ms=None) -> None:
        self.last_trade_price = _to_decimal(price)
        self.last_trade_size = _to_decimal(size)
        self.last_trade_side = str(side) if side is not None else None
        self.last_trade_ms = int(ts_ms) if ts_ms else _now_ms()

    def mark_resolved(self) -> None:
        self.resolved = True
        self._touch()

    # ------------------------------------------------------------------ #
    def bbo(self) -> Optional[BBO]:
        if self.best_bid is None and self.best_ask is None:
            return None
        return BBO(
            symbol=self.asset_id, venue=self.venue,
            bid=float(self.best_bid) if self.best_bid is not None else 0.0,
            ask=float(self.best_ask) if self.best_ask is not None else 0.0,
            bid_size=float(self.bids.get(self.best_bid, 0)) if self.best_bid is not None else 0.0,
            ask_size=float(self.asks.get(self.best_ask, 0)) if self.best_ask is not None else 0.0,
            ts=self.last_update_ms / 1000.0 if self.last_update_ms else 0.0,
        )

    def to_snapshot(self, depth: int = 50) -> OrderbookStateSnapshot:
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        return OrderbookStateSnapshot(
            asset_id=self.asset_id, market_id=self.market_id, venue=self.venue,
            bids=[OrderbookLevel(price=str(p), size=str(s)) for p, s in bids],
            asks=[OrderbookLevel(price=str(p), size=str(s)) for p, s in asks],
            best_bid=str(self.best_bid) if self.best_bid is not None else None,
            best_ask=str(self.best_ask) if self.best_ask is not None else None,
            spread=str(self.spread) if self.spread is not None else None,
            midpoint=str(self.midpoint) if self.midpoint is not None else None,
            tick_size=str(self.tick_size) if self.tick_size is not None else None,
            last_update_ms=self.last_update_ms, sequence=self.sequence,
            resolved=self.resolved, tick_size_dirty=self.tick_size_dirty)
