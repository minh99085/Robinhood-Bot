"""Per-market taker-fee + slippage model for realistic paper fills.

Naive paper P&L ignores costs and is therefore optimistic. This model applies
adverse slippage to fill prices and charges round-trip taker fees so the paper
ledger reflects what real execution would actually cost. The pulse binary market
already prices its cost as the vig, so fees here apply only to position markets
(crypto / stock / polymarket).
"""

from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# taker fees by market (fraction of notional, one side)
TAKER_FEES = {
    "crypto": _env_float("HTE_FEE_CRYPTO", 0.0010),       # 10 bps
    "stock": _env_float("HTE_FEE_STOCK", 0.0005),         # 5 bps
    "polymarket": _env_float("HTE_FEE_POLYMARKET", 0.0),  # Polymarket: no taker fee
    "pulse": 0.0,                                         # cost already in the vig
}
SLIPPAGE = _env_float("HTE_SLIPPAGE", 0.0005)             # 0.05% per fill


class FeeModel:
    @staticmethod
    def taker_fee(market: str) -> float:
        return TAKER_FEES.get(market, 0.0010)

    @staticmethod
    def fill_price(price: float, side: str, market: str) -> float:
        """Adverse slippage: buys fill a touch higher, sells a touch lower."""
        if market == "pulse":
            return price
        buy = side in ("BUY", "UP", "YES")
        return price * (1 + SLIPPAGE) if buy else price * (1 - SLIPPAGE)

    @staticmethod
    def round_trip_cost(stake: float, market: str) -> float:
        """Total cost (entry+exit fees + the exit-side slippage) charged at close.
        Entry slippage is already baked into the recorded fill price."""
        if market == "pulse":
            return 0.0
        return stake * (2 * FeeModel.taker_fee(market) + SLIPPAGE)
