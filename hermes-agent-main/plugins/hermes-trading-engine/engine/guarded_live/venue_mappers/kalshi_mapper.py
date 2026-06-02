"""Kalshi dry-run order mapper (Phase 8).

Maps an internal order to a would-be Kalshi create-order body for VALIDATION
only. It does NOT POST, does NOT sign a trading request, does NOT call the create
order endpoint, and does NOT use private user channels. Marked UNSENT_DRY_RUN_ONLY.
``cancel_order_on_pause`` defaults True for future safety.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

_TIF_MAP = {"FOK": "fill_or_kill", "GTC": "good_till_canceled", "IOC": "immediate_or_cancel",
            "MARKETABLE_LIMIT": "immediate_or_cancel", "LIMIT": "good_till_canceled"}


def _cents(v) -> Optional[int]:
    # Internal prices are dollars/probability in [0,1]; convert to integer cents.
    if v in (None, ""):
        return None
    try:
        return int((Decimal(str(v)) * 100).to_integral_value())
    except Exception:  # noqa: BLE001
        return None


def map_order(order: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    ticker = order.get("market_ticker") or order.get("ticker") or order.get("market_id")
    outcome = str(order.get("outcome", "YES")).lower()
    side = "yes" if outcome == "yes" else "no"
    action = "buy" if str(order.get("side", "BUY")).upper() == "BUY" else "sell"
    count = order.get("quantity") or order.get("count")
    price_cents = _cents(order.get("price") or order.get("limit_price"))
    tif = _TIF_MAP.get(str(order.get("order_type", "GTC")).upper(), "good_till_canceled")

    if not ticker:
        errors.append("missing ticker")
    if count is None or float(count) <= 0:
        errors.append("count/count_fp must be positive")
    if price_cents is None or not (1 <= price_cents <= 99):
        errors.append("price out of 1..99 cents (0.01..0.99 dollars)")

    payload = {
        "venue": "kalshi", "ticker": ticker, "action": action, "side": side,
        "count": int(count) if count is not None else None,
        ("yes_price" if side == "yes" else "no_price"): price_cents,
        "type": "limit", "time_in_force": tif,
        "post_only": bool(order.get("post_only", False)),
        "cancel_order_on_pause": True,   # default-safe for any future live phase
        "self_trade_prevention_type": order.get("self_trade_prevention_type", "cancel_resting"),
        "_intent_tag": "UNSENT_DRY_RUN_ONLY", "_signed": False, "_sent": False,
    }
    return payload, errors
