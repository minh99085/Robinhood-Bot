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

    def impact_adjust(self, price, side: str, *, spread: Optional[Decimal] = None,
                      order_usd: float = 0.0, depth_usd: float = 0.0,
                      volatility: float = 0.0,
                      impact_coeff: Decimal = Decimal("0.5")) -> Decimal:
        """Size- and volatility-aware slippage (CLOB v2 realism).

        Adds, on top of the base bps + half-spread, a market-impact term that
        grows with the order's share of executable depth and a volatility term.
        Still ONLY ever makes the price worse (never improves it)."""
        price = D(price)
        penalty = price * self.slippage_bps / Decimal(10000)
        if self.spread_aware and spread is not None:
            penalty += D(spread) / Decimal(2)
        depth = max(1e-9, float(depth_usd))
        share = max(0.0, float(order_usd)) / depth
        # impact grows ~ with the depth share; volatility widens it further
        impact_frac = float(impact_coeff) * share + 0.5 * max(0.0, float(volatility))
        penalty += price * D(str(round(impact_frac, 10)))
        if side == OrderSide.BUY:
            return price + penalty
        adjusted = price - penalty
        return adjusted if adjusted > 0 else Decimal(0)

    def as_dict(self) -> dict:
        return {"slippage_bps": str(self.slippage_bps), "spread_aware": self.spread_aware}


def drag_breakdown(ask: float, bid: Optional[float], tick: float, *,
                   slippage_bps: float, fee_bps: float) -> dict:
    """Decompose a BUY leg's conservative executable price into its cost drags.

    Bregman-certification primitive (CLOB v2 Execution + Risk): the executable
    price is the best ask rounded UP to the next tick, then loaded with slippage
    + taker fee — only ever WORSE than the touch. Returns per-share
    ``base`` (ask), ``tick_rounding`` (tick-up), ``slippage``, ``fee``,
    ``half_spread`` (ask − mid; diagnostic, already in the ask), and the final
    ``exec_price``. Pure + deterministic so certification is reproducible."""
    import math as _math
    a = max(0.0, float(ask))
    t = float(tick or 0.0)
    px_tick = (_math.ceil(a / t - 1e-9) * t) if t > 0 else a
    slip = px_tick * (float(slippage_bps) / 10000.0)
    fee = px_tick * (float(fee_bps) / 10000.0)
    half_spread = max(0.0, (a - float(bid)) / 2.0) if bid is not None else 0.0
    return {
        "base": round(a, 8),
        "tick_rounding": round(px_tick - a, 8),
        "slippage": round(slip, 8),
        "fee": round(fee, 8),
        "half_spread": round(half_spread, 8),
        "exec_price": round(px_tick + slip + fee, 8),
    }


def markout_bps(fill_price, ref_price, side: str) -> Optional[Decimal]:
    """Adverse-selection markout in bps (favourable > 0, adverse < 0).

    BUY is favourable when the reference (later mid/touch) is ABOVE the fill;
    SELL when it is below. Used to attribute execution quality on paper fills."""
    fp, rp = D(fill_price), D(ref_price)
    if fp <= 0:
        return None
    sign = Decimal(1) if str(side).upper() == "BUY" else Decimal(-1)
    return sign * (rp - fp) / fp * Decimal(10000)
