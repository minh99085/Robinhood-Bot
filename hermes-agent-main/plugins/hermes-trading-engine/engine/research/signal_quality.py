"""Per-call research-signal quality: conviction + news freshness decay (PAPER ONLY).

Complements the long-run calibration trust (engine.training.grok_calibration): that
asks "is Grok generally right?"; this asks "how strong and how FRESH is THIS call?".

Both are ADVISORY: they only scale how much a research probability moves ``p_raw`` (and
therefore the edge). They never place, size, or gate a trade.

Pure + dependency-free + deterministic.
"""

from __future__ import annotations

from typing import Optional


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def freshness_decay(asof_ts: Optional[float], half_life_s: Optional[float],
                    now: float, *, floor: float = 0.0) -> float:
    """Exponential time-decay for a news-driven probability: ``0.5 ** (age/half_life)``.

    A fresh signal (age 0) decays to 1.0; one half-life old -> 0.5; etc., floored at
    ``floor`` so a stale-but-still-relevant view never fully vanishes. Returns 1.0 (no
    decay) when ``asof_ts`` or ``half_life_s`` is missing/invalid — so callers that
    don't supply freshness data keep today's behavior."""
    if asof_ts is None or not half_life_s or float(half_life_s) <= 0:
        return 1.0
    try:
        age = max(0.0, float(now) - float(asof_ts))
        decay = 0.5 ** (age / float(half_life_s))
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return max(_clamp01(floor), min(1.0, decay))


def conviction_multiplier(conviction: Optional[float] = None,
                          uncertainty: Optional[float] = None) -> float:
    """Per-call conviction in [0,1]. Prefers an explicit ``conviction``; else derives it
    from ``uncertainty`` as ``1 - uncertainty``; else 1.0 (no change)."""
    if conviction is not None:
        return _clamp01(conviction)
    if uncertainty is not None:
        return _clamp01(1.0 - _clamp01(uncertainty))
    return 1.0


def research_quality_multiplier(*, conviction: Optional[float] = None,
                                uncertainty: Optional[float] = None,
                                asof_ts: Optional[float] = None,
                                half_life_s: Optional[float] = None,
                                now: float = 0.0, freshness_floor: float = 0.0) -> dict:
    """Combined per-call multiplier = conviction * freshness, with the components for
    telemetry. Bounded to [0,1]. Advisory-only."""
    conv = conviction_multiplier(conviction, uncertainty)
    fresh = freshness_decay(asof_ts, half_life_s, now, floor=freshness_floor)
    return {"multiplier": round(_clamp01(conv * fresh), 6),
            "conviction": round(conv, 6), "freshness": round(fresh, 6)}
