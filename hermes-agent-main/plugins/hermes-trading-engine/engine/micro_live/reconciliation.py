"""Mandatory post-submit reconciliation (Phase 9). Polls order + fills, compares
expected vs actual, and classifies the terminal state. Conservative: unknown,
partial, mismatch, or unexpectedly-open(FOK) all FAIL/WARN and block new orders."""

from __future__ import annotations

from decimal import Decimal

from .schemas import MicroLiveReconciliationResult


def _dec(v, d="0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(d)


def _kalshi_status(raw_status: str) -> str:
    s = str(raw_status or "").lower()
    if s in ("executed", "filled"):
        return "FILLED"
    if s in ("canceled", "cancelled"):
        return "CANCELLED"
    if s in ("resting", "open", "active"):
        return "OPEN"  # unexpected for FOK -> CRITICAL
    if s in ("pending",):
        return "SUBMITTED"
    return "UNKNOWN"


def reconcile_kalshi(order_body: dict, fills_body: dict, *,
                     expected_qty: Decimal,
                     live_order_attempt_id: str) -> MicroLiveReconciliationResult:
    order = (order_body or {}).get("order", order_body or {})
    fills = (fills_body or {}).get("fills", [])
    local = _kalshi_status(order.get("status"))
    filled = _dec(order.get("filled_quantity",
                            sum(_dec(f.get("count", 0)) for f in fills)))
    fee = _dec(order.get("fee", sum(_dec(f.get("fee", 0)) for f in fills)))
    discrepancies = []
    status = "PASS"
    if local == "OPEN":
        status = "FAIL"
        discrepancies.append("fok_order_unexpectedly_open")
    elif local == "UNKNOWN":
        status = "FAIL"
        discrepancies.append("unknown_exchange_status")
    elif local == "FILLED":
        if expected_qty and filled != expected_qty:
            status = "WARN"
            discrepancies.append(f"partial_or_overfill expected={expected_qty} got={filled}")
    elif local == "CANCELLED":
        if filled > 0:
            status = "WARN"
            discrepancies.append("cancelled_but_partial_fill")
    return MicroLiveReconciliationResult(
        live_order_attempt_id=live_order_attempt_id, status=status,
        exchange_order_status=str(order.get("status")), local_order_status=local,
        filled_quantity=filled, local_filled_quantity=filled, fee=fee,
        position_delta=filled, discrepancies=discrepancies)


def terminal_order_status(local_status: str, filled: Decimal, expected: Decimal) -> str:
    if local_status == "FILLED":
        if expected and filled < expected:
            return "PARTIALLY_FILLED"
        return "FILLED"
    if local_status == "CANCELLED":
        return "PARTIALLY_FILLED" if filled > 0 else "REJECTED"
    if local_status == "OPEN":
        return "ACKNOWLEDGED"  # unexpectedly open -> needs emergency cancel
    return "UNKNOWN"
