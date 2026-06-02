"""CandidateRanker — a simple, explainable market-quality score.

Each component is in [0, 1]; the final score is a weighted sum scaled to
[0, 100]. Every component is returned so the score is fully auditable (and the
online learner can attribute performance to features later).
"""

from __future__ import annotations

import math
import time
from typing import Optional

from engine.markets import universe_manager as um


_WEIGHTS = {
    "liquidity": 0.22,
    "spread": 0.20,
    "volume": 0.16,
    "time_to_close": 0.12,
    "freshness": 0.10,
    "resolution_clarity": 0.08,
    "evidence": 0.06,
    "category": 0.06,
}


def _log_scale(value: float, full: float) -> float:
    """0 at value<=0, ~1 at value>=full, log-spaced in between."""
    value = max(0.0, float(value or 0.0))
    if value <= 0 or full <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log1p(value) / math.log1p(full)))


def score_candidate(rec: "um.MarketRecord", cfg, *,
                    category_reliability: Optional[dict] = None,
                    chainlink_relevance: float = 0.0,
                    bregman_suitability: float = 0.0,
                    now: Optional[float] = None) -> tuple:
    """Return (score_0_100, components_dict). Higher is better.

    ``chainlink_relevance`` (0..1, FRESH-only — stale/missing oracles pass 0) adds
    a bounded ranking bonus so oracle-linked crypto/FX/commodity/rate/index markets
    surface higher; this is how aggressive mode expands market coverage.

    ``bregman_suitability`` (0..1) adds a bounded bonus for markets that belong to
    a complete, all-leg-available, tight/deep, clearly-resolving outcome group —
    the structure Bregman reasoning can use. It is purely additive (never demotes
    a standalone market) and is computed by :mod:`engine.training.market_grouping`.
    """
    now = now or time.time()
    comp: dict = {}

    comp["liquidity"] = _log_scale(rec.liquidity_usd, 100_000.0)
    comp["volume"] = _log_scale(rec.volume_24h_usd, 50_000.0)

    # tighter spread -> closer to 1
    max_spread = max(1e-6, float(getattr(cfg, "max_allowed_spread", 0.04)))
    comp["spread"] = max(0.0, min(1.0, 1.0 - (float(rec.spread or 0.0) / max_spread)))

    # prefer markets that resolve in a few days-to-weeks; punish too-soon / far
    if rec.end_ts:
        days = max(0.0, (rec.end_ts - now) / 86400.0)
        if days <= 0.5:
            comp["time_to_close"] = 0.1          # too soon to research/trade
        elif days <= 30:
            comp["time_to_close"] = 1.0 - (abs(days - 7.0) / 30.0) * 0.5
        else:
            comp["time_to_close"] = max(0.2, 1.0 - (days - 30.0) / 180.0)
    else:
        comp["time_to_close"] = 0.0
    comp["time_to_close"] = max(0.0, min(1.0, comp["time_to_close"]))

    # CLOB freshness (younger book -> better). None -> neutral 0.5
    if rec.book_age_s is None:
        comp["freshness"] = 0.5
    else:
        comp["freshness"] = max(0.0, min(1.0, 1.0 - float(rec.book_age_s) / 30.0))

    comp["resolution_clarity"] = 1.0 if rec.has_resolution_text else 0.0

    desc = (rec.raw.get("description") or "")
    comp["evidence"] = max(0.0, min(1.0, len(str(desc)) / 600.0))

    # historical category performance (EWMA reliability in [0,1]); neutral 0.5
    cr = (category_reliability or {}).get(rec.category)
    comp["category"] = float(cr) if cr is not None else 0.5

    # explainable penalties (subtracted from the weighted quality sum)
    amb_raw = um._as_float(rec.raw.get("ambiguity"), None)
    ambiguity = amb_raw if amb_raw is not None else (0.0 if rec.has_resolution_text else 0.5)
    comp["ambiguity_penalty"] = round(0.20 * max(0.0, min(1.0, ambiguity)), 4)
    stale = 0.0 if rec.book_age_s is None else max(0.0, min(1.0, rec.book_age_s / 60.0))
    comp["stale_data_penalty"] = round(0.10 * stale, 4)

    score = sum(_WEIGHTS[k] * comp[k] for k in _WEIGHTS)
    score = max(0.0, score - comp["ambiguity_penalty"] - comp["stale_data_penalty"])
    # bounded Chainlink relevance bonus (fresh-only) -> coverage expansion
    cl = max(0.0, min(1.0, float(chainlink_relevance)))
    comp["chainlink_relevance"] = round(cl, 4)
    score = min(1.0, score + 0.15 * cl)
    # bounded Bregman-suitability bonus (complete/executable outcome group)
    bg = max(0.0, min(1.0, float(bregman_suitability)))
    comp["bregman_suitability"] = round(bg, 4)
    score = min(1.0, score + 0.10 * bg)
    comp_rounded = {k: round(v, 4) for k, v in comp.items()}
    return round(score * 100.0, 3), comp_rounded


def rank_candidates(records, cfg, *, category_reliability: Optional[dict] = None,
                    chainlink=None, bregman_by_market: Optional[dict] = None,
                    now: Optional[float] = None) -> list:
    """Return records sorted best-first as list of dicts with score+components.

    When a Chainlink ``scanner`` (with ``chainlink_boost``) is supplied, each
    record gets a fresh-only relevance bonus that lifts oracle-linked markets.

    ``bregman_by_market`` optionally maps ``market_id`` -> Bregman suitability
    (0..1) so markets in complete, executable outcome groups rank higher."""
    now = now or time.time()
    bregman_by_market = bregman_by_market or {}
    scored = []
    for rec in records:
        boost = 0.0
        if chainlink is not None:
            try:
                boost = float(chainlink.chainlink_boost(rec, now=now))
            except Exception:  # noqa: BLE001 — Chainlink must never break ranking
                boost = 0.0
        bg = float(bregman_by_market.get(getattr(rec, "market_id", None), 0.0) or 0.0)
        s, comp = score_candidate(rec, cfg, category_reliability=category_reliability,
                                  chainlink_relevance=boost, bregman_suitability=bg,
                                  now=now)
        scored.append({"record": rec, "score": s, "components": comp})
    scored.sort(key=lambda d: (d["score"], d["record"].market_id), reverse=True)
    return scored


def feedback_value_features(rec, *, learner=None, chainlink_relevance: float = 0.0,
                            bregman_relevance: float = 0.0, category_target: int = 50,
                            now: Optional[float] = None) -> dict:
    """Derive the active-learning feedback-value feature dict from a market
    record + learner state (Feature Engineering for active learning).

    Coarse, deterministic, offline: the uncertainty term is a pre-estimate proxy
    (spread + low-liquidity + ambiguity) since the full ProbabilityEstimate is
    computed later; the learner supplies per-category sample counts + the local
    calibration gap. Never changes a gate."""
    now = now or time.time()
    liq = max(0.0, float(getattr(rec, "liquidity_usd", 0.0) or 0.0))
    liq_q = min(1.0, math.log1p(liq) / math.log1p(100_000.0)) if liq > 0 else 0.0
    spread = float(getattr(rec, "spread", 0.0) or 0.0)
    amb_raw = um._as_float(rec.raw.get("ambiguity"), None) if getattr(rec, "raw", None) else None
    amb = amb_raw if amb_raw is not None else (0.0 if getattr(rec, "has_resolution_text", False) else 0.5)
    uncertainty = max(0.0, min(1.0, 0.5 * min(1.0, spread / 0.08) + 0.3 * (1.0 - liq_q) + 0.2 * amb))
    ttr = (rec.end_ts - now) if getattr(rec, "end_ts", None) else None
    cat_samples = learner.category_samples(rec.category) if (
        learner is not None and hasattr(learner, "category_samples")) else 0
    mid = float(getattr(rec, "yes_price", None) or 0.5)
    calib_gap = learner.calibration_gap_at(mid) if (
        learner is not None and hasattr(learner, "calibration_gap_at")) else 0.0
    has_text = bool(getattr(rec, "has_resolution_text", False))
    avail = 0.9 if (has_text and ttr is not None and 0 < ttr <= 30 * 86400) else (
        0.5 if has_text else 0.2)
    return dict(uncertainty=uncertainty, category_samples=cat_samples,
                category_target=category_target, liquidity_quality=liq_q,
                time_to_resolution_s=ttr, chainlink_relevance=chainlink_relevance,
                calibration_gap=calib_gap, bregman_relevance=bregman_relevance,
                expected_label_availability=avail)


def annotate_feedback_value(scored: list, *, learner=None, category_target: int = 50,
                            now: Optional[float] = None) -> list:
    """Annotate ranked candidate dicts (from :func:`rank_candidates`) with a
    ``feedback_value`` in [0,1] + components, reusing the Chainlink/Bregman
    bonuses already on each record. Additive — ordering by quality is unchanged;
    active learning consumes ``feedback_value`` separately."""
    from .active_learning import feedback_value_score
    for d in scored:
        rec = d.get("record")
        if rec is None:
            continue
        comps = d.get("components", {})
        feats = feedback_value_features(
            rec, learner=learner,
            chainlink_relevance=float(comps.get("chainlink_relevance", 0.0) or 0.0),
            bregman_relevance=float(comps.get("bregman_suitability", 0.0) or 0.0),
            category_target=category_target, now=now)
        fv, fcomp = feedback_value_score(**feats)
        d["feedback_value"] = fv
        d["feedback_components"] = fcomp
    return scored


def annotate_clusters(scored: list, graph, *, correlated: bool = True) -> list:
    """Annotate ranked candidate dicts with their dependency-graph ``cluster_id``
    (correlated cluster by default) so active-learning diversity + risk netting
    can avoid over-trading one correlated cluster. Additive — ordering unchanged."""
    if graph is None:
        return scored
    for d in scored:
        rec = d.get("record")
        if rec is None:
            continue
        d["cluster_id"] = graph.cluster_of(getattr(rec, "market_id", ""), correlated=correlated)
    return scored


def annotate_profitability(scored: list, cfg, *, memory=None, decay=None,
                           aggressive: bool = False, profitability_first: bool = False,
                           now: Optional[float] = None) -> list:
    """Annotate ranked candidate dicts with an ``after_cost_score`` + ``timing``
    decision (Profitability Governor; Signal Generation + Risk Management).

    The after-cost score is a pre-trade PROXY: the market-quality base score haircut
    by an estimated cost drag (spread + slippage + fee proxies) — so a fat-spread
    book ranks below a tight one at equal quality. When ``profitability_first`` the
    shortlist is RE-RANKED by after-cost score (shift from trade count toward net
    profitability); otherwise it is purely additive (ordering unchanged). A
    graylisted market gets a ``tiny_exploration`` (aggressive) or ``skip`` timing.
    Never sizes or places an order."""
    from .profitability_governor import (STATE_CLEAN, profitability_score,
                                         timing_decision)
    max_spread = max(1e-6, float(getattr(cfg, "max_spread",
                                         getattr(cfg, "max_allowed_spread", 0.08))))
    slip = float(getattr(cfg, "slippage_bps", 25.0)) / 10000.0
    fee = float(getattr(cfg, "taker_fee_bps", 0.0)) / 10000.0
    for d in scored:
        rec = d.get("record")
        if rec is None:
            continue
        base = float(d.get("score", 0.0)) / 100.0
        spread = float(getattr(rec, "spread", 0.0) or 0.0)
        # cost drag proxy in edge-units: half-spread crossing + slippage + fee
        cost_proxy = min(1.0, (0.5 * spread + slip + fee) / max_spread)
        net_proxy = base * (1.0 - cost_proxy)
        d["after_cost_score"] = round(net_proxy * 100.0, 3)
        d["profitability_score"] = profitability_score(net_proxy - 0.5, scale=0.25)
        state = memory.state(getattr(rec, "market_id", "")) if memory is not None else STATE_CLEAN
        d["graylist_state"] = state
        d["timing"] = timing_decision(
            net_edge=net_proxy - 0.5, decay_factor=1.0, graylist_state=state,
            aggressive=aggressive, min_net_edge=-0.5)
    if profitability_first:
        scored.sort(key=lambda x: (x.get("after_cost_score", 0.0),
                                   getattr(x.get("record"), "market_id", "")), reverse=True)
    return scored


class CandidateRanker:
    """Thin stateful wrapper that folds in learned category reliability and an
    optional Chainlink relevance boost (fresh-only)."""

    def __init__(self, cfg, learner=None, chainlink=None):
        self.cfg = cfg
        self.learner = learner
        self.chainlink = chainlink

    def rank(self, records, now: Optional[float] = None) -> list:
        cr = self.learner.category_reliability() if self.learner else None
        return rank_candidates(records, self.cfg, category_reliability=cr,
                               chainlink=self.chainlink, now=now)
