"""Deterministic Kalshi orderbook reconstruction for replay (Phase 6, offline).

Consumes saved Kalshi market-data events (snapshot/delta) — as raw dicts or
Phase 4 ReplayEvents — and rebuilds per-ticker binary YES/NO books. Sequence
gaps are detected and require a fresh snapshot before the book is trusted again.
No network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .orderbook import KalshiBinaryOrderbook

KALSHI_EVENT_TYPES = frozenset({
    "kalshi_orderbook_snapshot", "kalshi_orderbook_delta", "kalshi_ticker",
    "kalshi_trade", "kalshi_market_lifecycle_v2", "kalshi_event_lifecycle",
    "kalshi_event_fee_update", "orderbook_snapshot", "orderbook_delta",
})


def _payload(ev: Any) -> tuple[str, dict]:
    if isinstance(ev, dict):
        return (ev.get("event_type") or ev.get("type") or ""), ev
    et = getattr(ev, "event_type", "")
    payload = getattr(ev, "payload", None)
    return et, (payload if isinstance(payload, dict) else {})


def _cents(v) -> Decimal:
    return Decimal(str(v)) / 100


def reconstruct(events) -> dict[str, KalshiBinaryOrderbook]:
    """Replay events into {market_ticker: KalshiBinaryOrderbook}. Deterministic."""
    books: dict[str, KalshiBinaryOrderbook] = {}

    def book_for(ticker: str) -> KalshiBinaryOrderbook:
        b = books.get(ticker)
        if b is None:
            b = KalshiBinaryOrderbook(ticker)
            books[ticker] = b
        return b

    for ev in events or []:
        et, p = _payload(ev)
        et = (et or "").lower()
        ticker = p.get("market_ticker") or p.get("ticker")
        if not ticker:
            continue
        if et.endswith("orderbook_snapshot"):
            yes = [(_cents(x[0]), Decimal(str(x[1]))) for x in (p.get("yes") or [])]
            no = [(_cents(x[0]), Decimal(str(x[1]))) for x in (p.get("no") or [])]
            book_for(ticker).apply_snapshot(yes, no, seq=p.get("seq"))
        elif et.endswith("orderbook_delta"):
            book_for(ticker).apply_delta(p.get("side", "yes"), _cents(p.get("price")),
                                         Decimal(str(p.get("delta"))), seq=p.get("seq"))
    return books


def venue_breakdown(events) -> dict[str, int]:
    """Deterministic per-venue event counts for replay metrics."""
    out: dict[str, int] = {}
    for ev in events or []:
        if isinstance(ev, dict):
            venue = ev.get("venue") or "unknown"
        else:
            venue = getattr(ev, "venue", "") or "unknown"
        out[venue] = out.get(venue, 0) + 1
    return dict(sorted(out.items()))
