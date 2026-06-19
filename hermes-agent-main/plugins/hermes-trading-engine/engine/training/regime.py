"""Tier-2 market-regime detector (PAPER ONLY, pure/deterministic).

Classifies the current paper-trading regime (``calm`` / ``normal`` / ``stressed``) from
recent realized-return volatility, drawdown, stale-data rate, and loss streak, and emits an
AGGRESSION MULTIPLIER in ``[floor, 1.0]`` that scales position size + (optionally) gate
strictness DOWN as conditions worsen. Risk-off in stress; full size only when calm.

Pure, no I/O — safe on the hot path. TIGHTEN-ONLY: the multiplier never exceeds 1.0, so it
can only reduce risk; it never enables live trading.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _stdev(xs: list) -> float:
    xs = [float(x) for x in (xs or [])]
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / n) ** 0.5


@dataclass
class RegimeState:
    regime: str
    aggression_multiplier: float
    volatility: float
    drawdown_pct: float
    stale_rate: float
    loss_streak: int
    reasons: list

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "aggression_multiplier": round(self.aggression_multiplier, 4),
            "return_volatility": round(self.volatility, 6),
            "drawdown_pct": round(self.drawdown_pct, 6),
            "stale_data_rate": round(self.stale_rate, 6),
            "loss_streak": int(self.loss_streak),
            "reasons": list(self.reasons),
        }


def detect_regime(*, recent_returns: list, drawdown_pct: float = 0.0,
                  stale_rate: float = 0.0, loss_streak: int = 0,
                  vol_normal: float = 0.10, vol_stressed: float = 0.25,
                  drawdown_stressed: float = 0.10, stale_stressed: float = 0.10,
                  loss_streak_stressed: int = 5, floor: float = 0.25,
                  min_drawdown_to_stress: float = 0.0,
                  min_loss_streak: int = 0) -> RegimeState:
    """Classify regime + aggression multiplier from recent risk signals.

    Each stress channel (return volatility, drawdown, stale-data rate, loss streak) applies a
    multiplicative haircut; the worst channels dominate. ``calm`` (mult ~1.0) when all are
    benign; ``stressed`` (mult -> ``floor``) when any breaches its stressed threshold.

    ``min_drawdown_to_stress`` / ``min_loss_streak`` are DEAD-ZONES: trivial drawdown or a
    short loss streak (expected during profit-discovery exploration) never trip risk-off —
    only MATERIAL stress reduces aggression. This keeps discovery at full size while still
    de-risking on real adverse moves."""
    vol = _stdev(recent_returns)
    dd = abs(float(drawdown_pct or 0.0))
    stale = max(0.0, float(stale_rate or 0.0))
    streak = max(0, int(loss_streak or 0))
    reasons: list = []
    mult = 1.0

    # volatility haircut: 1.0 below vol_normal, -> floor at vol_stressed
    if vol > vol_normal:
        span = max(1e-9, vol_stressed - vol_normal)
        hv = _clamp(1.0 - (vol - vol_normal) / span, floor, 1.0)
        mult = min(mult, hv)
        if vol >= vol_stressed:
            reasons.append("high_return_volatility")
    # drawdown haircut (only above the material dead-zone)
    if dd > max(0.0, float(min_drawdown_to_stress)):
        hd = _clamp(1.0 - dd / max(1e-9, drawdown_stressed), floor, 1.0)
        mult = min(mult, hd)
        if dd >= drawdown_stressed:
            reasons.append("drawdown_breach")
    # stale-data haircut (data quality is a market-condition risk)
    if stale > 0.0:
        hs = _clamp(1.0 - stale / max(1e-9, stale_stressed), floor, 1.0)
        mult = min(mult, hs)
        if stale >= stale_stressed:
            reasons.append("stale_data")
    # loss-streak haircut (only beyond the material dead-zone)
    if streak > max(0, int(min_loss_streak)):
        hl = _clamp(1.0 - streak / max(1, loss_streak_stressed), floor, 1.0)
        mult = min(mult, hl)
        if streak >= loss_streak_stressed:
            reasons.append("loss_streak")

    mult = _clamp(mult, floor, 1.0)
    if mult >= 0.85 and not reasons:
        regime = "calm"
    elif mult <= 0.5 or reasons:
        regime = "stressed"
    else:
        regime = "normal"
    return RegimeState(regime=regime, aggression_multiplier=mult, volatility=vol,
                       drawdown_pct=dd, stale_rate=stale, loss_streak=streak, reasons=reasons)
