"""Tier-2 confidence-aware fractional-Kelly sizing (PAPER ONLY, pure/deterministic).

Institutional sizing: bet a FRACTION of the full-Kelly stake, scaled DOWN by how uncertain
the probability estimate is (a wide calibrated confidence interval -> smaller size) and by a
regime aggression multiplier, then hard-clamped to the bankroll and the paper order band.

This is TIGHTEN-ONLY: it can only reduce size vs the configured fixed/Kelly notional — it
never increases risk, and it never enables live trading. No I/O; safe on the hot path.
"""

from __future__ import annotations

from engine.training.portfolio import fractional_kelly


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def confidence_multiplier(ci_width: float, *, ci_width_max: float = 0.5,
                          floor: float = 0.2) -> float:
    """Shrink factor in ``[floor, 1.0]`` from the calibrated CI width: a tight interval
    (confident) -> ~1.0; a wide interval (uncertain) -> ``floor``. Monotonically
    non-increasing in width — uncertainty can never inflate size."""
    w = max(0.0, float(ci_width))
    span = max(1e-9, float(ci_width_max))
    return round(_clamp(1.0 - w / span, floor, 1.0), 6)


def confidence_kelly_size_usd(p: float, price: float, *, bankroll: float,
                              ci_width: float = 0.0, kelly_fraction: float = 0.25,
                              max_fraction: float = 0.05, max_size_usd: float,
                              floor_usd: float = 1.0, regime_multiplier: float = 1.0,
                              ci_width_max: float = 0.5, ci_floor: float = 0.2
                              ) -> "tuple[float, dict]":
    """Confidence- + regime-scaled fractional-Kelly stake (USD) for a $1 binary at ``price``.

    Returns ``(size_usd, components)``. ``size = fractional_kelly(p,price) * bankroll *
    confidence_multiplier(ci_width) * regime_multiplier``, clamped to ``[0, max_size_usd]``
    then floored to ``floor_usd`` only when there is a positive Kelly stake (no edge -> 0)."""
    base_frac = fractional_kelly(p, price, kelly_fraction=kelly_fraction,
                                 max_fraction=max_fraction)
    conf = confidence_multiplier(ci_width, ci_width_max=ci_width_max, floor=ci_floor)
    regime = _clamp(regime_multiplier, 0.0, 1.0)
    raw = base_frac * max(0.0, float(bankroll)) * conf * regime
    size = min(raw, float(max_size_usd))
    # floor only a genuinely-positive stake (no edge => stay at 0, no forced probe)
    if size > 0.0:
        size = max(float(floor_usd), size)
    size = min(size, float(max_size_usd))
    return round(max(0.0, size), 4), {
        "kelly_fraction_of_bankroll": round(base_frac, 6),
        "confidence_multiplier": conf,
        "regime_multiplier": round(regime, 6),
        "ci_width": round(float(ci_width), 6),
        "raw_size_usd": round(raw, 4),
    }
