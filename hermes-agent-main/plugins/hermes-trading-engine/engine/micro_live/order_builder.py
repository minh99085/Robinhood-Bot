"""Order builder (Phase 9). Turns a CanaryPlan into a venue payload, enforcing
FOK / fill_or_kill only, rejecting GTC/GTD/batch/replace/amend, and computing a
minimal share count from the notional cap. Builds the payload HASH used to
compare against the approved Phase 8 dry-run intent."""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from .config import MicroLiveConfig
from .schemas import VenueLiveOrderPayload

_FORBIDDEN_TIF = {"good_till_canceled", "good_till_date", "gtc", "gtd"}
_FORBIDDEN_TYPE = {"GTC", "GTD", "LIMIT_GTC", "BATCH"}


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def validate_shape(order_type: str, tif: str, config: MicroLiveConfig, *,
                   batch: bool = False, replace: bool = False,
                   amend: bool = False) -> list[str]:
    errs: list[str] = []
    if batch or config.allow_batch:
        errs.append("batch_orders_forbidden")
    if replace or config.allow_replace:
        errs.append("replace_forbidden")
    if amend or config.allow_amend:
        errs.append("amend_forbidden")
    if str(tif).lower() in _FORBIDDEN_TIF:
        errs.append(f"tif_forbidden:{tif}")
    if str(order_type).upper() in _FORBIDDEN_TYPE:
        errs.append(f"order_type_forbidden:{order_type}")
    if not config.order_type_allowed(order_type):
        errs.append(f"order_type_not_allowed:{order_type}")
    if not config.tif_allowed(tif):
        errs.append(f"tif_not_allowed:{tif}")
    return errs


def _count_from_notional(notional: Decimal, price: Decimal) -> int:
    """Kalshi: 1 contract pays $1 at settle; price is in dollars [0.01,0.99].
    cost per contract = price dollars. count = floor(notional / price), min 1."""
    if price <= 0:
        return 0
    c = int((notional / price).to_integral_value(rounding=ROUND_DOWN))
    return max(c, 1)


def build_kalshi_fok_payload(plan, config: MicroLiveConfig) -> tuple[Optional[VenueLiveOrderPayload],
                                                                     list[str]]:
    errs = validate_shape(plan.order_type, plan.time_in_force, config)
    price = Decimal(str(plan.limit_price or "0"))
    if not (Decimal("0.01") <= price <= Decimal("0.99")):
        errs.append(f"price_out_of_range:{price}")
    notional = Decimal(str(plan.notional or "0"))
    if notional <= 0 or notional > config.max_order_notional_usd:
        errs.append(f"notional_out_of_cap:{notional}")
    if errs:
        return None, errs
    count = _count_from_notional(notional, price)
    # cap the count so count*price never exceeds the notional cap
    while count > 1 and Decimal(count) * price > config.max_order_notional_usd:
        count -= 1
    cents = int((price * 100).to_integral_value())
    side = "yes" if str(plan.outcome).upper() == "YES" else "no"
    payload = {
        "ticker": plan.market_ticker,
        "action": "buy" if str(plan.side).upper() == "BUY" else "sell",
        "side": side,
        "type": "limit",
        "count": count,
        "time_in_force": "fill_or_kill",
        "yes_price": cents if side == "yes" else None,
        "no_price": cents if side == "no" else None,
        "client_order_id": None,  # filled in by execution service (idempotency)
        "cancel_order_on_pause": True,
        "self_trade_prevention_type": "taker_at_cross",
        "exchange_index": 0,
        "subaccount": 0,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    p = VenueLiveOrderPayload(
        venue="kalshi", environment=plan.environment, payload_redacted=dict(payload),
        payload_hash=payload_hash(payload), unsigned_payload_hash=payload_hash(payload),
        order_type="FOK", time_in_force="fill_or_kill", price=price,
        quantity=Decimal(count), notional=Decimal(count) * price, post_only=False,
        cancel_on_pause=True)
    return p, []


def build_polymarket_fok_payload(plan, config: MicroLiveConfig) -> tuple[Optional[VenueLiveOrderPayload],
                                                                         list[str]]:
    errs = validate_shape(plan.order_type, plan.time_in_force, config)
    price = Decimal(str(plan.limit_price or "0"))
    if not (Decimal("0.01") <= price <= Decimal("0.99")):
        errs.append(f"price_out_of_range:{price}")
    notional = Decimal(str(plan.notional or "0"))
    if notional <= 0 or notional > config.max_order_notional_usd:
        errs.append(f"notional_out_of_cap:{notional}")
    if errs:
        return None, errs
    size = (notional / price)
    payload = {
        "token_id": plan.asset_id,
        "side": "BUY" if str(plan.side).upper() == "BUY" else "SELL",
        "price": str(price),
        "size": str(size),
        "order_type": "FOK",
        "fee_rate_bps": 0,
    }
    p = VenueLiveOrderPayload(
        venue="polymarket", environment=plan.environment, payload_redacted=dict(payload),
        payload_hash=payload_hash(payload), unsigned_payload_hash=payload_hash(payload),
        order_type="FOK", time_in_force="fill_or_kill", price=price, quantity=size,
        notional=notional, post_only=False)
    return p, []


def build_payload(plan, config: MicroLiveConfig):
    if plan.venue == "kalshi":
        return build_kalshi_fok_payload(plan, config)
    if plan.venue == "polymarket":
        return build_polymarket_fok_payload(plan, config)
    return None, [f"venue_not_supported:{plan.venue}"]
