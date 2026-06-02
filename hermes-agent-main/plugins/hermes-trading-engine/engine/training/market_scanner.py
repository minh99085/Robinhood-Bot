"""MarketScanner — fast Polymarket catalog scan + filter + rank + feature pass.

Quant responsibilities — *Data Acquisition & Ingestion* (catalog fetch, opt-in /
offline), *Data Preprocessing & Feature Engineering* (normalization caching +
institutional feature extraction), *Bregman arbitrage market grouping* (event
structure), and *Signal Generation* support (ranked shortlist).

Uses the Adaptive Universe Manager primitives (``passes_filters`` +
``MarketRecord.from_raw``) so the scanner shares one filtering contract with the
rest of the engine. It separates the *scan* concern (fetch + filter + rank +
feature) from the *trade* concern (probability + edge + risk), and tracks the
funnel, latency, feature null-rate/coverage, grouping coverage, and book quality
so the training reports can show scan speed and data quality.

Speed without extra network risk:

* normalization is **cached** per (market id + book signature) so re-scans of an
  unchanged market skip re-parsing ``outcomePrices`` / ``clobTokenIds`` etc.,
* the scan limit is always honored (``cfg.scan_limit``),
* network fetch is opt-in (``fetch``) — tests inject a raw catalog list and stay
  fully offline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from engine.markets import universe_manager as um

from .candidate_ranker import annotate_feedback_value, rank_candidates
from .institutional_features import compute_features, feature_coverage
from .market_grouping import bregman_suitability, group_markets, grouping_metrics
from .metrics import ScanMetrics

log = logging.getLogger("hte.training.scanner")


@dataclass
class ScanResult:
    scanned: int = 0
    kept: int = 0
    shortlisted: int = 0
    records: list = field(default_factory=list)        # MarketRecord (kept)
    shortlist: list = field(default_factory=list)      # ranked dicts (top shortlist)
    reject_reasons: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)         # EventGroup (over kept)
    features: list = field(default_factory=list)        # InstitutionalFeatures (shortlist)
    graph: object = None                                # MarketDependencyGraph (shortlist)
    feature_coverage: float = 0.0
    null_rate: float = 0.0
    stale_rate: float = 0.0
    group_coverage: float = 0.0
    cache_hits: int = 0
    latency_ms: float = 0.0
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned, "kept": self.kept, "shortlisted": self.shortlisted,
            "reject_reasons": self.reject_reasons,
            "groups_detected": len(self.groups),
            "feature_coverage": round(self.feature_coverage, 4),
            "null_rate": round(self.null_rate, 4),
            "stale_rate": round(self.stale_rate, 4),
            "group_coverage": round(self.group_coverage, 4),
            "cache_hits": self.cache_hits,
            "dependency_graph": self.graph.to_report() if self.graph is not None else {},
            "latency_ms": round(self.latency_ms, 2), "ts": self.ts,
        }


def _book_signature(raw: dict) -> str:
    """A cheap signature of the bits that change a normalized record.

    Re-scanning a market whose price / spread / book timestamp / size are all
    unchanged is a cache hit, so we skip re-parsing the heavier list fields.
    """
    g = raw.get
    return "|".join(str(g(k, "")) for k in (
        "bestBid", "bestAsk", "spread", "outcomePrices", "lastTradePrice",
        "liquidityNum", "volume24hr", "bookUpdatedTs", "orderBookTs",
        "closed", "active", "acceptingOrders"))


class MarketScanner:
    def __init__(self, cfg, metrics: Optional[ScanMetrics] = None, learner=None,
                 chainlink=None):
        self.cfg = cfg
        self.metrics = metrics or ScanMetrics()
        self.learner = learner
        # optional Chainlink scanner -> fresh relevance boost in ranking
        self.chainlink = chainlink
        # normalization cache: market_id -> (signature, MarketRecord)
        self._norm_cache: dict = {}

    def fetch(self, client=None) -> list:
        """Network fetch of the Polymarket catalog (opt-in / realtime only)."""
        ucfg = getattr(self.cfg, "universe", None)
        return um.fetch_catalog(ucfg, client=client)

    def _normalize(self, raw: dict, now: float) -> tuple:
        """Return (MarketRecord, cache_hit). Reuses a cached parse when the
        market's book signature is unchanged, only refreshing book age."""
        mid = str(raw.get("id") or raw.get("slug") or "")
        sig = _book_signature(raw)
        cached = self._norm_cache.get(mid) if mid else None
        if cached is not None and cached[0] == sig:
            rec = cached[1]
            book_ts = raw.get("bookUpdatedTs") or raw.get("orderBookTs")
            if book_ts:
                rec.book_age_s = now - um._as_float(book_ts)
            return rec, True
        rec = um.MarketRecord.from_raw(raw, now=now)
        if mid:
            self._norm_cache[mid] = (sig, rec)
        return rec, False

    def scan(self, raw_catalog: Optional[list] = None, *, client=None,
             now: Optional[float] = None) -> ScanResult:
        now = now or time.time()
        t0 = time.time()
        if raw_catalog is None:
            raw_catalog = self.fetch(client=client)
        raw_catalog = list(raw_catalog or [])

        scan_limit = int(getattr(self.cfg, "scan_limit", 1000))
        raw_catalog = raw_catalog[:scan_limit]

        ucfg = getattr(self.cfg, "universe", None) or um.UniverseConfig.from_env()
        kept: list = []
        reasons: dict = {}
        cache_hits = 0
        for raw in raw_catalog:
            ok, reason = um.passes_filters(raw, ucfg, now=now)
            if not ok:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            rec, hit = self._normalize(raw, now)
            cache_hits += 1 if hit else 0
            kept.append(rec)

        # --- Bregman event grouping over kept markets (structure only) ---
        groups: list = []
        bregman_by_market: dict = {}
        if getattr(self.cfg, "grouping_enabled", True):
            groups = group_markets(kept)
            for g in groups:
                suit = bregman_suitability(g)
                for mid in g.market_ids:
                    bregman_by_market[mid] = suit

        cr = self.learner.category_reliability() if self.learner else None
        ranked = rank_candidates(kept, ucfg, category_reliability=cr,
                                 chainlink=self.chainlink,
                                 bregman_by_market=bregman_by_market, now=now)
        shortlist_limit = int(getattr(self.cfg, "shortlist_limit", 150))
        shortlist = ranked[:shortlist_limit]
        # Active-learning annotation (aggressive paper mode only): tag each
        # shortlisted candidate with a feedback_value so the ActiveLearningSelector
        # can fill idle paper budget with the highest-learning-value near-misses.
        # Purely additive — quality ordering above is unchanged.
        if getattr(self.cfg, "active_learning_enabled", False):
            annotate_feedback_value(
                shortlist, learner=self.learner,
                category_target=int(getattr(self.cfg, "category_sample_target", 50)),
                now=now)
        recs = [d["record"] for d in shortlist]

        # --- institutional feature extraction over the shortlist ---
        features: list = []
        if getattr(self.cfg, "feature_extraction_enabled", True) and recs:
            gsize = {}
            for g in groups:
                for mid in g.market_ids:
                    gsize[mid] = g.n_legs
            for d in shortlist:
                rec = d["record"]
                cl = None
                if self.chainlink is not None:
                    try:
                        cl = float(self.chainlink.chainlink_boost(rec, now=now))
                    except Exception:  # noqa: BLE001 — never break the scan
                        cl = None
                f = compute_features(rec, group_size=gsize.get(rec.market_id, 1),
                                     chainlink_relevance=cl, now=now)
                d["features"] = f
                features.append(f)

        # --- market dependency graph over the shortlist (structure + risk) ---
        # Feeds combinatorial Bregman grouping, exposure netting, and aggressive
        # diversification. Built over the shortlist to bound cost; never trades.
        graph = None
        if getattr(self.cfg, "dependency_graph_enabled", True) and recs:
            try:
                from .dependency_graph import MarketDependencyGraph
                graph = MarketDependencyGraph.build(recs)
                for d in shortlist:
                    d["cluster_id"] = graph.cluster_of(
                        getattr(d["record"], "market_id", ""), correlated=True)
            except Exception:  # noqa: BLE001 — graph must never break a scan
                graph = None

        cov = feature_coverage(features)
        clob_stale_ms = float(getattr(self.cfg, "clob_stale_ms", 3000.0))
        stale = sum(1 for r in recs
                    if (r.book_age_s or 0.0) * 1000.0 > clob_stale_ms)
        stale_rate = (stale / len(recs)) if recs else 0.0
        gm = grouping_metrics(kept, groups) if groups else {"group_coverage": 0.0}

        latency_ms = (time.time() - t0) * 1000.0
        res = ScanResult(
            scanned=len(raw_catalog), kept=len(kept), shortlisted=len(shortlist),
            records=recs, shortlist=shortlist, reject_reasons=reasons,
            groups=groups, features=features, graph=graph,
            feature_coverage=cov["coverage"], null_rate=cov["null_rate"],
            stale_rate=round(stale_rate, 4), group_coverage=gm.get("group_coverage", 0.0),
            cache_hits=cache_hits, latency_ms=latency_ms, ts=now)

        self.metrics.record_scan(scanned=res.scanned, kept=res.kept,
                                 shortlisted=res.shortlisted, candidates=0,
                                 latency_ms=latency_ms, ts=now)
        self.metrics.record_feature_quality(
            feature_coverage=res.feature_coverage, null_rate=res.null_rate,
            stale_rate=res.stale_rate, group_coverage=res.group_coverage,
            groups_detected=len(groups))
        self.metrics.norm_cache_hits = cache_hits
        # rolling book-quality aggregates over the shortlist
        if recs:
            self.metrics.avg_spread = sum(r.spread for r in recs) / len(recs)
            self.metrics.avg_depth = sum(r.top_depth_usd for r in recs) / len(recs)
            ages = [r.book_age_s for r in recs if r.book_age_s is not None]
            self.metrics.avg_bbo_age_ms = (sum(ages) / len(ages) * 1000.0) if ages else 0.0
        log.debug("scan kept=%d shortlist=%d groups=%d coverage=%.2f null=%.2f "
                  "stale=%.2f cache_hits=%d %.1fms", res.kept, res.shortlisted,
                  len(groups), res.feature_coverage, res.null_rate, res.stale_rate,
                  cache_hits, latency_ms)
        return res
