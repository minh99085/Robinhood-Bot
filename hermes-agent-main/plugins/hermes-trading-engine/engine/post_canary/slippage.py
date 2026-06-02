"""Slippage helpers (Phase 10).

Quant scope — *Execution Engine CLOB v2 simulation*: realized adverse-slippage
for the live canary (UNCHANGED). The PAPER/replay forward slippage FORECAST lives
in ``engine.training.execution_quality.slippage_forecast``."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def slippage_bps(intended_price, fill_price, side: str = "BUY") -> Optional[Decimal]:
    """Adverse slippage in bps. Positive = worse than intended.

    BUY: paying more than intended is adverse. SELL: receiving less is adverse.
    """
    ip, fp = _d(intended_price), _d(fill_price)
    if ip is None or fp is None or ip <= 0:
        return None
    raw = (fp - ip) if str(side).upper() == "BUY" else (ip - fp)
    return (raw / ip) * Decimal(10000)
