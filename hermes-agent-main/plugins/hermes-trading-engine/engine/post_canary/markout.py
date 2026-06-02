"""MarkoutAnalyzer (Phase 10). Computes fill-to-mid / fill-to-touch markout and
adverse selection at configured horizons using captured local market data.

Quant scope — *Execution Engine CLOB v2 simulation* + *Live Trading & Monitoring*:
the live-canary markout analyzer (UNCHANGED). The PAPER/replay markout-by-horizon
estimate lives in ``engine.training.execution_quality.markout_by_horizon``."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .schemas import MarkoutAnalysisResult, MarkoutObservation


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _bps(markout, fill):
    if markout is None or fill is None or fill == 0:
        return None
    return (markout / fill) * Decimal(10000)


def run(ctx: dict, cfg) -> MarkoutAnalysisResult:
    md = ctx.get("market_data") or {}
    plan = ctx.get("plan") or {}
    a = ctx.get("attempt") or {}
    side = str(plan.get("side", "BUY")).upper()
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    fill = _d(md.get("fill_price")) or _d(a.get("avg_fill_price")) or _d(plan.get("limit_price"))
    horizons = md.get("horizons") or {}

    obs_list, bps_vals = [], {}
    missing_all = True
    worst = best = None
    adverse = False
    for h in cfg.markout_horizons_ms:
        hd = horizons.get(str(h), horizons.get(h))
        if not hd:
            obs_list.append(MarkoutObservation(horizon_ms=h, data_missing=True))
            continue
        missing_all = False
        bid, ask = _d(hd.get("best_bid")), _d(hd.get("best_ask"))
        mid = _d(hd.get("midpoint"))
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / Decimal(2)
        touch = ask if side == "BUY" else bid
        m_mid = (mid - fill) * sign if (mid is not None and fill is not None) else None
        m_touch = (touch - fill) * sign if (touch is not None and fill is not None) else None
        adv = (-m_mid if (m_mid is not None and m_mid < 0) else Decimal(0)) \
            if m_mid is not None else None
        if adv is not None and adv > 0:
            adverse = True
        bps = _bps(m_mid, fill)
        if bps is not None:
            bps_vals[h] = bps
            worst = bps if worst is None else min(worst, bps)
            best = bps if best is None else max(best, bps)
        obs_list.append(MarkoutObservation(
            horizon_ms=h, observed_ts_ms=hd.get("observed_ts_ms"), best_bid=bid, best_ask=ask,
            midpoint=mid, spread=(ask - bid) if (bid is not None and ask is not None) else None,
            last_trade_price=_d(hd.get("last_trade_price")), markout_vs_mid=m_mid,
            markout_vs_touch=m_touch, adverse_selection=adv, data_missing=False))

    if missing_all:
        status = "UNKNOWN"
    elif worst is not None and worst < -Decimal(str(cfg.max_adverse_markout_bps)):
        status = "WARN"
    else:
        status = "PASS"
    return MarkoutAnalysisResult(
        status=status, observations=obs_list, worst_markout_bps=worst, best_markout_bps=best,
        markout_5s_bps=bps_vals.get(5000), markout_60s_bps=bps_vals.get(60000),
        markout_5m_bps=bps_vals.get(300000), adverse_selection_detected=adverse)
