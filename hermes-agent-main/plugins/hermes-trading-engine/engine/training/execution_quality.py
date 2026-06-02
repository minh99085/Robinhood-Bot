"""Simulated CLOB execution-quality metrics (pure Python, deterministic).

Quant scope — *Execution Engine CLOB v2 simulation* + *Backtesting & Simulation*
+ *Live Trading & Monitoring*: forward-looking execution-quality estimates for
the PAPER simulator and replay — queue-position approximation, fill probability,
slippage forecast, spread-blowout detection, partial-fill risk, markout by
horizon, and an aggregated Bregman-bundle execution-quality score.

These are ANALYTICS ONLY: they estimate how an order/bundle WOULD fill against a
simulated book. They never place, size, approve, or submit an order, and never
touch a live venue. Deterministic + stdlib-only.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger("hte.training.execution_quality")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def queue_position_approximation(ahead_size: float, order_size: float,
                                 refreshed_depth: float = 0.0) -> float:
    """Approximate queue position in ``[0, 1]`` (0 = front, 1 = back).

    A coarse model: the fraction of resting size ahead of us at our price level
    relative to the size that must clear before our order, including any
    refreshed depth that jumps the queue ahead of us.
    """
    ahead = max(0.0, float(ahead_size)) + max(0.0, float(refreshed_depth))
    order = max(1e-9, float(order_size))
    return round(_clamp01(ahead / (ahead + order)), 6)


def fill_probability(spread: float, depth_usd: float, order_usd: float, *,
                     stale: bool = False, max_spread: float = 0.08) -> float:
    """Estimated probability a marketable paper order fills in ``[0, 1]``.

    Falls toward 0 as the spread widens past ``max_spread``, as the order grows
    relative to top-of-book depth, and to 0 outright on a stale book.
    """
    if stale or depth_usd <= 0 or order_usd <= 0:
        return 0.0
    spread_term = _clamp01(1.0 - max(0.0, float(spread)) / max(1e-9, max_spread))
    depth_term = _clamp01(float(depth_usd) / (float(depth_usd) + float(order_usd)))
    return round(_clamp01(spread_term * depth_term), 6)


def slippage_forecast(order_usd: float, depth_usd: float, *, base_bps: float = 25.0,
                      impact_coeff: float = 100.0) -> float:
    """Forecast adverse slippage in bps: a fixed base plus a depth-impact term
    that grows with the order's share of top-of-book depth."""
    depth = max(1e-9, float(depth_usd))
    impact = float(impact_coeff) * (max(0.0, float(order_usd)) / depth)
    return round(float(base_bps) + impact, 6)


def spread_blowout(spread: float, baseline_spread: float, *, factor: float = 3.0) -> bool:
    """True when the current spread has blown out past ``factor`` x baseline."""
    base = max(1e-9, float(baseline_spread))
    return float(spread) > factor * base


def partial_fill_risk(order_usd: float, depth_usd: float, *,
                      max_depth_fraction: float = 0.35) -> float:
    """Risk in ``[0, 1]`` that an order only partially fills: the fraction of the
    order that exceeds the executable depth slice (``max_depth_fraction`` of top
    of book)."""
    fillable = max(0.0, float(depth_usd)) * float(max_depth_fraction)
    order = max(1e-9, float(order_usd))
    if order <= fillable:
        return 0.0
    return round(_clamp01((order - fillable) / order), 6)


def markout_by_horizon(fill_price: float, mid_by_horizon: dict, *, side: str = "BUY") -> dict:
    """Signed markout (favourable > 0) at each horizon from a fill price.

    BUY: favourable when the mid rises above the fill; SELL: when it falls below.
    ``mid_by_horizon`` maps a horizon label -> midpoint at that horizon.
    """
    sign = 1.0 if str(side).upper() == "BUY" else -1.0
    out: dict = {}
    for h, mid in mid_by_horizon.items():
        if mid is None:
            out[str(h)] = None
        else:
            out[str(h)] = round(sign * (float(mid) - float(fill_price)), 6)
    return out


def bundle_execution_quality(legs: list[dict], *, max_spread: float = 0.08,
                             max_depth_fraction: float = 0.35) -> dict:
    """Aggregate execution quality for a multi-leg Bregman bundle.

    Each leg dict: ``{"spread","depth_usd","order_usd","baseline_spread"(opt),
    "stale"(opt)}``. Returns the all-leg fill probability (product — every leg
    must fill for a hedge), worst-leg slippage forecast, max partial-fill risk,
    any spread blowout, and an overall quality score in ``[0, 1]``.
    """
    if not legs:
        return {"all_leg_fill_probability": 0.0, "worst_slippage_bps": 0.0,
                "max_partial_fill_risk": 0.0, "spread_blowout": False,
                "overall_quality": 0.0, "leg_count": 0}
    all_fill = 1.0
    worst_slip = 0.0
    max_partial = 0.0
    blowout = False
    for leg in legs:
        spread = float(leg.get("spread", 0.0))
        depth = float(leg.get("depth_usd", 0.0))
        order = float(leg.get("order_usd", 0.0))
        stale = bool(leg.get("stale", False))
        all_fill *= fill_probability(spread, depth, order, stale=stale, max_spread=max_spread)
        worst_slip = max(worst_slip, slippage_forecast(order, depth))
        max_partial = max(max_partial, partial_fill_risk(order, depth,
                                                         max_depth_fraction=max_depth_fraction))
        if "baseline_spread" in leg and spread_blowout(spread, leg["baseline_spread"]):
            blowout = True
    # overall: high all-leg fill, low partial risk, no blowout
    quality = _clamp01(all_fill * (1.0 - max_partial) * (0.0 if blowout else 1.0))
    result = {
        "all_leg_fill_probability": round(all_fill, 6),
        "worst_slippage_bps": round(worst_slip, 6),
        "max_partial_fill_risk": round(max_partial, 6),
        "spread_blowout": blowout,
        "overall_quality": round(quality, 6),
        "leg_count": len(legs),
    }
    logger.debug("bundle_execution_quality legs=%d all_fill=%.4f quality=%.4f",
                 len(legs), all_fill, quality)
    return result
