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
                           grok_candidates: Optional[list] = None,
                           voi_targets: Optional[list] = None,
                           focus_targets: Optional[list] = None,
                           focus_only: bool = False,
                           min_candidate_score: float = 0.02,
                           min_voi: float = 0.05,
                           min_liquidity_usd: float = 0.0,
                           exclude_market_ids: Optional[set] = None) -> dict:
    """Choose ONE advisory target. Returns a dict with ``market_ctx`` (or ``None``)
    plus ``target_kind``, ``reason``, the analyzed-counter increments, and advisory
    features. Read-only; never executes. Works even with zero executable trades.

    ``focus_targets`` (e.g. the BTC/ETH directional "pulse" shortlist) are researched
    ABSOLUTELY FIRST when present — this is how the operator steers Grok's bounded budget
    onto the lane being traded. With ``focus_only`` set, NO other target kind is chosen
    (Grok researches only the focus universe; it simply makes no call when all focus
    targets are on cooldown).

    ``grok_candidates`` (from bregman_candidate_finder.rank_candidates) are STRONG
    Grok-flagged mispricings; when present and above ``min_candidate_score`` they are
    researched next (the tightest discovery loop: Grok studies what it itself
    flagged). Still research-only — the certifier remains the only trade gate."""
    near_misses = near_misses or []
    watch_markets = watch_markets or []
    grok_candidates = grok_candidates or []
    voi_targets = voi_targets or []
    focus_targets = focus_targets or []
    # COVERAGE ROTATION (Option 2): drop targets already researched recently this run so
    # the scheduler advances to a FRESH market each call instead of re-picking the same
    # top target (which returns a cached result and wastes the budget). Read-only.
    excl = {str(x) for x in (exclude_market_ids or set())}
    if excl:
        def _keep(mid) -> bool:
            return str(mid) not in excl
        near_misses = [n for n in near_misses
                       if _keep(n.get("group_key"))
                       and not (set(map(str, n.get("market_ids", []) or [])) <= excl
                                and (n.get("market_ids")))]
        voi_targets = [v for v in voi_targets if _keep(v.get("market_id"))]
        watch_markets = [w for w in watch_markets if _keep(w.get("market_id"))]
        grok_candidates = [c for c in grok_candidates if _keep(c.get("group_id"))]
        focus_targets = [f for f in focus_targets if _keep(f.get("market_id"))]
    news_ids = set(_news_market_ids(news_packet))
    # eligible-target census (so "0 scheduled calls" is never implied without reason)
    eligible = (len(near_misses) + len(news_ids) + len(watch_markets)
                + len(grok_candidates) + len(voi_targets) + len(focus_targets))

    # 0a) BTC-PULSE FOCUS — steer the budget onto the traded directional lane first.
    if focus_targets:
        def _fscore(f):
            return (float(f.get("confidence", 0.0) or 0.0),
                    float(f.get("liquidity_usd", 0.0) or 0.0))
        ft = sorted(focus_targets, key=_fscore, reverse=True)[0]
        return {
            "market_ctx": {"market_id": str(ft.get("market_id") or "btc_pulse_target"),
                           "question": ft.get("question") or "btc_pulse_directional"},
            "target_kind": "btc_pulse_focus", "reason": "btc_pulse_focus",
            "eligible_targets": eligible,
            "groups_analyzed": 1, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
            "news_linked_analyzed": 1 if str(ft.get("market_id")) in news_ids else 0,
            "advisory_features": {"advisory_target_kind": "btc_pulse_focus",
                                  "btc_signal_confidence": float(
                                      ft.get("confidence", 0.0) or 0.0),
                                  "asset": ft.get("asset"), "advisory_only": True,
                                  "affects_execution": False},
        }
    if focus_only:
        # focus mode + every focus target on cooldown (or none this tick): research NOTHING
        # rather than spend the budget off the pulse lane.
        return {"market_ctx": None, "target_kind": None,
                "reason": "btc_focus_only_no_target", "eligible_targets": eligible,
                "groups_analyzed": 0, "near_misses_analyzed": 0,
                "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
                "news_linked_analyzed": 0, "advisory_features": {}}

    # 0) STRONGEST Grok-flagged Bregman candidate (research what Grok itself flagged).
    strong = [c for c in grok_candidates
              if float(c.get("candidate_score", 0.0)) >= float(min_candidate_score)]
    if strong:
        strong.sort(key=lambda c: float(c.get("candidate_score", 0.0)), reverse=True)
        c = strong[0]
        gid = c.get("group_id")
        return {
            "market_ctx": {"market_id": str(gid or "grok_bregman_candidate"),
                           "question": "grok_flagged_bregman_candidate",
                           "group_ids": [gid], "market_ids": list(c.get("market_ids", []) or [])},
            "target_kind": "grok_bregman_candidate",
            "reason": "grok_flagged_bregman_candidate", "eligible_targets": eligible,
            "groups_analyzed": 1, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0 if c.get("complete") else 1,
            "malformed_groups_analyzed": 0, "news_linked_analyzed": 0,
            "advisory_features": {"advisory_target_kind": "grok_bregman_candidate",
                                  "grok_candidate_score": float(c.get("candidate_score", 0.0)),
                                  "grok_candidate_incoherence": float(c.get("incoherence", 0.0)),
                                  "grok_candidate_disagreement": float(
                                      c.get("grok_disagreement", 0.0)),
                                  "advisory_only": True},
        }

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

    # 1b) HIGHEST value-of-information market (#5): uncertain + near-threshold + liquid —
    # where a bounded Grok call most reduces edge uncertainty. Above news/liquidity.
    strong_voi = [v for v in voi_targets if float(v.get("voi", 0.0)) >= float(min_voi)]
    if strong_voi:
        strong_voi.sort(key=lambda v: float(v.get("voi", 0.0)), reverse=True)
        v = strong_voi[0]
        return {
            "market_ctx": {"market_id": str(v.get("market_id") or "voi_target"),
                           "question": v.get("question") or "high_value_of_information"},
            "target_kind": "value_of_information",
            "reason": "highest_value_of_information", "eligible_targets": eligible,
            "groups_analyzed": 1, "near_misses_analyzed": 0,
            "incomplete_groups_analyzed": 0, "malformed_groups_analyzed": 0,
            "news_linked_analyzed": 0,
            "advisory_features": {"advisory_target_kind": "value_of_information",
                                  "voi_score": float(v.get("voi", 0.0)),
                                  "ensemble_disagreement": float(
                                      v.get("ensemble_disagreement", 0.0)),
                                  "advisory_only": True},
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
