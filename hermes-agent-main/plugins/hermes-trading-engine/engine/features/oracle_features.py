"""Oracle + fast-price derived features for paper training (PAPER ONLY, pure).

This module turns the two read-only BTC price surfaces into model/risk features:

* **Chainlink BTC/USD → slow ANCHOR feature.** Chainlink updates on a ~1h
  heartbeat; it is a trusted, slow reference, NOT a tick feed. We expose its
  freshness as a *stale-anchor penalty* (a confidence multiplier) rather than a
  hard price signal.
* **Coinbase fast spot → short-horizon features.** The seconds-fresh spot price
  drives short-horizon returns (30s/60s/300s), realized volatility, a microtrend
  direction, and trend persistence.
* **Cross-feed features.** Anchor-vs-fast disagreement in basis points.
* **Market-close proximity.** How close a market is to resolution.

It is a *pure* transform: no network, no I/O, no globals, deterministic given its
inputs. Acquisition stays in ``engine.training.chainlink_oracle`` and
``engine.feeds.btc_fast_price`` (audited, unchanged). Adapters here consume those
read-only statuses/feeds via duck typing so there is no structural coupling.

Bregman-consumption contract
----------------------------
``bregman_risk_filter`` returns a RISK FILTER only — booleans + a size
multiplier + reasons. These features gate/scale risk; they are **never** proof of
an arbitrage opportunity. ``risk_filter["is_arbitrage_proof"]`` is always
``False``. Bregman certification must come from the Bregman/convex machinery
itself; this layer can only *veto or shrink* a candidate on data-quality grounds.

Quant responsibilities (this module = "preprocessing / features")
-----------------------------------------------------------------
See :data:`QUANT_RESPONSIBILITIES` for the explicit RACI-style mapping across
acquisition/ingestion, preprocessing/features, probabilistic modeling, Bregman
signal development, risk/portfolio, backtesting, optimization/robustness, CLOB v2
execution, monitoring, and compliance/security/ops.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("hte.features.oracle")

# Default short-horizon windows (seconds) for fast-price return features.
DEFAULT_HORIZONS: tuple[int, ...] = (30, 60, 300)
# Default window (seconds) used to scale market-close proximity to [0, 1].
DEFAULT_CLOSE_WINDOW_S: int = 300

# Explicit quant responsibility matrix (documentation surfaced to reviewers).
QUANT_RESPONSIBILITIES: dict[str, str] = {
    "acquisition_ingestion": (
        "Owned by engine.training.chainlink_oracle + engine.feeds.btc_fast_price "
        "(read-only). This module does NOT acquire data."),
    "preprocessing_features": (
        "THIS MODULE: anchor staleness penalty, short-horizon returns, realized "
        "volatility, microtrend, trend persistence, feed disagreement (bps), "
        "market-close proximity. Pure, deterministic, validated."),
    "probabilistic_modeling": (
        "Downstream consumers may use these as model inputs; calibration/Brier/"
        "ECE remain owned by engine.calibration."),
    "bregman_signal_development": (
        "Bregman consumes outputs ONLY via bregman_risk_filter() as risk filters; "
        "never as arbitrage proof (is_arbitrage_proof is always False)."),
    "risk_portfolio": (
        "RiskEngine remains the execution gate; this module provides a size "
        "multiplier + veto reasons as advisory risk inputs only."),
    "backtesting": (
        "Pure functions are replay-safe and deterministic for backtests."),
    "optimization_robustness": (
        "Thresholds (heartbeat, disagreement bps, close window) are explicit "
        "parameters for sweeps; no hidden state."),
    "clobv2_execution": (
        "No execution here; CLOB v2 (paper) execution is unchanged and external."),
    "monitoring": (
        "Features are logged at DEBUG and surfaced as dicts for the inspection "
        "report / status CLI."),
    "compliance_security_ops": (
        "PAPER ONLY; read-only inputs; no wallet/keys/order path; no secrets."),
}


# --------------------------------------------------------------------------- #
# Small numeric helpers (validated, never raise on bad input)
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    """Coerce to float (bool -> 1/0); return None on failure."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a Mapping or an object attribute (duck typing)."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def disagreement_bps(price_a: Any, price_b: Any) -> Optional[float]:
    """Absolute disagreement between two prices in basis points, or None.

    ``bps = |a - b| / b * 10_000``. Returns None for missing/non-positive inputs.
    """
    a, b = _num(price_a), _num(price_b)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return round(abs(a - b) / b * 10_000.0, 4)


def stale_anchor_penalty(age_seconds: Any, max_age_seconds: Any,
                         heartbeat_seconds: Any = None) -> float:
    """Confidence multiplier in [0, 1] for the slow anchor based on freshness.

    Returns 1.0 while the anchor is within its heartbeat (fresh), then decays
    linearly to 0.0 at ``max_age_seconds`` and stays 0.0 beyond. Unknown age =>
    0.0 (treat unknown freshness as fully penalized / no confidence). The
    *penalty* is ``1 - multiplier``.
    """
    age = _num(age_seconds)
    max_age = _num(max_age_seconds)
    hb = _num(heartbeat_seconds)
    if age is None or max_age is None or max_age <= 0:
        return 0.0
    fresh_below = hb if (hb is not None and 0 < hb < max_age) else 0.0
    if age <= fresh_below:
        return 1.0
    if age >= max_age:
        return 0.0
    span = max_age - fresh_below
    if span <= 0:
        return 0.0
    return round(max(0.0, min(1.0, 1.0 - (age - fresh_below) / span)), 6)


def realized_volatility(returns: Sequence[Any]) -> Optional[float]:
    """Sample standard deviation of a return series, or None if < 2 valid points."""
    vals = [r for r in (_num(x) for x in (returns or [])) if r is not None]
    if len(vals) < 2:
        return None
    return round(statistics.stdev(vals), 10)


def trend_persistence(returns: Sequence[Any]) -> Optional[float]:
    """Fraction in [0, 1] of consecutive return pairs sharing the same sign.

    0.5 ~= random walk; ->1.0 strongly persistent (trending); ->0.0 mean-
    reverting. Zero returns are ignored. None if fewer than 2 non-zero returns.
    """
    vals = [r for r in (_num(x) for x in (returns or [])) if r is not None and r != 0.0]
    if len(vals) < 2:
        return None
    pairs = list(zip(vals, vals[1:]))
    same = sum(1 for a, b in pairs if (a > 0) == (b > 0))
    return round(same / len(pairs), 6)


def microtrend(returns: Sequence[Any]) -> float:
    """Directional bias in [-1, 1]: net move / gross move over the window.

    +1 = all moves up, -1 = all moves down, 0 = balanced / no movement.
    """
    vals = [r for r in (_num(x) for x in (returns or [])) if r is not None]
    gross = sum(abs(v) for v in vals)
    if gross <= 0:
        return 0.0
    return round(sum(vals) / gross, 6)


def returns_from_history(history: Sequence[Sequence[Any]], now: Optional[float],
                         horizons: Sequence[int] = DEFAULT_HORIZONS) -> dict[int, Optional[float]]:
    """Compute simple returns over each horizon from a ``[(ts, price), ...]``
    history. Return for horizon H = price_now / price_at(now-H) - 1, or None.
    """
    out: dict[int, Optional[float]] = {int(h): None for h in horizons}
    pts = []
    for row in history or []:
        try:
            ts, px = _num(row[0]), _num(row[1])
        except (IndexError, TypeError):
            continue
        if ts is not None and px is not None and px > 0:
            pts.append((ts, px))
    if not pts:
        return out
    pts.sort(key=lambda p: p[0])
    cur_ts, cur_px = pts[-1]
    ref_now = _num(now)
    if ref_now is None:
        ref_now = cur_ts
    for h in horizons:
        cutoff = ref_now - float(h)
        old = None
        for ts, px in pts:
            if ts <= cutoff:
                old = px
            else:
                break
        if old and old > 0:
            out[int(h)] = round(cur_px / old - 1.0, 10)
    return out


# --------------------------------------------------------------------------- #
# Feature dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class AnchorFeatures:
    """Slow Chainlink BTC/USD anchor features (freshness as confidence)."""

    present: bool = False
    price: Optional[float] = None
    age_seconds: Optional[float] = None
    heartbeat_seconds: Optional[float] = None
    max_age_seconds: Optional[float] = None
    stale: Optional[bool] = None
    valid: Optional[bool] = None
    confidence_multiplier: float = 0.0   # 1.0 fresh -> 0.0 fully stale
    stale_penalty: float = 1.0           # 1 - confidence_multiplier

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FastFeatures:
    """Coinbase fast-spot short-horizon features."""

    present: bool = False
    price: Optional[float] = None
    age_seconds: Optional[float] = None
    valid: Optional[bool] = None
    returns: dict = field(default_factory=dict)   # {horizon_s: return}
    realized_vol: Optional[float] = None
    microtrend: Optional[float] = None
    trend_persistence: Optional[float] = None
    samples: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrossFeatures:
    """Anchor-vs-fast cross-feed features."""

    disagreement_bps: Optional[float] = None
    agree: Optional[bool] = None
    max_disagreement_bps: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketCloseFeatures:
    """Proximity of a market to its resolution/close time."""

    seconds_to_close: Optional[float] = None
    close_proximity: float = 0.0          # 0 far -> 1 at/after close
    within_close_window: bool = False
    window_seconds: int = DEFAULT_CLOSE_WINDOW_S

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OracleFeatureSet:
    """Bundle of all oracle/fast/cross/close features + the risk filter."""

    anchor: AnchorFeatures
    fast: FastFeatures
    cross: CrossFeatures
    market_close: MarketCloseFeatures
    risk_filter: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "anchor": self.anchor.to_dict(),
            "fast": self.fast.to_dict(),
            "cross": self.cross.to_dict(),
            "market_close": self.market_close.to_dict(),
            "risk_filter": dict(self.risk_filter),
        }


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_anchor_features(anchor: Any, *, default_heartbeat: int = 3600,
                          default_max_age: int = 7200) -> AnchorFeatures:
    """Build anchor features from a ChainlinkOracleStatus (or dict)."""
    if anchor is None:
        return AnchorFeatures(present=False, confidence_multiplier=0.0, stale_penalty=1.0)
    price = _num(_get(anchor, "price"))
    age = _num(_get(anchor, "age_seconds"))
    hb = _num(_get(anchor, "heartbeat_seconds")) or float(default_heartbeat)
    max_age = _num(_get(anchor, "max_age_seconds")) or float(default_max_age)
    mult = stale_anchor_penalty(age, max_age, hb)
    feats = AnchorFeatures(
        present=True, price=price, age_seconds=age,
        heartbeat_seconds=hb, max_age_seconds=max_age,
        stale=_get(anchor, "stale"), valid=_get(anchor, "valid"),
        confidence_multiplier=mult, stale_penalty=round(1.0 - mult, 6))
    logger.debug("anchor features: price=%s age=%s mult=%s", price, age, mult)
    return feats


def build_fast_features(fast: Any, *, returns: Optional[Mapping[int, Any]] = None,
                        history: Optional[Sequence[Sequence[Any]]] = None,
                        now: Optional[float] = None,
                        horizons: Sequence[int] = DEFAULT_HORIZONS) -> FastFeatures:
    """Build fast-spot features from a BtcFastPriceStatus (or dict) plus either a
    precomputed ``returns`` map or a ``[(ts, price), ...]`` history."""
    if fast is None and not returns and not history:
        return FastFeatures(present=False)
    price = _num(_get(fast, "price"))
    age = _num(_get(fast, "age_seconds"))
    valid = _get(fast, "valid")

    rets: dict[int, Optional[float]] = {int(h): None for h in horizons}
    samples = 0
    if returns:
        for h in horizons:
            rets[int(h)] = _num(returns.get(int(h), returns.get(h)))  # type: ignore[union-attr]
        samples = sum(1 for v in rets.values() if v is not None)
    elif history:
        rets = returns_from_history(history, now, horizons)
        samples = len([r for r in history if r])

    rvals = [v for v in rets.values() if v is not None]
    feats = FastFeatures(
        present=bool(fast is not None or rvals),
        price=price, age_seconds=age, valid=valid,
        returns={int(k): v for k, v in rets.items()},
        realized_vol=realized_volatility(rvals),
        microtrend=microtrend(rvals) if rvals else None,
        trend_persistence=trend_persistence(rvals),
        samples=samples)
    logger.debug("fast features: price=%s returns=%s vol=%s", price, rets, feats.realized_vol)
    return feats


def build_cross_features(anchor_price: Any, fast_price: Any,
                         max_disagreement_bps: float = 150.0) -> CrossFeatures:
    """Build anchor-vs-fast disagreement features."""
    bps = disagreement_bps(fast_price, anchor_price)
    agree = None if bps is None else (bps <= max_disagreement_bps)
    return CrossFeatures(disagreement_bps=bps, agree=agree,
                         max_disagreement_bps=float(max_disagreement_bps))


def build_market_close_features(now: Any, close_ts: Any,
                                window_seconds: int = DEFAULT_CLOSE_WINDOW_S) -> MarketCloseFeatures:
    """Build market-close proximity features. ``close_proximity`` ramps from 0
    (>= window away) to 1 (at/after close) over the last ``window_seconds``."""
    n, c = _num(now), _num(close_ts)
    if n is None or c is None:
        return MarketCloseFeatures(window_seconds=int(window_seconds))
    secs = c - n
    win = max(1, int(window_seconds))
    if secs <= 0:
        prox = 1.0
    elif secs >= win:
        prox = 0.0
    else:
        prox = round(1.0 - secs / win, 6)
    return MarketCloseFeatures(seconds_to_close=round(secs, 6), close_proximity=prox,
                               within_close_window=secs <= win, window_seconds=win)


def bregman_risk_filter(fs: OracleFeatureSet, *, min_anchor_confidence: float = 0.2,
                        max_disagreement_bps: float = 150.0,
                        max_realized_vol: Optional[float] = None) -> dict:
    """Derive a RISK FILTER for Bregman candidates from the feature set.

    Returns ``{allow, size_multiplier, reasons, is_arbitrage_proof}``. This is a
    data-quality veto/scaler ONLY: ``is_arbitrage_proof`` is always ``False`` —
    fresh/agreeing feeds never *prove* an arbitrage, they only permit a candidate
    that the Bregman machinery certified to proceed at a (possibly reduced) size.
    """
    reasons: list[str] = []
    allow = True
    mult = 1.0

    if not fs.anchor.present:
        allow = False
        reasons.append("anchor_missing")
    else:
        conf = fs.anchor.confidence_multiplier
        if conf < min_anchor_confidence:
            allow = False
            reasons.append("anchor_stale")
        mult *= max(0.0, conf)

    if fs.cross.disagreement_bps is not None and fs.cross.disagreement_bps > max_disagreement_bps:
        allow = False
        reasons.append("feed_disagreement")

    if max_realized_vol is not None and fs.fast.realized_vol is not None \
            and fs.fast.realized_vol > max_realized_vol:
        # Extreme volatility shrinks size but does not by itself veto.
        mult *= 0.5
        reasons.append("high_volatility")

    if fs.market_close.within_close_window:
        # Near close, shrink risk (less time to be wrong / thin liquidity).
        mult *= max(0.0, 1.0 - 0.5 * fs.market_close.close_proximity)
        reasons.append("near_close")

    return {
        "allow": bool(allow),
        "size_multiplier": round(max(0.0, min(1.0, mult)), 6),
        "reasons": reasons,
        "is_arbitrage_proof": False,  # contract: never proof of arbitrage
    }


def build_oracle_features(*, anchor: Any = None, fast: Any = None,
                          fast_returns: Optional[Mapping[int, Any]] = None,
                          fast_history: Optional[Sequence[Sequence[Any]]] = None,
                          now: Optional[float] = None,
                          market_close_ts: Optional[float] = None,
                          horizons: Sequence[int] = DEFAULT_HORIZONS,
                          max_disagreement_bps: float = 150.0,
                          close_window_seconds: int = DEFAULT_CLOSE_WINDOW_S,
                          min_anchor_confidence: float = 0.2,
                          max_realized_vol: Optional[float] = None) -> OracleFeatureSet:
    """Build the full oracle/fast feature set + Bregman risk filter (pure).

    ``anchor``/``fast`` accept the engine's ChainlinkOracleStatus /
    BtcFastPriceStatus (or plain dicts). Provide fast returns either precomputed
    (``fast_returns``) or via a ``fast_history`` of ``(ts, price)`` rows.
    """
    anchor_f = build_anchor_features(anchor)
    fast_f = build_fast_features(fast, returns=fast_returns, history=fast_history,
                                 now=now, horizons=horizons)
    cross_f = build_cross_features(anchor_f.price, fast_f.price,
                                   max_disagreement_bps=max_disagreement_bps)
    close_f = build_market_close_features(now, market_close_ts, close_window_seconds)
    fs = OracleFeatureSet(anchor=anchor_f, fast=fast_f, cross=cross_f, market_close=close_f)
    fs.risk_filter = bregman_risk_filter(
        fs, min_anchor_confidence=min_anchor_confidence,
        max_disagreement_bps=max_disagreement_bps, max_realized_vol=max_realized_vol)
    return fs


# --------------------------------------------------------------------------- #
# Adapters for the engine's read-only feed objects (duck-typed; no hard import)
# --------------------------------------------------------------------------- #
def returns_from_feed(feed: Any, now: Optional[float] = None,
                      horizons: Sequence[int] = DEFAULT_HORIZONS) -> dict[int, Optional[float]]:
    """Use a BtcFastPriceFeed's ``return_over(seconds)`` to build a returns map.

    Falls back to its rolling ``_hist`` if ``return_over`` is unavailable. Never
    raises; missing horizons are None."""
    out: dict[int, Optional[float]] = {int(h): None for h in horizons}
    ro = getattr(feed, "return_over", None)
    if callable(ro):
        for h in horizons:
            try:
                out[int(h)] = _num(ro(float(h), now=now))
            except TypeError:
                try:
                    out[int(h)] = _num(ro(float(h)))
                except Exception:  # noqa: BLE001
                    out[int(h)] = None
            except Exception:  # noqa: BLE001
                out[int(h)] = None
        return out
    hist = getattr(feed, "_hist", None)
    if hist is not None:
        return returns_from_history(list(hist), now, horizons)
    return out
