"""Multi-factor cross-sectional scout ranking.

The old scout ranked on one thing — |21-day return| × trend alignment —
which surfaces "what already moved", not "what is set up to move next". A
quant scanner blends several ORTHOGONAL, cross-sectionally-ranked factors so
a name has to be good on multiple axes, not lucky on one.

Per symbol, from daily closes + volumes, we compute six factors:

  1. mom_63       — 3-month total return (medium-term trend)
  2. mom_21       — 1-month return (near-term push)
  3. risk_adj_mom — mom_63 / realized volatility (smooth beats choppy)
  4. trend_persist— fraction of the last 21 closes above a rising EMA21
  5. vol_surge    — 5-day avg $volume / 60-day avg (accumulation / interest)
  6. high_prox    — last / 126-day high (breakout readiness; 1.0 = at highs)

Each factor is turned into a cross-sectional z-score (ranked against the
whole scanned set that day), then combined with FIXED weights (no tuning) —
so "top-decile momentum AND rising volume AND near highs" scores far above
"moved 3%". Direction is set by the medium-term trend; long-only account →
bullish composites lead, bearish ones become inverse-ETF / avoid context.

Pure, dependency-free, deterministic. This is a candidate FINDER, not an
edge claim — every survivor still goes through the chart battery, MC, and
the confluence rules before it means anything.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional

from engine.chart_vision.mcp_validator import _ema, indicators_from_closes

# Fixed factor weights (documented, never tuned to a backtest).
FACTOR_WEIGHTS: Dict[str, float] = {
    "mom_63": 0.30,
    "mom_21": 0.15,
    "risk_adj_mom": 0.25,
    "trend_persist": 0.15,
    "vol_surge": 0.10,
    "high_prox": 0.05,
}

# Overbought RSI above which a bullish composite is down-weighted (chasing).
RSI_CHASE = 80.0
MIN_BARS = 70            # need 63-day momentum + a stable EMA
MIN_ABS_MOM63 = 0.02     # ±2% materiality floor on the medium-term move


def compute_factors(closes: List[float],
                    dollar_volumes: Optional[List[float]] = None,
                    ) -> Optional[Dict[str, Any]]:
    """Raw (un-normalized) factors for one symbol, or None if too short."""
    if len(closes) < MIN_BARS:
        return None
    last = closes[-1]
    if last <= 0:
        return None

    mom_63 = last / closes[-64] - 1.0
    mom_21 = last / closes[-22] - 1.0

    rets = [closes[i + 1] / closes[i] - 1.0
            for i in range(len(closes) - 64, len(closes) - 1)
            if closes[i] > 0]
    vol = pstdev(rets) * math.sqrt(252.0) if len(rets) > 2 else float("nan")
    risk_adj = mom_63 / vol if vol and vol > 1e-6 else 0.0

    ema21 = _ema(closes, 21) or last
    window = closes[-21:]
    ema_now = _ema(closes, 21) or last
    ema_prev = _ema(closes[:-10], 21) or ema_now
    ema_rising = ema_now > ema_prev
    persist = (sum(1 for c in window if c > ema21) / len(window)
               if ema_rising else 0.0)

    hi_126 = max(closes[-126:]) if len(closes) >= 126 else max(closes)
    high_prox = last / hi_126 if hi_126 > 0 else 0.0

    vol_surge = 1.0
    if dollar_volumes and len(dollar_volumes) >= 60:
        recent = mean(dollar_volumes[-5:])
        base = mean(dollar_volumes[-60:])
        vol_surge = recent / base if base > 0 else 1.0

    ind = indicators_from_closes(closes)
    return {
        "mom_63": mom_63, "mom_21": mom_21, "risk_adj_mom": risk_adj,
        "trend_persist": persist, "vol_surge": vol_surge,
        "high_prox": high_prox, "rsi14": ind.get("rsi14"),
        "ema_cross": ind.get("ema_cross"), "ret_21d": mom_21,
    }


def _zscores(values: List[float]) -> List[float]:
    finite = [v for v in values if v == v]
    if len(finite) < 2:
        return [0.0 for _ in values]
    mu = mean(finite)
    sd = pstdev(finite) or 1.0
    return [((v - mu) / sd if v == v else 0.0) for v in values]


def _setup_label(f: Dict[str, Any]) -> str:
    """Human-readable setup archetype from the raw factors."""
    if f["high_prox"] >= 0.98 and f["vol_surge"] >= 1.3:
        return "breakout (near highs on rising volume)"
    if f["risk_adj_mom"] >= 1.0 and f["trend_persist"] >= 0.6:
        return "steady uptrend (smooth, holds its average)"
    if f["mom_21"] > 0 and f["mom_63"] <= 0:
        return "early turn (near-term up, base still forming)"
    if f["vol_surge"] >= 1.5:
        return "volume spike (unusual interest)"
    return "momentum"


def rank_multifactor(
    factors_by_symbol: Dict[str, Dict[str, Any]],
    *,
    inverse_map: Optional[Dict[str, str]] = None,
    underlying_to_inverse: Optional[Dict[str, str]] = None,
    top_n: int = 8,
) -> Dict[str, Any]:
    """Cross-sectionally z-score every factor and rank by weighted composite."""
    syms = list(factors_by_symbol.keys())
    if not syms:
        return {"usable": 0, "suggest": [], "avoid": [], "downside_ideas": []}

    zs: Dict[str, List[float]] = {}
    for fac in FACTOR_WEIGHTS:
        zs[fac] = _zscores([factors_by_symbol[s][fac] for s in syms])

    rows: List[Dict[str, Any]] = []
    for i, sym in enumerate(syms):
        f = factors_by_symbol[sym]
        composite = sum(FACTOR_WEIGHTS[fac] * zs[fac][i] for fac in FACTOR_WEIGHTS)
        # Down-weight extreme overbought (chasing) on the bullish side.
        rsi = f.get("rsi14")
        if rsi is not None and rsi > RSI_CHASE and composite > 0:
            composite *= 0.6
        direction = "bullish" if f["mom_63"] > 0 else "bearish"
        rows.append({
            "symbol": sym, "direction": direction,
            "composite": round(composite, 3),
            "setup": _setup_label(f),
            "mom_63": round(f["mom_63"], 4), "mom_21": round(f["mom_21"], 4),
            "ret_21d": round(f["ret_21d"], 4),
            "risk_adj_mom": round(f["risk_adj_mom"], 3),
            "trend_persist": round(f["trend_persist"], 2),
            "vol_surge": round(f["vol_surge"], 2),
            "high_prox": round(f["high_prox"], 3),
            "rsi14": rsi, "ema_cross": f.get("ema_cross"),
            "inverse_of": (inverse_map or {}).get(sym),
            "why": (f"{f['mom_63'] * 100:+.0f}%/3mo, "
                    f"risk-adj {f['risk_adj_mom']:+.2f}, "
                    f"trend {f['trend_persist'] * 100:.0f}%, "
                    f"vol ×{f['vol_surge']:.1f}, "
                    f"{f['high_prox'] * 100:.0f}% of 6mo-high — "
                    f"{_setup_label(f)}"),
        })

    rows.sort(key=lambda r: r["composite"], reverse=True)
    bullish = [r for r in rows
               if r["direction"] == "bullish"
               and abs(r["mom_63"]) >= MIN_ABS_MOM63]
    bearish = sorted(
        [r for r in rows if r["direction"] == "bearish"
         and abs(r["mom_63"]) >= MIN_ABS_MOM63],
        key=lambda r: r["composite"])  # most-negative composite first

    downside: List[Dict[str, Any]] = []
    u2i = underlying_to_inverse or {}
    for r in bearish:
        inv = u2i.get(r["symbol"])
        if inv:
            downside.append({
                "inverse": inv, "underlying": r["symbol"],
                "why": (f"{r['symbol']} {r['mom_63'] * 100:+.0f}%/3mo, "
                        f"weakest-ranked → {inv} (1x inverse) rises as it "
                        f"falls — chart {inv}"),
            })

    return {
        "usable": len(rows),
        "suggest": bullish[:top_n],
        "avoid": bearish[: max(3, top_n // 2)],
        "downside_ideas": downside[:5],
        "factor_weights": FACTOR_WEIGHTS,
    }
