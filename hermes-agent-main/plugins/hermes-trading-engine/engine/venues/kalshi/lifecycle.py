"""Kalshi market/event lifecycle parsing (Phase 6, read-only)."""

from __future__ import annotations

from typing import Optional

from ..metadata import MarketLifecycleStatus, MarketResolutionEvent, payload_hash
from .normalizer import _ts_ms, cents_to_dollars

_TERMINAL = {"closed", "settled", "determined", "finalized", "resolved", "expired"}


def parse_lifecycle(msg: dict) -> MarketLifecycleStatus:
    msg = msg or {}
    return MarketLifecycleStatus(
        venue="kalshi", market_ticker=msg.get("market_ticker"),
        market_id=msg.get("market_id"), event_ticker=msg.get("event_ticker"),
        status=(msg.get("status") or msg.get("new_status") or "unknown"),
        event_type=msg.get("type") or msg.get("event_type"),
        open_ts_ms=_ts_ms(msg.get("open_ts") or msg.get("open_time")),
        close_ts_ms=_ts_ms(msg.get("close_ts") or msg.get("close_time")),
        expected_expiration_ts_ms=_ts_ms(msg.get("expected_expiration_ts")
                                         or msg.get("expected_expiration_time")),
        determined_ts_ms=_ts_ms(msg.get("determined_ts") or msg.get("determined_time")),
        settled_ts_ms=_ts_ms(msg.get("settled_ts") or msg.get("settled_time")),
        raw_payload_hash=payload_hash(msg))


def parse_resolution(msg: dict) -> Optional[MarketResolutionEvent]:
    msg = msg or {}
    status = (msg.get("status") or msg.get("new_status") or "").lower()
    result = (msg.get("result") or msg.get("outcome") or "").lower()
    if status not in _TERMINAL and not result:
        return None
    outcome = None
    if result in ("yes", "no"):
        outcome = result.upper()
    return MarketResolutionEvent(
        venue="kalshi", market_ticker=msg.get("market_ticker"),
        market_id=msg.get("market_id"), event_ticker=msg.get("event_ticker"),
        outcome=outcome,
        yes_settlement_value=cents_to_dollars(msg.get("yes_settlement_value"))
        if msg.get("yes_settlement_value") is not None
        else (1 if outcome == "YES" else (0 if outcome == "NO" else None)),
        no_settlement_value=cents_to_dollars(msg.get("no_settlement_value"))
        if msg.get("no_settlement_value") is not None
        else (1 if outcome == "NO" else (0 if outcome == "YES" else None)),
        resolved_ts_ms=_ts_ms(msg.get("determined_ts") or msg.get("determined_time")),
        settled_ts_ms=_ts_ms(msg.get("settled_ts") or msg.get("settled_time")),
        source="kalshi_lifecycle", raw_payload_hash=payload_hash(msg))
