"""Chainlink scanner — data-acquisition + edge-detection layer for Polymarket.

This module NEVER trades from Chainlink alone. It scans Chainlink feeds, links
them to Polymarket markets by asset/category/slug keywords, and exposes
Chainlink as an *input feature* for:

* Statistical & Probabilistic Modeling — a conservative, bounded
  Chainlink-conditioned probability adjustment (only when a market encodes a
  parseable price threshold for the linked asset; otherwise feature/confidence
  only).
* Signal Generation — a Chainlink-informed confidence/edge score.
* Bregman Arbitrage — feature grouping by linked feed (improves fair-prob
  estimates + market grouping; it does NOT override certified arbitrage math).
* Risk Management — no-trade flags when oracle data is stale, missing,
  inconsistent, or the market is linked but unreliable.
* Backtesting — replay-safe via timestamped snapshots (no future data).
* Monitoring — scanner health metrics.

Compliance/Security: no private keys, no signing, no infrastructure mutation.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .feeds.chainlink import ChainlinkReading, ChainlinkSource, StaticChainlinkSource
from .feeds.chainlink_registry import ChainlinkFeedSpec, load_registry
from .features_chainlink import ChainlinkFeatures, compute_features

_WORD = re.compile(r"[a-z0-9&]+")
_PRICE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|m|bn)?", re.IGNORECASE)
_ABOVE = ("above", "over", "exceed", "greater", "reach", "hit", "at least",
          ">=", ">", "higher", "surpass", "top")
_BELOW = ("below", "under", "less", "drop", "fall", "<=", "<", "lower", "dip")

LINK_MIN_RELEVANCE = 0.5
MAX_PROB_ADJUSTMENT = 0.10   # hard cap on |Chainlink probability nudge|


@dataclass
class ChainlinkSignal:
    market_id: str
    feed_key: Optional[str]
    relevance: float
    confidence: float
    prob_adjustment: float
    no_trade: bool
    reasons: list = field(default_factory=list)
    features: dict = field(default_factory=dict)

    def apply(self, p_base: float) -> float:
        """Conservative Chainlink-conditioned probability. No change when there
        is no usable, fresh, relevant link."""
        if self.no_trade or self.confidence <= 0.0 or self.feed_key is None:
            return float(p_base)
        return max(0.02, min(0.98, float(p_base) + self.prob_adjustment))

    def to_dict(self) -> dict:
        return {"market_id": self.market_id, "feed_key": self.feed_key,
                "relevance": round(self.relevance, 4), "confidence": round(self.confidence, 4),
                "prob_adjustment": round(self.prob_adjustment, 5), "no_trade": self.no_trade,
                "reasons": list(self.reasons), "features": self.features}


@dataclass
class ChainlinkScanSnapshot:
    ts: float
    feeds_scanned: int
    stale_feeds: int
    readings: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ts": self.ts, "feeds_scanned": self.feeds_scanned,
                "stale_feeds": self.stale_feeds, "readings": self.readings,
                "features": self.features, "metrics": self.metrics}


def _market_text(market) -> str:
    parts = []
    for attr in ("question", "title", "slug", "category"):
        v = getattr(market, attr, None)
        if v:
            parts.append(str(v))
    raw = getattr(market, "raw", None)
    if isinstance(market, dict):
        for k in ("question", "title", "slug", "category", "description"):
            if market.get(k):
                parts.append(str(market[k]))
        raw = market
    if isinstance(raw, dict):
        for k in ("slug", "description", "groupItemTitle"):
            if raw.get(k):
                parts.append(str(raw[k]))
    return " ".join(parts).lower()


def _tokens(text: str) -> set:
    return set(_WORD.findall(text))


class ChainlinkScanner:
    """Stateful scanner. Inject a deterministic ``ChainlinkSource`` for tests /
    replay; defaults to an empty static source (safe no-op)."""

    def __init__(self, source: Optional[ChainlinkSource] = None,
                 registry: Optional[dict] = None, *, history_limit: int = 30):
        self.source = source or StaticChainlinkSource()
        self.registry = registry or load_registry()
        self.history_limit = history_limit
        self._snapshot: Optional[ChainlinkScanSnapshot] = None
        self._features: dict = {}
        self._matched_markets = 0
        self._unmatched_feeds = 0
        self._prob_impact_sum = 0.0
        self._signal_impact_sum = 0.0
        self._signal_count = 0

    # -- scanning ------------------------------------------------------------
    def scan(self, now: Optional[float] = None) -> ChainlinkScanSnapshot:
        now = now if now is not None else time.time()
        readings: dict = {}
        features: dict = {}
        stale = 0
        for key, spec in self.registry.items():
            hist = self.source.history(key, now=now, limit=self.history_limit)
            if not hist:
                continue
            feats = compute_features(hist, now=now, heartbeat_s=spec.heartbeat_s)
            if feats is None:
                continue
            readings[key] = hist[-1].to_dict()
            features[key] = feats.to_dict()
            if feats.stale:
                stale += 1
        self._features = features
        avg_dev = (sum(abs(f["deviation"]) for f in features.values()) / len(features)
                   if features else 0.0)
        metrics = {
            "feeds_in_registry": len(self.registry),
            "feeds_scanned": len(readings),
            "stale_feeds": stale,
            "fresh_feeds": len(readings) - stale,
            "avg_abs_deviation": round(avg_dev, 6),
            "matched_markets": self._matched_markets,
            "unmatched_feeds": self._unmatched_feeds,
        }
        self._snapshot = ChainlinkScanSnapshot(
            ts=now, feeds_scanned=len(readings), stale_feeds=stale,
            readings=readings, features=features, metrics=metrics)
        return self._snapshot

    # -- linking -------------------------------------------------------------
    def link_market(self, market) -> list:
        """Return [(feed_key, relevance)] sorted best-first for a market."""
        text = _market_text(market)
        toks = _tokens(text)
        scored = []
        for key, spec in self.registry.items():
            rel = 0.0
            # WHOLE-WORD match for single-word keywords (avoid "sol" matching
            # "re*sol*ves"); substring only for multi-word keywords ("s&p 500").
            hit_kw = [kw for kw in spec.asset_keywords
                      if (kw in toks) or (" " in kw and kw in text)]
            if hit_kw:
                rel += 0.6
            # category alignment (e.g. market category mentions 'crypto'/'fx')
            if spec.category and spec.category in text:
                rel += 0.2
            # pair symbol (e.g. "eth/usd" or base symbol in slug)
            base = spec.pair.split("/")[0].lower()
            if base in toks:
                rel += 0.2
            if rel > 0:
                scored.append((key, min(1.0, round(rel, 4))))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored

    # -- signal --------------------------------------------------------------
    def signal_for_market(self, market, *, p_base: float = 0.5,
                          now: Optional[float] = None) -> ChainlinkSignal:
        now = now if now is not None else time.time()
        market_id = str(getattr(market, "market_id", "") or
                        (market.get("id") if isinstance(market, dict) else "") or
                        getattr(market, "id", ""))
        links = self.link_market(market)
        if not links or links[0][1] < LINK_MIN_RELEVANCE:
            # Chainlink abstains on unlinked markets (does NOT block them)
            return ChainlinkSignal(market_id=market_id, feed_key=None, relevance=0.0,
                                   confidence=0.0, prob_adjustment=0.0, no_trade=False,
                                   reasons=["no_chainlink_link"])

        feed_key, relevance = links[0]
        self._matched_markets += 1
        spec = self.registry[feed_key]
        hist = self.source.history(feed_key, now=now, limit=self.history_limit)
        feats = compute_features(hist, now=now, heartbeat_s=spec.heartbeat_s) if hist else None

        reasons: list = []
        if feats is None:
            return ChainlinkSignal(market_id, feed_key, relevance, 0.0, 0.0, True,
                                   ["missing_oracle"])
        if feats.stale:
            reasons.append("stale_oracle")
        if feats.inconsistent:
            reasons.append("inconsistent_oracle")
        if reasons:
            # linked market but unreliable oracle -> no-trade flag, no adjustment
            return ChainlinkSignal(market_id, feed_key, relevance, 0.0, 0.0, True,
                                   reasons, feats.to_dict())

        # confidence: relevant + fresh + not too volatile
        vol_pen = min(1.0, feats.volatility * 25.0)
        confidence = round(relevance * feats.freshness * (1.0 - 0.5 * vol_pen), 4)
        prob_adjustment = self._threshold_adjustment(market, spec, feats, confidence)

        self._signal_count += 1
        self._prob_impact_sum += abs(prob_adjustment)
        self._signal_impact_sum += confidence
        reasons.append("chainlink_feature")
        return ChainlinkSignal(market_id, feed_key, relevance, confidence,
                               round(prob_adjustment, 5), False, reasons, feats.to_dict())

    def _threshold_adjustment(self, market, spec, feats: ChainlinkFeatures,
                              confidence: float) -> float:
        """Directional, bounded nudge only when the market title encodes a price
        threshold for the linked asset (Chainlink-conditioned fair value)."""
        text = _market_text(market)
        direction = 0
        if any(k in text for k in _ABOVE):
            direction = 1
        if any(k in text for k in _BELOW):
            direction = -1 if direction == 0 else direction
        if direction == 0:
            return 0.0
        threshold = _parse_threshold(text)
        if threshold is None or threshold <= 0 or feats.value <= 0:
            return 0.0
        # distance scaled by oracle volatility (more volatile -> weaker nudge)
        scale = max(0.02, feats.volatility * 4.0 + 0.05)
        z = (feats.value - threshold) / (threshold * scale)
        nudge = math.tanh(z)              # in (-1, 1); >0 means value above threshold
        signed = nudge if direction == 1 else -nudge
        return max(-MAX_PROB_ADJUSTMENT, min(MAX_PROB_ADJUSTMENT,
                                             MAX_PROB_ADJUSTMENT * confidence * signed))

    def chainlink_boost(self, market, *, now: Optional[float] = None) -> float:
        """Fresh-only relevance boost in [0, 1] for market ranking / aggressive
        coverage expansion. Returns 0.0 when the market is unlinked OR the linked
        oracle is stale/missing/inconsistent — stale data can NEVER raise a
        market's rank or eligibility (Risk Management invariant)."""
        sig = self.signal_for_market(market, now=now)
        if sig.no_trade or sig.feed_key is None or sig.confidence <= 0.0:
            return 0.0
        return round(max(0.0, min(1.0, sig.relevance * sig.confidence)), 4)

    # -- monitoring ----------------------------------------------------------
    def metrics(self) -> dict:
        base = dict(self._snapshot.metrics) if self._snapshot else {
            "feeds_scanned": 0, "stale_feeds": 0}
        base.update({
            "matched_markets": self._matched_markets,
            "unmatched_feeds": self._unmatched_feeds,
            "avg_probability_impact": round(
                self._prob_impact_sum / self._signal_count, 6) if self._signal_count else 0.0,
            "avg_signal_impact": round(
                self._signal_impact_sum / self._signal_count, 6) if self._signal_count else 0.0,
            "signals_emitted": self._signal_count,
        })
        return base

    def snapshot(self) -> Optional[ChainlinkScanSnapshot]:
        return self._snapshot


def _parse_threshold(text: str) -> Optional[float]:
    best = None
    for m in _PRICE.finditer(text):
        num = m.group(1).replace(",", "")
        try:
            val = float(num)
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix == "k":
            val *= 1_000
        elif suffix == "m":
            val *= 1_000_000
        elif suffix == "bn":
            val *= 1_000_000_000
        # ignore tiny numbers that are likely years/counts handled by caller
        if best is None or val > best:
            best = val
    return best
