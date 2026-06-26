"""Non-atomic (sequential) leg-fill simulation for within-window dutch-book arb.

Simulates: fill leg 1, apply conservative adverse slippage on leg 2, recompute VWAP.
Rejects opportunities where guaranteed profit does not survive (Bible failure case:
buy 0.30 then second leg slips to 0.78 → net loss). PAPER ONLY.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

from engine.pulse.execution_gate import vwap_fill


def _consume_asks(levels: list, notional: float) -> list:
    """Return a shallow-copied ask ladder with ``notional`` USD consumed from the front."""
    out = []
    remaining = float(notional)
    for px, sz in levels or []:
        px, sz = float(px), float(sz)
        lvl_usd = px * sz
        if remaining <= 0:
            out.append((px, sz))
            continue
        if lvl_usd <= remaining + 1e-9:
            remaining -= lvl_usd
            continue
        keep_sh = (lvl_usd - remaining) / px
        out.append((px, keep_sh))
        remaining = 0.0
    return out


def simulate_buy_both_nonatomic(
    up_book,
    down_book,
    *,
    target_usd: float,
    fees: float = 0.0,
    epsilon: float = 0.05,
    leg2_slippage_bps: float = 50.0,
    max_book_age_s: float = 30.0,
    now: Optional[float] = None,
) -> dict:
    """Sequential BUY-both: fill UP first, re-walk DOWN asks after impact + slippage buffer."""
    up_asks = list(getattr(up_book, "asks", None) or [])
    dn_asks = list(getattr(down_book, "asks", None) or [])
    if not up_asks or not dn_asks:
        return {"survives": False, "reason": "missing_book"}

    vwu, spent_u, sh_u, full_u = vwap_fill(up_asks, target_usd)
    if vwu is None or not full_u or sh_u <= 0:
        return {"survives": False, "reason": "leg1_partial_or_empty"}

    impacted_dn = _consume_asks(dn_asks, spent_u)
    slip_mult = 1.0 + float(leg2_slippage_bps) / 10000.0
    stressed_dn = [(round(px * slip_mult, 6), sz) for px, sz in impacted_dn]

    vwd, spent_d, sh_d, full_d = vwap_fill(stressed_dn, target_usd)
    if vwd is None or not full_d:
        return {"survives": False, "reason": "leg2_partial_after_impact",
                "leg1_vwap": vwu, "leg1_spent_usd": spent_u}

    shares = min(sh_u, sh_d)
    ask_sum = vwu + vwd
    profit = shares * (1.0 - ask_sum)
    threshold = 1.0 - float(fees) - float(epsilon)
    survives = bool(shares > 0 and ask_sum < threshold and profit > 0)

    reason = "ok" if survives else "nonatomic_profit_gone"
    if ask_sum >= threshold:
        reason = "below_epsilon_after_nonatomic"

    return {
        "survives": survives,
        "reason": reason,
        "shares": round(shares, 4),
        "leg1_vwap": round(vwu, 6),
        "leg2_vwap": round(vwd, 6),
        "leg2_stressed": True,
        "leg2_slippage_bps": leg2_slippage_bps,
        "ask_sum": round(ask_sum, 6),
        "guaranteed_profit_usd": round(profit, 4),
        "leg1_spent_usd": round(spent_u, 4),
    }