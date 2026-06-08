"""Bregman/ABCAS near-miss diagnostics (read-only, NON-EXECUTING).

For every REJECTED Bregman group this module computes a structured diagnostic that
explains *how close* the group was to being a certified complete-set arbitrage and
*exactly why* the certifier said no — per-leg depth, book freshness, spread,
outcome completeness, an invalid-simplex breakdown, and a bounded near-miss score.

It NEVER trades, sizes, lowers a gate, refreshes thresholds, or relaxes
certification. It only records WHY the strict certifier rejected a group so paper
training can scan smarter and label better. All functions are pure + deterministic.
"""

from __future__ import annotations

import statistics
from typing import Optional

from engine.training.bregman_text import (classify_market_kind, infer_outcome_label,
                                          normalize_text)

# A near-miss "fix category" — exactly which strict gate stood in the way.
FIX_DEPTH = "depth"
FIX_STALE = "stale_book"
FIX_SPREAD = "spread"
FIX_EXHAUSTIVE = "not_exhaustive"
FIX_SIMPLEX = "invalid_simplex"
FIX_AMBIGUITY = "settlement_ambiguity"
FIX_EDGE = "no_positive_edge"
FIX_OTHER = "other"

_REASON_TO_FIX = {
    "depth_too_thin": FIX_DEPTH,
    "stale_book": FIX_STALE,
    "spread_too_wide": FIX_SPREAD,
    "not_exhaustive": FIX_EXHAUSTIVE,
    "not_mutually_exclusive": FIX_EXHAUSTIVE,
    "invalid_simplex": FIX_SIMPLEX,
    "duplicate_legs": FIX_SIMPLEX,
    "insufficient_legs": FIX_SIMPLEX,
    "settlement_ambiguity": FIX_AMBIGUITY,
    "no_positive_edge": FIX_EDGE,
    "no_executable_price": FIX_DEPTH,
}


def _legs(group) -> list:
    return list(getattr(group, "legs", None) or [])


def depth_quality(group, *, min_depth_usd: float) -> dict:
    """Per-leg depth diagnostic (read-only). Reports min/median/worst leg depth,
    thin-leg count, executable notional at the touch, and whether ONE leg or MANY
    legs are below the strict ``min_depth_usd`` (which is NOT lowered here)."""
    legs = _legs(group)
    depths = [float(getattr(l, "depth_usd", 0.0) or 0.0) for l in legs]
    if not depths:
        return {"min_leg_depth_usd": 0.0, "median_leg_depth_usd": 0.0,
                "worst_leg_depth_usd": 0.0, "thin_legs": 0, "total_legs": 0,
                "executable_notional_usd": 0.0, "worst_leg_market_id": None,
                "thin_cause": "no_legs", "required_depth_usd": float(min_depth_usd)}
    worst_i = min(range(len(depths)), key=lambda i: depths[i])
    thin = [i for i, d in enumerate(depths) if d < float(min_depth_usd)]
    notional = 0.0
    for l in legs:
        ask = getattr(l, "ask", None)
        if ask and ask > 0:
            notional += min(float(getattr(l, "depth_usd", 0.0) or 0.0),
                            float(getattr(l, "depth_usd", 0.0) or 0.0))
    return {
        "min_leg_depth_usd": round(min(depths), 4),
        "median_leg_depth_usd": round(statistics.median(depths), 4),
        "worst_leg_depth_usd": round(depths[worst_i], 4),
        "thin_legs": len(thin),
        "total_legs": len(depths),
        "executable_notional_usd": round(notional, 4),
        "worst_leg_market_id": str(getattr(legs[worst_i], "market_id", "") or ""),
        "thin_cause": ("none" if not thin else
                       "one_leg" if len(thin) == 1 else "many_legs"),
        "required_depth_usd": float(min_depth_usd),
    }


def freshness_quality(group, *, max_age_s: float, refresh_attempted: bool = False,
                      refresh_ok: bool = False,
                      refresh_reason: Optional[str] = None) -> dict:
    """Per-leg book-freshness diagnostic. Records stale legs, worst book age, the
    strict freshness threshold, and whether a refresh was attempted/succeeded
    (freshness itself is NEVER loosened)."""
    legs = _legs(group)
    ages = [float(getattr(l, "book_age_s", 0.0) or 0.0) for l in legs]
    stale = [l for l in legs if getattr(l, "stale", False)
             or not getattr(l, "fresh_book", True)]
    worst_i = max(range(len(ages)), key=lambda i: ages[i]) if ages else None
    return {
        "stale_legs": len(stale),
        "total_legs": len(legs),
        "worst_leg_age_s": round(max(ages), 4) if ages else None,
        "worst_leg_market_id": (str(getattr(legs[worst_i], "market_id", "") or "")
                                if worst_i is not None else None),
        "freshness_threshold_s": float(max_age_s),
        "refresh_attempted": bool(refresh_attempted),
        "refresh_ok": bool(refresh_ok),
        "refresh_reason": refresh_reason,
    }


def spread_quality(group, *, max_spread: float) -> dict:
    legs = _legs(group)
    spreads = [float(l.spread) for l in legs if getattr(l, "spread", None) is not None]
    return {
        "max_leg_spread": round(max(spreads), 6) if spreads else None,
        "median_leg_spread": round(statistics.median(spreads), 6) if spreads else None,
        "spread_threshold": float(max_spread),
        "wide_legs": sum(1 for s in spreads if s > float(max_spread)),
    }


def simplex_diagnostic(group) -> dict:
    """Probability-simplex breakdown for ``invalid_simplex`` / completeness debugging.

    Reports the sum of executable prices (≈ implied probability mass), per-leg
    probabilities, outcome labels, duplicate outcomes, whether a binary complement
    is missing, whether normalization is invalid (sum ≪ or ≫ payout), and whether
    the failure looks like a PARSING issue (e.g. zero/empty prices) versus TRUE
    invalid economics. Diagnostic only — never forces an invalid group through."""
    legs = _legs(group)
    payout = float(getattr(group, "payout", 1.0) or 1.0)
    prices = [float(getattr(l, "ask", 0.0) or 0.0) for l in legs]
    labels = [str(getattr(l, "outcome", "") or "") for l in legs]
    tokens = [str(getattr(l, "token_id", "") or f"{getattr(l, 'market_id', '')}:"
                  f"{getattr(l, 'outcome', '')}") for l in legs]
    psum = sum(p for p in prices if p > 0)
    n_priced = sum(1 for p in prices if p > 0)
    # Duplicate detection keys on (market_id, outcome) / token — NOT the bare outcome
    # label, since every leg of a multi-market event group is legitimately "YES".
    mkt_outcomes = [f"{getattr(l, 'market_id', '')}:{getattr(l, 'outcome', '')}"
                    for l in legs]
    dup_tokens = len(tokens) != len(set(tokens))
    dup_outcomes = len(mkt_outcomes) != len(set(mkt_outcomes))
    missing_complement = (getattr(group, "group_type", "") == "binary_yes_no"
                          and n_priced < 2)
    # invalid normalization: priced legs exist but mass is far from the [.. payout]
    # band that a coherent complete set must straddle.
    invalid_norm = bool(n_priced >= 2 and (psum <= 0.0 or psum > payout * 3.0))
    parsing_suspected = bool(n_priced < len(legs))  # some legs failed to price
    return {
        "sum_of_probabilities": round(psum, 6),
        "payout": payout,
        "leg_probabilities": [round(p, 6) for p in prices],
        "outcome_labels": labels,
        "priced_legs": n_priced,
        "total_legs": len(legs),
        "duplicate_outcomes": bool(dup_outcomes or dup_tokens),
        "missing_complement": bool(missing_complement),
        "invalid_normalization": invalid_norm,
        "suspected_parsing_issue": parsing_suspected,
        "true_invalid_economics": bool(invalid_norm and not parsing_suspected),
    }


def completeness_diagnostic(group) -> dict:
    """Outcome-completeness diagnostic for ``not_exhaustive`` rejections.

    Records the group key, the inferred outcome FAMILY/kind (binary / multi_way /
    range / winner_take_all / ambiguous), observed outcome labels, any declared
    expected outcome count, and the reason completeness could not be PROVEN.
    Completeness is never fabricated — an unproven set stays rejected."""
    legs = _legs(group)
    meta = dict(getattr(group, "meta", {}) or {})
    question = meta.get("question") or meta.get("title") or ""
    observed = [infer_outcome_label(getattr(l, "outcome", ""),
                                    [getattr(l, "outcome", "")]) for l in legs]
    kind = classify_market_kind(question, n_legs=len(legs),
                                outcomes=[getattr(l, "outcome", "") for l in legs])
    expected = meta.get("outcome_count") or meta.get("expected_outcomes")
    try:
        expected = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected = None
    proven = bool(getattr(group, "exhaustive", False))
    missing = None
    if expected is not None and expected > len(legs):
        missing = expected - len(legs)
    if proven:
        reason = "completeness_proven"
    elif expected is not None:
        reason = "declared_outcome_count_exceeds_observed_legs"
    else:
        reason = "no_explicit_completeness_marker_negRiskComplete_or_outcomeCount"
    return {
        "group_key": str(getattr(group, "group_id", "") or ""),
        "expected_outcome_family": normalize_text(question) or None,
        "market_kind": kind,
        "observed_outcomes": observed,
        "observed_count": len(legs),
        "declared_expected_count": expected,
        "missing_or_unknown_outcomes": missing,
        "completeness_proven": proven,
        "reason_incomplete": None if proven else reason,
    }


def after_cost_lower_bound(group) -> Optional[float]:
    """Rough, NON-EXECUTING after-cost lower-bound proxy: ``payout − implied_sum``
    when every leg is priced. Positive → potential edge if all gates passed.
    Returns ``None`` when the group is not fully priced (cannot bound)."""
    legs = _legs(group)
    prices = [float(getattr(l, "ask", 0.0) or 0.0) for l in legs]
    if not prices or any(p <= 0 for p in prices):
        return None
    return round(float(getattr(group, "payout", 1.0) or 1.0) - sum(prices), 6)


def analyze_rejection(group, reason: str, *, min_depth_usd: float,
                      max_spread: float, max_age_s: float,
                      refresh_attempted: bool = False, refresh_ok: bool = False,
                      refresh_reason: Optional[str] = None) -> dict:
    """Full near-miss diagnostic for one rejected group. Read-only; computes a
    bounded ``near_miss_score`` in ``[0, 1]`` (higher = closer to executable) and
    classification flags. Does NOT execute, size, or relax any gate."""
    fix = _REASON_TO_FIX.get(reason, FIX_OTHER)
    dq = depth_quality(group, min_depth_usd=min_depth_usd)
    fq = freshness_quality(group, max_age_s=max_age_s,
                           refresh_attempted=refresh_attempted, refresh_ok=refresh_ok,
                           refresh_reason=refresh_reason)
    sq = spread_quality(group, max_spread=max_spread)
    sx = simplex_diagnostic(group)
    comp = completeness_diagnostic(group)
    alb = after_cost_lower_bound(group)

    # component confidences in [0,1] (1.0 = that dimension is fully satisfied).
    depth_ok = dq["thin_legs"] == 0
    fresh_ok = fq["stale_legs"] == 0
    spread_ok = (sq["wide_legs"] == 0)
    complete_ok = comp["completeness_proven"]
    simplex_ok = not (sx["duplicate_outcomes"] or sx["invalid_normalization"]
                      or sx["missing_complement"])
    completeness_conf = 1.0 if complete_ok else (0.5 if comp["declared_expected_count"]
                                                 else 0.2)
    depth_conf = 1.0 if depth_ok else max(0.0, min(1.0,
                 dq["min_leg_depth_usd"] / max(1e-9, dq["required_depth_usd"])))
    fresh_conf = 1.0 if fresh_ok else (0.5 if fq["refresh_attempted"] else 0.0)
    spread_conf = 1.0 if spread_ok else 0.3
    edge_conf = 1.0 if (alb is not None and alb > 0) else (0.5 if alb is not None else 0.0)
    score = round(0.30 * completeness_conf + 0.25 * depth_conf + 0.15 * fresh_conf
                  + 0.10 * spread_conf + 0.20 * edge_conf, 6)

    blockers = [b for b, ok in (
        (FIX_EXHAUSTIVE, complete_ok), (FIX_SIMPLEX, simplex_ok),
        (FIX_DEPTH, depth_ok), (FIX_STALE, fresh_ok), (FIX_SPREAD, spread_ok))
        if not ok]
    one_fix_away = len(blockers) == 1
    return {
        "group_key": str(getattr(group, "group_id", "") or ""),
        "group_type": str(getattr(group, "group_type", "") or ""),
        "raw_market_ids": [str(getattr(l, "market_id", "") or "") for l in _legs(group)],
        "reject_reason": reason,
        "fix_category": fix,
        "near_miss_score": score,
        "one_fix_away": one_fix_away,
        "remaining_blockers": blockers,
        "depth_quality": dq,
        "freshness": fq,
        "spread_quality": sq,
        "simplex": sx,
        "completeness": comp,
        "after_cost_lower_bound": alb,
        "advisory_only": True,
        "executed": False,
        "trade_gate_bypassed": False,
    }


def rank_near_misses(items: list, *, top_n: int = 10) -> list:
    """Sort near-miss diagnostics by descending closeness (score, then one-fix-away,
    then positive edge). Pure — does not mutate the input."""
    def key(it):
        return (float(it.get("near_miss_score", 0.0)),
                1 if it.get("one_fix_away") else 0,
                1 if (it.get("after_cost_lower_bound") or 0) > 0 else 0)
    return sorted(items, key=key, reverse=True)[:max(0, int(top_n))]


def summarize(items: list, *, top_n: int = 10) -> dict:
    """Aggregate near-miss metrics for the (light) report. Diagnostic only."""
    by_reason: dict = {}
    one_fix = depth_only = not_exhaustive = stale_refresh_failed = 0
    for it in items:
        r = it.get("reject_reason", "unknown")
        by_reason[r] = by_reason.get(r, 0) + 1
        if it.get("one_fix_away"):
            one_fix += 1
        blk = it.get("remaining_blockers", [])
        if blk == [FIX_DEPTH]:
            depth_only += 1
        if it.get("reject_reason") in ("not_exhaustive", "not_mutually_exclusive"):
            not_exhaustive += 1
        fq = it.get("freshness", {}) or {}
        if (it.get("reject_reason") == "stale_book" and fq.get("refresh_attempted")
                and not fq.get("refresh_ok")):
            stale_refresh_failed += 1
    # ranking buckets (each is diagnostic only — NEVER implies a tradeable edge).
    def _top(key, n=5, predicate=None):
        pool = [it for it in items if (predicate is None or predicate(it))]
        return sorted(pool, key=key, reverse=True)[:n]

    lbs = [it.get("after_cost_lower_bound") for it in items
           if it.get("after_cost_lower_bound") is not None]
    all_negative = bool(lbs) and all(v <= 0 for v in lbs)
    return {
        "bregman_near_misses_total": len(items),
        "bregman_top_near_misses": rank_near_misses(items, top_n=top_n),
        "near_miss_by_rejection_reason": dict(sorted(by_reason.items())),
        "near_miss_one_fix_away_count": one_fix,
        "near_miss_depth_only_count": depth_only,
        "near_miss_not_exhaustive_count": not_exhaustive,
        "near_miss_stale_refresh_failed_count": stale_refresh_failed,
        # ranking buckets (diagnostic only; none of these are tradeable)
        "near_miss_buckets": {
            "top_by_depth_quality": _top(
                lambda it: it.get("depth_quality", {}).get("min_leg_depth_usd", 0.0)),
            "top_by_completeness_confidence": _top(
                lambda it: 1 if it.get("completeness", {}).get("completeness_proven") else 0),
            "top_by_after_cost_lower_bound": _top(
                lambda it: (it.get("after_cost_lower_bound") or -1e9)),
            "top_by_one_fix_away": _top(lambda it: it.get("near_miss_score", 0.0),
                                        predicate=lambda it: it.get("one_fix_away")),
            "top_by_grok_news_relevance": _top(
                lambda it: float((it.get("advisory_features") or {}).get(
                    "grok_news_relevance_score", 0.0))),
        },
        "near_miss_all_negative_after_cost_lower_bound": all_negative,
        "near_miss_tradeable_count": 0,        # diagnostics NEVER tradeable
        "near_miss_note": ("all near-misses have non-positive after-cost lower bound; "
                           "none are tradeable" if all_negative else
                           "near-misses are diagnostic only and are never executed"),
    }
