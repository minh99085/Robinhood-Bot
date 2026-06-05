"""Realistic-fill scoring + fantasy-fill rejection (PAPER ONLY, pure).

A paper backtest is only honest if it refuses to "fill" trades the live book
could not actually execute. This module models a marketable BUY (or SELL)
against displayed depth and rejects **fantasy fills** — sizes that exceed
available depth, cross an excessive spread, or imply unrealistic slippage.

It is pure and deterministic: no I/O, no order placement, no wallet. The paper
OMS / backtester calls :func:`assess_fill` and must treat ``fantasy=True`` as a
HARD no-fill (count it as a rejection, not a trade). After-cost economics use
:attr:`FillResult.avg_price` (incl. slippage) and :attr:`FillResult.fees`.

Quant responsibilities
----------------------
* **Quant researcher** — defines realism thresholds (depth ratio, max spread,
  max slippage, min score) and validates them against live fills.
* **Quant developer** — owns this pure model + the rejection contract (tested).
* **Backtesting/robustness** — uses fantasy rejection so paper PnL cannot be
  inflated by unfillable size.
* **CLOB v2 execution** — the live path must honor the same depth/slippage caps;
  this module is the paper-side mirror of that realism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

logger = logging.getLogger("hte.fill_realism")


@dataclass
class FillModel:
    """Realistic-fill thresholds + cost model (conservative paper defaults)."""

    taker_fee_bps: float = 60.0        # bps on filled notional
    per_share_fee: float = 0.0         # flat fee per share
    max_slippage_frac: float = 0.02    # reject if avg fill slips > 2% past top
    max_spread: float = 0.10           # reject if fractional spread > 10%
    min_fill_score: float = 0.5        # reject if realism score below this
    min_depth_ratio: float = 0.9       # require >=90% of size fillable at/in book


def _f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def spread_frac(bid, ask) -> float:
    """Fractional spread ``(ask - bid) / mid``; 0 when not computable (pure)."""
    b, a = _f(bid, 0.0), _f(ask, 0.0)
    if a <= 0 or b <= 0 or a < b:
        return 0.0
    mid = 0.5 * (a + b)
    return (a - b) / mid if mid > 0 else 0.0


def walk_book(size: float, levels: Sequence[Sequence[float]]) -> tuple[float, float]:
    """Walk ``levels`` = ``[(price, depth), ...]`` (best first) for ``size`` shares.

    Returns ``(filled_size, avg_price)``. Deterministic; partial fill when depth
    is insufficient (avg_price over the portion that filled)."""
    want = max(0.0, _f(size))
    if want <= 0 or not levels:
        return 0.0, 0.0
    filled = 0.0
    notional = 0.0
    for lvl in levels:
        price = _f(lvl[0])
        depth = max(0.0, _f(lvl[1]))
        if depth <= 0 or price <= 0:
            continue
        take = min(depth, want - filled)
        if take <= 0:
            break
        filled += take
        notional += take * price
        if filled >= want - 1e-12:
            break
    avg = notional / filled if filled > 0 else 0.0
    return round(filled, 10), round(avg, 10)


@dataclass
class FillResult:
    """Outcome of a realistic-fill assessment (pure)."""

    requested_size: float
    filled_size: float
    avg_price: float
    fees: float
    slippage_frac: float
    spread_frac: float
    score: float
    fantasy: bool
    reason: str = ""
    depth_ratio: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def fill_realism_score(depth_ratio: float, spread: float, slippage_frac: float,
                       model: FillModel) -> float:
    """Realism score in ``[0, 1]`` (1 = perfectly fillable). Pure.

    Multiplicative penalty across depth coverage, spread, and slippage so any
    single unrealistic dimension drags the score down."""
    dr = max(0.0, min(1.0, _f(depth_ratio)))
    sp_pen = max(0.0, 1.0 - _f(spread) / model.max_spread) if model.max_spread > 0 else 1.0
    sl_pen = (max(0.0, 1.0 - _f(slippage_frac) / model.max_slippage_frac)
              if model.max_slippage_frac > 0 else 1.0)
    return round(dr * sp_pen * sl_pen, 8)


def assess_fill(*, requested_size: float, ask, ask_depth: float = 0.0,
                bid=None, levels: Optional[Sequence[Sequence[float]]] = None,
                model: Optional[FillModel] = None) -> FillResult:
    """Assess whether a marketable BUY of ``requested_size`` shares is realistic.

    Provide either a single top level (``ask`` + ``ask_depth``) or a full
    ``levels`` book ``[(price, depth), ...]``. Returns a :class:`FillResult`;
    ``fantasy=True`` means the live book could not honor this fill and the caller
    MUST reject it (no paper trade). Deterministic + pure.
    """
    model = model or FillModel()
    req = max(0.0, _f(requested_size))
    top_ask = _f(ask)
    if levels is None:
        levels = [(top_ask, max(0.0, _f(ask_depth)))]
        top_ask = top_ask if top_ask > 0 else (levels[0][0] if levels else 0.0)
    else:
        levels = [(_f(p), _f(d)) for p, d in levels]
        if top_ask <= 0 and levels:
            top_ask = levels[0][0]

    filled, avg = walk_book(req, levels)
    avail = sum(max(0.0, _f(d)) for _, d in levels)
    depth_ratio = (filled / req) if req > 0 else 0.0
    spr = spread_frac(bid, top_ask) if bid is not None else 0.0
    slippage = ((avg - top_ask) / top_ask) if (top_ask > 0 and avg > 0) else 0.0
    slippage = max(0.0, slippage)  # buying: pay more = positive slippage
    score = fill_realism_score(depth_ratio, spr, slippage, model)

    reasons: list[str] = []
    if req <= 0:
        reasons.append("non_positive_size")
    if depth_ratio < model.min_depth_ratio:
        reasons.append(f"insufficient_depth({avail:.2f}<{req:.2f})")
    if spr > model.max_spread:
        reasons.append(f"spread_too_wide({spr:.4f})")
    if slippage > model.max_slippage_frac:
        reasons.append(f"slippage_too_high({slippage:.4f})")
    if score < model.min_fill_score:
        reasons.append(f"low_realism_score({score:.3f})")
    fantasy = bool(reasons)

    fees = (avg * filled) * (model.taker_fee_bps / 10_000.0) + model.per_share_fee * filled
    result = FillResult(
        requested_size=round(req, 10), filled_size=filled, avg_price=avg,
        fees=round(fees, 10), slippage_frac=round(slippage, 8),
        spread_frac=round(spr, 8), score=score, fantasy=fantasy,
        reason=";".join(reasons) if reasons else "realistic",
        depth_ratio=round(depth_ratio, 8),
        meta={"available_depth": round(avail, 6), "top_ask": round(top_ask, 8)})
    if fantasy:
        logger.debug("fantasy fill rejected: %s", result.reason)
    return result


def is_fantasy_fill(*, requested_size: float, ask, ask_depth: float = 0.0,
                    bid=None, levels: Optional[Sequence[Sequence[float]]] = None,
                    model: Optional[FillModel] = None) -> bool:
    """Convenience boolean: would this fill be a fantasy (unfillable) fill? Pure."""
    return assess_fill(requested_size=requested_size, ask=ask, ask_depth=ask_depth,
                       bid=bid, levels=levels, model=model).fantasy


# Non-null required fields for the fill-realism section of the canonical
# AlgorithmicEdgeAudit. ``fantasy_fills_rejected`` being null means realism is
# not wired — a hard audit failure (paper PnL cannot be trusted).
FILL_REALISM_AUDIT_REQUIRED: tuple = ("fantasy_fills_rejected",)


def missing_fill_realism_fields(section) -> list:
    """Return the required fill-realism audit fields that are None/absent."""
    section = section or {}
    return [f"fill_realism.{k}" for k in FILL_REALISM_AUDIT_REQUIRED
            if section.get(k) is None]


def arbitrage_execution_costs(legs: Sequence[dict], *, slippage_bps: float = 0.0) -> dict:
    """Aggregate executable costs for a multi-leg arbitrage *set* (pure).

    ``legs`` are ``{"ask","bid","requested_shares","available_depth"}`` dicts.
    Returns ``spread_cost_per_set`` (sum of per-leg half-spreads),
    ``slippage_cost_per_set`` (bps on the buy notional), and
    ``fantasy_fills_rejected`` (legs whose requested size exceeds available depth —
    a fantasy fill that must be rejected, never counted as filled). Deterministic.
    """
    spread = 0.0
    notional = 0.0
    fantasy = 0
    for leg in legs or []:
        ask = _f(leg.get("ask"))
        bid = _f(leg.get("bid"), ask)
        if ask > 0 and bid > 0 and ask >= bid:
            spread += (ask - bid) / 2.0
        notional += max(0.0, ask)
        req = _f(leg.get("requested_shares"), 0.0)
        avail = _f(leg.get("available_depth"), 0.0)
        if req > 0 and avail + 1e-12 < req:
            fantasy += 1
    slippage = (max(0.0, float(slippage_bps)) / 10_000.0) * notional
    return {"spread_cost_per_set": round(spread, 8),
            "slippage_cost_per_set": round(slippage, 8),
            "fantasy_fills_rejected": fantasy}


def fill_audit_fields(result: FillResult, *, fee_adjusted_ev: Optional[float] = None,
                      clob_v2_executable: Optional[bool] = None) -> dict:
    """Map a :class:`FillResult` to the Algorithmic Edge Audit "fill realism"
    fields (pure): fantasy rejection, spread paid, estimated slippage, partial-fill
    assumption, available depth at decision time, fee-adjusted EV, CLOB v2 status.
    """
    return {
        "fantasy_fill_rejected": bool(result.fantasy),
        "spread_paid": result.spread_frac,
        "estimated_slippage": result.slippage_frac,
        "partial_fill": result.filled_size + 1e-12 < result.requested_size,
        "available_depth_at_decision": result.meta.get("available_depth"),
        "fee_adjusted_ev": fee_adjusted_ev,
        "clob_v2_executable": (None if clob_v2_executable is None
                               else bool(clob_v2_executable)),
        "reason": result.reason,
    }
