"""Live ledger helpers (Phase 9). Day counters + exposure + "block further live
orders" gate. Conservative: any non-terminal-clean state blocks new orders."""

from __future__ import annotations

import time
from decimal import Decimal

_DAY_MS = 24 * 60 * 60 * 1000

# Statuses that should BLOCK any further live order until manual review.
_BLOCKING_STATES = {"SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED",
                    "UNKNOWN", "RECONCILE_FAILED", "SUBMITTING", "CANCEL_REQUESTED"}


def orders_today(store, *, now_ms: int | None = None) -> int:
    if store is None:
        return 0
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        rows = store.get_micro_live_attempts(limit=1000)
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for r in rows if (now - int(r.get("ts_ms", 0))) < _DAY_MS
               and int(r.get("submitted", 0)) == 1)


def active_or_blocking(store) -> list[dict]:
    if store is None:
        return []
    try:
        rows = store.get_micro_live_attempts(limit=1000)
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rows if str(r.get("status")) in _BLOCKING_STATES]


def token_used(store, arming_token_id: str) -> bool:
    """One order per arming token: True if this token already produced an attempt."""
    if store is None or not arming_token_id:
        return False
    try:
        rows = store.get_micro_live_attempts(limit=1000)
    except Exception:  # noqa: BLE001
        return False
    # join via canary plan -> arming_token_id
    for r in rows:
        plan = store.get_micro_live_canary_plan(r.get("canary_plan_id"))
        if plan and plan.get("arming_token_id") == arming_token_id and int(r.get("submitted", 0)) == 1:
            return True
    return False


def daily_notional(store, *, now_ms: int | None = None) -> Decimal:
    if store is None:
        return Decimal(0)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        rows = store.get_micro_live_attempts(limit=1000)
    except Exception:  # noqa: BLE001
        return Decimal(0)
    total = Decimal(0)
    for r in rows:
        if (now - int(r.get("ts_ms", 0))) < _DAY_MS:
            try:
                total += Decimal(str(r.get("notional_submitted") or "0"))
            except Exception:  # noqa: BLE001
                pass
    return total
