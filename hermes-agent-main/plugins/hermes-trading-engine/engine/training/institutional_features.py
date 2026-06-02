"""Institutional feature engineering for Polymarket paper training.

Quant responsibility — *Data Preprocessing & Feature Engineering* and
*Statistical & Probabilistic Modeling*. Pure, deterministic, offline: given a
:class:`~engine.markets.universe_manager.MarketRecord` (or any object exposing
the same attributes) plus an optional rolling history, derive a fixed set of
microstructure / liquidity / time / information features used by the scanner,
ranker, probability stack and risk gates.

Every numeric feature is ``Optional[float]`` so a *missing* value is explicit
(``None``) rather than silently zero — this is what lets the scanner report an
honest **null-rate** and **feature-coverage**. Features never raise: bad inputs
degrade to ``None``.

PAPER ONLY / read-only: this module computes features from public market data
and never places, cancels, sizes, or signs anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Normalization references (USD / seconds). Chosen to be log-comparable to the
# universe manager's scales so feature outputs stay in a consistent [0, 1] band.
_DEPTH_FULL_USD = 2_000.0
_LIQ_FULL_USD = 100_000.0
_SPREAD_REF = 0.10           # spread at/above which spread_quality -> 0
_STALE_REF_S = 60.0          # book age at/above which stale_book_score -> 1
_TTR_IDEAL_DAYS = 7.0        # preferred days-to-resolution (research has time)

# The numeric feature fields used for null-rate / coverage accounting.
FEATURE_FIELDS = (
    "time_to_resolution_s", "time_to_resolution_score", "spread_quality",
    "top_depth_quality", "depth_weighted_microprice", "order_book_imbalance",
    "liquidity_velocity", "volume_acceleration", "stale_book_score",
    "quote_persistence", "market_entropy", "resolution_ambiguity",
    "event_correlation", "chainlink_relevance",
)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _as_float(v, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float coercion that returns ``default`` on failure."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _log_scale(value: Optional[float], full: float) -> Optional[float]:
    """0 at value<=0, ~1 at value>=full, log-spaced; ``None`` passes through."""
    if value is None:
        return None
    value = max(0.0, float(value))
    if full <= 0:
        return 0.0
    return _clamp(math.log1p(value) / math.log1p(full))


def _level_pair(level) -> tuple[Optional[float], Optional[float]]:
    """Accept ``{'price','size'}`` dicts or ``[price, size]`` pairs."""
    if isinstance(level, dict):
        return _as_float(level.get("price")), _as_float(level.get("size"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _as_float(level[0]), _as_float(level[1])
    return None, None


def _extract_book(raw: dict) -> tuple[list, list]:
    """Return ``(bids, asks)`` as lists of ``(price, size)`` floats.

    Supports several Polymarket-ish shapes so feature extraction works on live,
    replayed, and synthetic fixtures:

    * ``raw['bids']`` / ``raw['asks']`` — level lists,
    * ``raw['orderBook'] = {'bids': [...], 'asks': [...]}``,
    * best-level only: ``bestBid``/``bestBidSize`` + ``bestAsk``/``bestAskSize``.

    Levels missing a size are dropped (size is required for microprice /
    imbalance). Returns empty lists when no usable book is present.
    """
    if not isinstance(raw, dict):
        return [], []
    book = raw.get("orderBook") if isinstance(raw.get("orderBook"), dict) else raw
    bids_src = book.get("bids") if isinstance(book, dict) else None
    asks_src = book.get("asks") if isinstance(book, dict) else None
    bids: list = []
    asks: list = []
    for lvl in bids_src or []:
        p, s = _level_pair(lvl)
        if p is not None and s is not None and s > 0:
            bids.append((p, s))
    for lvl in asks_src or []:
        p, s = _level_pair(lvl)
        if p is not None and s is not None and s > 0:
            asks.append((p, s))
    if not bids and not asks:
        # best-level fallback (requires explicit sizes to be meaningful)
        bb = _as_float(raw.get("bestBid"))
        ba = _as_float(raw.get("bestAsk"))
        bbs = _as_float(raw.get("bestBidSize"))
        bas = _as_float(raw.get("bestAskSize"))
        if bb is not None and bbs and bbs > 0:
            bids.append((bb, bbs))
        if ba is not None and bas and bas > 0:
            asks.append((ba, bas))
    bids.sort(key=lambda x: x[0], reverse=True)   # best (highest) bid first
    asks.sort(key=lambda x: x[0])                 # best (lowest) ask first
    return bids, asks


def binary_entropy(p: Optional[float]) -> Optional[float]:
    """Shannon entropy (bits) of a Bernoulli(p) outcome, in ``[0, 1]``.

    Maximal (1.0) at p=0.5 (most uncertain / informative), 0 at p in {0, 1}.
    """
    if p is None:
        return None
    p = float(p)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return _clamp(-(p * math.log2(p) + (1 - p) * math.log2(1 - p)))


def _ttr_score(days: Optional[float]) -> Optional[float]:
    """Preference score for time-to-resolution (peaks near a week out)."""
    if days is None:
        return None
    if days <= 0.0:
        return 0.0
    if days <= 0.5:
        return 0.1
    if days <= 30:
        return _clamp(1.0 - abs(days - _TTR_IDEAL_DAYS) / 30.0 * 0.5)
    return _clamp(max(0.2, 1.0 - (days - 30.0) / 180.0))


@dataclass
class InstitutionalFeatures:
    """A fixed, auditable feature vector for one market at one point in time.

    All numeric fields are ``Optional[float]``; ``None`` means *not derivable
    from the available data* (e.g. microprice with no sized book), which is
    tracked as a null for coverage accounting.
    """

    market_id: str
    # --- time ---
    time_to_resolution_s: Optional[float] = None
    time_to_resolution_score: Optional[float] = None
    # --- microstructure ---
    spread_quality: Optional[float] = None
    top_depth_quality: Optional[float] = None
    depth_weighted_microprice: Optional[float] = None
    order_book_imbalance: Optional[float] = None
    # --- dynamics (need history) ---
    liquidity_velocity: Optional[float] = None
    volume_acceleration: Optional[float] = None
    quote_persistence: Optional[float] = None
    # --- freshness / information ---
    stale_book_score: Optional[float] = None
    market_entropy: Optional[float] = None
    resolution_ambiguity: Optional[float] = None
    event_correlation: Optional[float] = None
    chainlink_relevance: Optional[float] = None
    # --- bookkeeping ---
    notes: list = field(default_factory=list)

    def null_fields(self) -> list:
        """Names of numeric feature fields that are ``None`` (missing)."""
        return [f for f in FEATURE_FIELDS if getattr(self, f) is None]

    def coverage(self) -> float:
        """Fraction of numeric feature fields that are populated (non-null)."""
        n = len(FEATURE_FIELDS)
        if not n:
            return 1.0
        return round(1.0 - len(self.null_fields()) / n, 4)

    def to_dict(self) -> dict:
        d = {"market_id": self.market_id}
        for f in FEATURE_FIELDS:
            v = getattr(self, f)
            d[f] = round(v, 6) if isinstance(v, float) else v
        d["coverage"] = self.coverage()
        return d


def _dynamics(rec, history: Optional[list]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (liquidity_velocity, volume_acceleration, quote_persistence).

    ``history`` is an oldest->newest list of dicts with any of ``ts``,
    ``liquidity_usd``, ``volume_total_usd``, ``yes_price``. With <2 prior points
    velocity/acceleration are ``None`` (not enough signal). Persistence measures
    BBO/mid stability (1.0 = perfectly stable) and needs >=1 prior price.
    """
    if not history:
        return None, None, None
    pts = list(history)
    cur_liq = _as_float(getattr(rec, "liquidity_usd", None))
    cur_vol = _as_float(getattr(rec, "volume_total_usd", None))
    cur_price = _as_float(getattr(rec, "yes_price", None))

    velocity: Optional[float] = None
    if cur_liq is not None and len(pts) >= 1:
        pl = _as_float(pts[-1].get("liquidity_usd"))
        if pl is not None and pl != 0:
            # normalized per-snapshot liquidity change (dimensionless)
            velocity = _clamp((cur_liq - pl) / abs(pl), -1.0, 1.0)

    acceleration: Optional[float] = None
    vols = [(_as_float(p.get("volume_total_usd"))) for p in pts]
    vols = [v for v in vols if v is not None]
    if cur_vol is not None and len(vols) >= 2:
        d1 = cur_vol - vols[-1]
        d0 = vols[-1] - vols[-2]
        base = abs(vols[-1]) or 1.0
        acceleration = _clamp((d1 - d0) / base, -1.0, 1.0)

    persistence: Optional[float] = None
    prices = [_as_float(p.get("yes_price")) for p in pts]
    prices = [p for p in prices if p is not None]
    if cur_price is not None and prices:
        series = prices + [cur_price]
        mean = sum(series) / len(series)
        var = sum((x - mean) ** 2 for x in series) / len(series)
        std = math.sqrt(var)
        # 0 stdev -> perfectly persistent; scale so ~0.1 move halves the score
        persistence = _clamp(1.0 - std / 0.10)
    return velocity, acceleration, persistence


def compute_features(rec, *, history: Optional[list] = None,
                     group_size: int = 1,
                     chainlink_relevance: Optional[float] = None,
                     now: Optional[float] = None) -> InstitutionalFeatures:
    """Compute the institutional feature vector for ``rec``.

    Parameters
    ----------
    rec:
        A ``MarketRecord``-like object (``market_id``, ``spread``,
        ``top_depth_usd``, ``book_age_s``, ``liquidity_usd``, ``yes_price``,
        ``end_ts``, ``has_resolution_text``, ``raw`` …). Missing attributes
        degrade gracefully to ``None``.
    history:
        Optional oldest->newest list of prior snapshots (dicts) for the same
        market, used for velocity / acceleration / quote persistence.
    group_size:
        Number of markets in this market's correlated event group (>=1). Drives
        ``event_correlation``.
    chainlink_relevance:
        Optional fresh-only Chainlink relevance in ``[0, 1]`` (None -> null).
    now:
        Evaluation timestamp (defaults to ``time.time()``).
    """
    import time as _time
    now = now or _time.time()
    raw = getattr(rec, "raw", None) or {}
    feats = InstitutionalFeatures(market_id=str(getattr(rec, "market_id", "") or ""))

    # --- time-to-resolution ---
    end_ts = _as_float(getattr(rec, "end_ts", None))
    if end_ts is not None:
        ttr = max(0.0, end_ts - now)
        feats.time_to_resolution_s = ttr
        feats.time_to_resolution_score = _ttr_score(ttr / 86400.0)

    # --- spread / depth quality ---
    spread = _as_float(getattr(rec, "spread", None))
    if spread is not None and spread > 0:
        feats.spread_quality = _clamp(1.0 - spread / _SPREAD_REF)
    elif spread == 0:
        feats.notes.append("zero_or_missing_spread")
    feats.top_depth_quality = _log_scale(
        _as_float(getattr(rec, "top_depth_usd", None)), _DEPTH_FULL_USD)

    # --- microprice + imbalance (need a sized book) ---
    bids, asks = _extract_book(raw)
    if bids and asks:
        best_bid, _ = bids[0]
        best_ask, _ = asks[0]
        tot_bid = sum(s for _, s in bids)
        tot_ask = sum(s for _, s in asks)
        denom = tot_bid + tot_ask
        if denom > 0:
            # depth-weighted microprice: nearer the side with MORE resting size
            feats.depth_weighted_microprice = round(
                (best_bid * tot_ask + best_ask * tot_bid) / denom, 6)
            feats.order_book_imbalance = round((tot_bid - tot_ask) / denom, 6)
    else:
        feats.notes.append("no_sized_book")

    # --- freshness ---
    book_age = getattr(rec, "book_age_s", None)
    if book_age is not None:
        feats.stale_book_score = _clamp(float(book_age) / _STALE_REF_S)

    # --- information content ---
    feats.market_entropy = binary_entropy(_as_float(getattr(rec, "yes_price", None)))

    amb_raw = _as_float(raw.get("ambiguity"))
    if amb_raw is not None:
        feats.resolution_ambiguity = _clamp(amb_raw)
    else:
        has_text = bool(getattr(rec, "has_resolution_text", False))
        feats.resolution_ambiguity = 0.0 if has_text else 0.5

    g = max(1, int(group_size))
    feats.event_correlation = round(1.0 - 1.0 / g, 4)

    if chainlink_relevance is not None:
        feats.chainlink_relevance = _clamp(float(chainlink_relevance))

    # --- dynamics ---
    vel, acc, persist = _dynamics(rec, history)
    feats.liquidity_velocity = vel
    feats.volume_acceleration = acc
    feats.quote_persistence = persist
    return feats


def feature_coverage(features_list: list) -> dict:
    """Aggregate per-field and overall null-rate / coverage over many vectors.

    Returns ``{"n", "null_rate", "coverage", "per_field": {field: null_rate}}``.
    An empty input yields a fully-covered (null_rate 0.0) summary.
    """
    feats = list(features_list or [])
    n = len(feats)
    per_field: dict = {}
    if n == 0:
        return {"n": 0, "null_rate": 0.0, "coverage": 1.0,
                "per_field": {f: 0.0 for f in FEATURE_FIELDS}}
    total_nulls = 0
    for f in FEATURE_FIELDS:
        nulls = sum(1 for x in feats if getattr(x, f, None) is None)
        per_field[f] = round(nulls / n, 4)
        total_nulls += nulls
    null_rate = round(total_nulls / (n * len(FEATURE_FIELDS)), 4)
    return {"n": n, "null_rate": null_rate, "coverage": round(1.0 - null_rate, 4),
            "per_field": per_field}
