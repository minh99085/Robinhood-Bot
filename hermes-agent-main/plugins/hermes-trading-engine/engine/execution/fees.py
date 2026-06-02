"""Venue-aware paper fee model (conservative defaults)."""

from __future__ import annotations

import os
from decimal import Decimal

from .types import D, LiquidityFlag


def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:  # noqa: BLE001
        return Decimal(default)


class FeeModel:
    """Maker/taker bps fees with an optional fixed minimum. USD/notional terms.

    Defaults are intentionally conservative (overestimate cost) so paper P&L is
    not flattered.
    """

    def __init__(self, *, taker_bps: Decimal | None = None, maker_bps: Decimal | None = None,
                 min_fee: Decimal | None = None):
        self.taker_bps = taker_bps if taker_bps is not None else _env_dec("PAPER_TAKER_FEE_BPS", "30")
        self.maker_bps = maker_bps if maker_bps is not None else _env_dec("PAPER_MAKER_FEE_BPS", "10")
        self.min_fee = min_fee if min_fee is not None else _env_dec("PAPER_MIN_FEE_USD", "0")

    def bps_for(self, liquidity: str) -> Decimal:
        if liquidity == LiquidityFlag.MAKER:
            return self.maker_bps
        # TAKER and SIMULATED both use the (more conservative) taker assumption
        return self.taker_bps

    def fee(self, notional, liquidity: str = LiquidityFlag.TAKER) -> Decimal:
        notional = abs(D(notional))
        bps = self.bps_for(liquidity)
        f = notional * bps / Decimal(10000)
        return max(f, self.min_fee)

    def as_dict(self) -> dict:
        return {"taker_bps": str(self.taker_bps), "maker_bps": str(self.maker_bps),
                "min_fee_usd": str(self.min_fee)}
