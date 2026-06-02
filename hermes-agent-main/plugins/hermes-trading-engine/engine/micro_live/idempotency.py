"""Idempotency (Phase 9). Deterministic client_order_id + duplicate-submit guard.
A timeout after submit must NEVER cause a blind resubmit."""

from __future__ import annotations

import datetime
import hashlib


def make_client_order_id(venue: str, canary_plan_id: str, nonce: int = 1) -> str:
    d = datetime.datetime.utcnow().strftime("%Y%m%d")
    h = hashlib.sha256((canary_plan_id or "").encode("utf-8")).hexdigest()[:8]
    return f"mlt-{venue}-{d}-{h}-{int(nonce)}"


def already_attempted(store, canary_plan_id: str) -> bool:
    """True if an order attempt already exists for this canary plan (idempotency)."""
    if store is None:
        return False
    try:
        return bool(store.get_micro_live_attempts_for_plan(canary_plan_id))
    except Exception:  # noqa: BLE001
        return False
