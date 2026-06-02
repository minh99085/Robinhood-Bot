"""Pre/post-submit account snapshots (Phase 9). Stores only a hash + redacted
summary; never raw credentials or full account payloads."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Optional

from .schemas import LiveAccountSnapshot
from .secret_runtime import redact_dict


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def build_account_snapshot(raw: Optional[dict], venue: str, environment: str) -> LiveAccountSnapshot:
    raw = raw or {}
    h = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    cash = raw.get("cash_available")
    if cash is None and raw.get("balance") is not None:
        # Kalshi balance endpoint reports cents
        try:
            cash = Decimal(str(raw["balance"])) / Decimal(100)
        except Exception:  # noqa: BLE001
            cash = None
    return LiveAccountSnapshot(
        venue=venue, environment=environment, cash_available=_dec(cash),
        collateral_available=_dec(raw.get("collateral_available")),
        positions_value=_dec(raw.get("positions_value")),
        open_order_notional=_dec(raw.get("open_order_notional")), raw_payload_hash=h)


def redacted_account_payload(raw: Optional[dict]) -> dict:
    return redact_dict(raw or {})
