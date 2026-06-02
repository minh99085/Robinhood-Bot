"""Polymarket dry-run order mapper (Phase 8).

Maps an internal order to a would-be Polymarket CLOB order intent for VALIDATION
only. It does NOT sign (no EIP-712), does NOT import a wallet signer, and does
NOT call createOrder/postOrder/createAndPostOrder/createAndPostMarketOrder.
Every intent is marked UNSIGNED_DRY_RUN_ONLY.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def _d(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def map_order(order: dict) -> tuple[dict, list[str]]:
    """Return (payload, errors). payload is a would-be CLOB order intent."""
    errors: list[str] = []
    asset_id = order.get("asset_id") or order.get("token_id")
    side = str(order.get("side", "")).upper()
    price = _d(order.get("price") or order.get("limit_price"))
    size = _d(order.get("quantity") or order.get("size"))
    tick = _d(order.get("tick_size")) or Decimal("0.01")
    order_type = str(order.get("order_type", "GTC")).upper()
    post_only = bool(order.get("post_only", False))

    if not asset_id:
        errors.append("missing tokenID/asset_id")
    if side not in ("BUY", "SELL"):
        errors.append(f"invalid side {side!r}")
    if price is None or not (Decimal("0.01") <= price <= Decimal("0.99")):
        errors.append("price out of [0.01, 0.99]")
    elif tick and (price / tick) != (price / tick).to_integral_value():
        errors.append(f"price {price} not aligned to tick {tick}")
    if size is None or size <= 0:
        errors.append("size/amount must be positive")
    if post_only and order_type in ("FOK", "FAK"):
        errors.append("post_only incompatible with FOK/FAK")

    payload = {
        "venue": "polymarket", "tokenID": asset_id, "side": side,
        "price": str(price) if price is not None else None,
        "size": str(size) if size is not None else None,
        "orderType": order_type, "tickSize": str(tick),
        "negRisk": bool(order.get("neg_risk", False)),
        "postOnly": post_only,
        "expiration": order.get("expiration") if order_type in ("GTD",) else None,
        "_intent_tag": "UNSIGNED_DRY_RUN_ONLY",
        "_signed": False, "_sent": False,
    }
    return payload, errors
