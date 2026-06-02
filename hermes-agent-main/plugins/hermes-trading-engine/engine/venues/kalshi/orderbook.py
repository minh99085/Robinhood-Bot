"""Kalshi binary YES/NO orderbook normalization (Phase 6).

Kalshi books are quoted as YES bids and NO bids (in dollars/probability, [0,1]).
The opposite-side ask is derived by binary complement:

  yes_ask_price = 1 - no_bid_price   (yes_ask_size = no_bid_size)
  no_ask_price  = 1 - yes_bid_price   (no_ask_size  = yes_bid_size)

We track ``seq`` per market_ticker, detect sequence gaps (which require a fresh
snapshot before the book may be trusted again), and flag crossed/invalid books.
All math uses Decimal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from ..metadata import NormalizedBinaryOrderbook, OrderbookLevel, _now_ms

ZERO = Decimal("0")
ONE = Decimal("1")


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


class KalshiBinaryOrderbook:
    def __init__(self, market_ticker: str, market_id: Optional[str] = None):
        self.market_ticker = market_ticker
        self.market_id = market_id
        self.yes_bids: dict[Decimal, Decimal] = {}
        self.no_bids: dict[Decimal, Decimal] = {}
        self.seq: Optional[int] = None
        self.gap_detected = False
        self.needs_snapshot = False
        self.last_update_ms = _now_ms()
        self._has_snapshot = False

    # -- mutation ------------------------------------------------------- #
    def apply_snapshot(self, yes_bids: Iterable, no_bids: Iterable,
                       seq: Optional[int] = None, ts_ms: Optional[int] = None) -> None:
        self.yes_bids = {}
        self.no_bids = {}
        for price, size in yes_bids or []:
            self._set("yes", _d(price), _d(size))
        for price, size in no_bids or []:
            self._set("no", _d(price), _d(size))
        self.seq = seq
        self.gap_detected = False
        self.needs_snapshot = False
        self._has_snapshot = True
        self.last_update_ms = ts_ms or _now_ms()

    def apply_delta(self, side: str, price, delta, seq: Optional[int] = None,
                    ts_ms: Optional[int] = None) -> None:
        side = side.lower()
        # Sequence-gap detection: a delta must be exactly prev_seq + 1.
        if seq is not None and self.seq is not None and seq != self.seq + 1:
            self.gap_detected = True
            self.needs_snapshot = True
        if seq is not None:
            self.seq = seq
        book = self.yes_bids if side == "yes" else self.no_bids
        p = _d(price)
        new_size = book.get(p, ZERO) + _d(delta)
        if new_size <= ZERO:
            book.pop(p, None)
        else:
            book[p] = new_size
        self.last_update_ms = ts_ms or _now_ms()

    def _set(self, side: str, price: Decimal, size: Decimal) -> None:
        book = self.yes_bids if side == "yes" else self.no_bids
        if size <= ZERO:
            book.pop(price, None)
        else:
            book[price] = size

    # -- views ---------------------------------------------------------- #
    def normalized(self, outcome: str = "YES", stale: bool = False) -> NormalizedBinaryOrderbook:
        outcome = outcome.upper()
        if outcome == "YES":
            bid_src, ask_src = self.yes_bids, self.no_bids
        else:
            bid_src, ask_src = self.no_bids, self.yes_bids

        bids = [OrderbookLevel(price=p, size=s) for p, s in
                sorted(bid_src.items(), key=lambda kv: kv[0], reverse=True)]
        # derived asks: complement price, same size, sorted ascending
        asks = [OrderbookLevel(price=(ONE - p), size=s) for p, s in
                sorted(ask_src.items(), key=lambda kv: (ONE - kv[0]))]

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        midpoint = ((best_bid + best_ask) / 2) if (best_bid is not None and best_ask is not None) else None

        valid = True
        crossed = False
        for lvl in bids + asks:
            if lvl.price < ZERO or lvl.price > ONE:
                valid = False
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            crossed = True
            valid = False

        return NormalizedBinaryOrderbook(
            venue="kalshi", market_id=self.market_id, market_ticker=self.market_ticker,
            outcome=outcome, bids=bids, asks=asks, best_bid=best_bid, best_ask=best_ask,
            spread=spread, midpoint=midpoint, last_update_ms=self.last_update_ms,
            seq=self.seq, stale=stale, gap_detected=self.gap_detected,
            needs_snapshot=self.needs_snapshot, crossed=crossed,
            valid=valid and not self.needs_snapshot)
