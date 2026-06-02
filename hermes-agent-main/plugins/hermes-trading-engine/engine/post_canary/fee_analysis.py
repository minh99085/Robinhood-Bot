"""Fee deviation helpers (Phase 10)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def fee_deviation_bps(expected_fee, actual_fee, notional) -> Optional[Decimal]:
    ef, af, n = _d(expected_fee), _d(actual_fee), _d(notional)
    if ef is None or af is None:
        return None
    base = n if (n and n > 0) else (ef if ef and ef > 0 else None)
    if base is None or base == 0:
        return None
    return abs(ef - af) / base * Decimal(10000)
