"""Conservative slippage model. Execution price is only ever made WORSE.

Quant scope — *Execution Engine CLOB v2 simulation* + *Risk Management*: the
single conservative slippage primitive. The Bregman-arbitrage certifier applies
the same "only-ever-worse" slippage (plus fees and tick-up rounding) to every
leg's executable price, so a certified opportunity is profitable AFTER slippage.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from .types import D, OrderSide


def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:  # noqa: BLE001
        return Decimal(default)


class SlippageModel:
    """bps slippage + optional half-spread penalty. Never improves a price."""

    def __init__(self, *, slippage_bps: Decimal | None = None,
                 spread_aware: bool = True):
        self.slippage_bps = slippage_bps if slippage_bps is not None else _env_dec("PAPER_SLIPPAGE_BPS", "25")
        self.spread_aware = spread_aware

    def adjust(self, price, side: str, *, spread: Optional[Decimal] = None) -> Decimal:
        price = D(price)
        penalty = price * self.slippage_bps / Decimal(10000)
        if self.spread_aware and spread is not None:
            penalty += D(spread) / Decimal(2)
        if side == OrderSide.BUY:
            return price + penalty            # pay up
        adjusted = price - penalty            # receive less
        return adjusted if adjusted > 0 else Decimal(0)

    def as_dict(self) -> dict:
        return {"slippage_bps": str(self.slippage_bps), "spread_aware": self.spread_aware}
