"""Bounded advisory-target selection for the Grok/xAI research scheduler.

Quant scope — *Signal Generation* (RESEARCH ONLY): pick the single most useful,
read-only market/group for the next bounded Grok advisory call. Priority order:

1. top Bregman near-miss (esp. ``not_exhaustive`` / ambiguous — where research on
   event-family completeness + resolution risk helps most),
2. news-linked market (a market that the current news packet references),
3. high-liquidity market group.

Grok is advisory-only: this module only CHOOSES what to research and assembles a
read-only context + advisory features. It never prices, sizes, executes, or
bypasses any gate. Pure + deterministic given its inputs.
"""

from __future__ import annotations

from typing import Optional


def _news_market_ids(news_packet) -> list:
    """Market ids referenced by the news packet (best-effort, read-only)."""
    if not news_packet:
        return []
    items = news_packet.get("items") if isinstance(news_packet, dict) else None
    if items is None and isinstance(news_packet, (list, tuple)):
        items = news_packet
    out = []
    for it in (items or []):
        mid = (it.get("market_id") if isinstance(it, dict) else None)
        if mid:
            out.append(str(mid))
    return out


def _news_relevance(news_packet) -> float:
    """Max relevance score across news items (advisory signal in [0,1])."""
    items = news_packet.get("items") if isinstance(news_packet, dict) else news_packet
    best = 0.0
    for it in (items or []):
        if isinstance(it, dict):
            try:
                best = max(best, float(it.get("relevance_score", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
    return round(best, 4)


def advisory_features_for(near_miss: Optional[dict], news_packet,
                          target_kind: str) -> dict:
    """Assemble ADVISORY-ONLY learning features for a target (never execution).

    These hints may inform ranking / active-learning labels but must never lower a
    gate. Derived from the news packet relevance + the near-miss completeness /
    ambiguity diagnostics that were sent to Grok for research."""
    nm = near_miss or {}
    comp = nm.get("completeness", {}) or {}
    nrel = _news_relevance(news_packet)
    amb = 0.0
    sx = nm.get("simplex", {}) or {}
    if comp.get("market_kind") == "ambiguous":
        amb = 0.7
    elif not comp.get("completeness_proven", True):
        amb = 0.4
    return {
        "grok_news_relevance_score": nrel,
        "grok_ambiguity_assessment": round(amb, 4),
        "grok_resolution_risk": round(0.5 if comp.get("market_kind")
                                      in ("range", "winner_take_all") else 0.2, 4),
        "grok_group_completeness_hint": ("incomplete" if not comp.get(
            "completeness_proven", True) else "complete"),
        "grok_event_family_hint": comp.get("expected_outcome_family"),
        "advisory_target_kind": target_kind,
        "advisory_only": True,
        "affects_execution": False,
    }


def select_advisory_target(*, near_misses: Optional[list] = None, news_packet=None,
                           watch_markets: Optional[list] = None,
                           min_liquidity_usd: float = 0.0) -> dict:
    """Choose ONE advisory target. Returns a dict with ``market_ctx`` (or ``None``)
    plus ``target_kind``, ``reason``, the analyzed-counter increments, and advisory
    features. Read-only; never executes. Works even with zero executable trades."""
    near_misses = near_misses or []
    watch_markets = watch_markets or []
    news_ids = set(_news_market_ids(news_packet))
    # eligible-target census (so "0 scheduled calls" is never implied without reason)
    eligible = len(near_misses) + len(news_ids) + len(watch_markets)

    # 1) top Bregman near-miss, preferring one that is news-linked.
    if near_misses:
        ranked = sorted(near_misses, key=lambda n: float(n.get("near_miss_score", 0.0)),
                        reverse=True)

        def _mids(nm):
            return set(nm.get("market_ids", nm.get("raw_market_ids", [])) or [])
        chosen = None
        for nm in ranked:
            if news_ids and (_mids(nm) & news_ids):
                chosen = nm
                break
        chosen = chosen or ranked[0]
        kind = "bregman_near_miss"
        news_linked = bool(news_ids and (_mids(chosen) & news_ids))
        comp = chosen.get("completeness", {}) or {}
        sx = chosen.get("simplex", {}) or {}
        incomplete = 1 if not comp.get("completeness_proven", True) else 0
        malformed = 1 if (sx.get("invalid_normalization")
                          or sx.get("duplicate_outcomes")) else 0
        mids = list(_mids(chosen))
        mctx = {"market_id": chosen.get("group_key") or (mids or ["near_miss"])[0],
                "question": comp.get("expected_outcome_family") or "bregman_near_miss",
                "group_ids": [chosen.get("group_key")], "market_ids": mids,
                "token_ids": chosen.get("token_ids", [])}
        return {
            "market_ctx": mctx, "target_kind": kind, "reason": "top_bregman_near_miss",
            "eligible_targets": eligible,
            "groups_analyzed": 1, "near_misses_analyzed": 1,
            "incomplete_groups_analyzed": incomplete,
            "malformed_groups_analyzed": malformed,
            "news_linked_analyzed": 1 if news_linked else 0,
            "advisory_features": advisory_features_for(chosen, news_packet, kind),
        }

    # 2) news-linked market (no near-misses available this run).
    if news_ids:
        mid = sorted(news_ids)[0]
        kind = "news_linked_market"
        return {
            "market_ctx": {"market_id": mid, "question": "news_linked_market"},
            "target_kind": kind, "reason": "news_linked_market",
            "eligible_targets": eligible,
            "groups_analyzed": 0, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
            "news_linked_analyzed": 1,
            "advisory_features": advisory_features_for(None, news_packet, kind),
        }

    # 3) highest-liquidity watched market.
    best = None
    best_liq = float(min_liquidity_usd)
    for m in watch_markets:
        liq = float((m.get("depth_usd") if isinstance(m, dict) else
                     getattr(m, "top_depth_usd", 0.0)) or 0.0)
        mid = (m.get("market_id") if isinstance(m, dict) else getattr(m, "market_id", None))
        if mid and liq >= best_liq:
            best, best_liq = mid, liq
    if best is not None:
        kind = "high_liquidity_market"
        return {
            "market_ctx": {"market_id": str(best), "question": "high_liquidity_market"},
            "target_kind": kind, "reason": "high_liquidity_market",
            "eligible_targets": eligible,
            "groups_analyzed": 1, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
            "news_linked_analyzed": 0,
            "advisory_features": advisory_features_for(None, news_packet, kind),
        }

    return {"market_ctx": None, "target_kind": None,
            "reason": "no_advisory_target_available", "eligible_targets": eligible,
            "groups_analyzed": 0, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
            "news_linked_analyzed": 0, "advisory_features": {}}
