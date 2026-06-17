"""Grok-driven Bregman candidate GENERATION (research-only; never certifies/trades).

Quant scope — *Signal Generation*: use Grok's per-leg probabilities to FLAG market
groups that look mispriced/incoherent (a possible after-cost arbitrage), so the
deterministic Bregman certifier evaluates/prioritizes them. Grok only PROPOSES; the
certifier still PROVES completeness + valid simplex + depth + a positive after-cost
lower bound. An incomplete / not-exhaustive family is NEVER tradeable here.

Pure + deterministic. Two independent candidate signals are combined:

* ``incoherence``     — structural: for a complete MECE set, the sum of executable
  ask prices vs the $1 payout. ``sum_asks < 1`` => a buy-all set could pay $1 for
  less than $1 (a real arb *candidate*, still to be certified).
* ``grok_disagreement`` — Grok's view: mean |market_price - grok_prob| across legs
  (Grok thinks the set is mispriced even if asks look coherent).

Neither is tradeable; both only rank what to scan / research next.
"""

from __future__ import annotations

from typing import Optional


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _leg_price(leg: dict) -> float:
    for k in ("ask", "executable_price", "price", "best_ask"):
        v = leg.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _leg_depth(leg: dict) -> float:
    for k in ("ask_depth", "depth_usd", "top_depth_usd", "depth"):
        v = leg.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def group_incoherence(legs: list) -> float:
    """Structural arb candidate signal for a (claimed) MECE set: ``1 - sum(asks)``
    when positive (buy-all pays $1 for < $1), else 0. Read-only candidate hint."""
    if not legs:
        return 0.0
    total = sum(_leg_price(l) for l in legs)
    return round(max(0.0, 1.0 - total), 6)


def grok_disagreement(legs: list) -> float:
    """Mean |market_price - grok_prob| across legs that carry a ``grok_prob``; 0 when
    no Grok probabilities are present. Grok's "this is mispriced" signal in [0,1]."""
    diffs = []
    for l in legs:
        gp = l.get("grok_prob")
        if gp is None:
            continue
        diffs.append(abs(_clamp01(gp) - _leg_price(l)))
    return round(sum(diffs) / len(diffs), 6) if diffs else 0.0


def score_group(group: dict) -> dict:
    """Score one candidate group. Returns the ranking score + components + a reason.
    NOT a tradeable signal — ``tradeable`` is always False (certification decides)."""
    legs = group.get("legs", []) or []
    inc = group_incoherence(legs)
    dis = grok_disagreement(legs)
    min_depth = min((_leg_depth(l) for l in legs), default=0.0)
    liq = _clamp01(min_depth / 500.0)              # normalized depth (0..1)
    complete = bool(group.get("complete", group.get("completeness_proven", False)))
    # candidate strength: structural incoherence dominates, Grok disagreement adds,
    # scaled by liquidity and a completeness prior (complete families are likelier
    # to actually certify). Pure ranking — never implies a tradeable edge.
    completeness_prior = 1.0 if complete else 0.5
    score = round((inc + 0.5 * dis) * (0.5 + 0.5 * liq) * completeness_prior, 6)
    reason = ("incoherent_sum_asks" if inc > 0 else
              ("grok_disagrees_with_market" if dis > 0 else "no_signal"))
    return {
        "group_id": group.get("group_id") or group.get("id"),
        "candidate_score": score,
        "incoherence": inc,
        "grok_disagreement": dis,
        "min_leg_depth_usd": round(min_depth, 2),
        "complete": complete,
        "reason": reason,
        "tradeable": False,                        # certification decides — never here
        "advisory_only": True,
    }


def rank_candidates(groups: list, *, top_n: int = 10,
                    min_score: float = 1e-6) -> list:
    """Score + rank candidate groups (descending). Drops zero-signal groups. Pure."""
    scored = [score_group(g) for g in (groups or [])]
    scored = [s for s in scored if s["candidate_score"] > min_score]
    scored.sort(key=lambda s: s["candidate_score"], reverse=True)
    return scored[: max(0, int(top_n))]


def summarize(groups: list, *, certified_ids: Optional[set] = None,
              top_n: int = 10) -> dict:
    """Candidate summary for telemetry: proposed count, top candidates, and how many
    Grok-flagged candidates the CERTIFIER actually certified (cross-referenced —
    proves Grok proposals are validated, not trusted)."""
    ranked = rank_candidates(groups, top_n=top_n)
    cids = certified_ids or set()
    certified = sum(1 for s in ranked if s["group_id"] in cids)
    return {
        "grok_bregman_candidates_proposed": len(ranked),
        "grok_bregman_candidates_certified": certified,
        "grok_bregman_candidates_uncertified": len(ranked) - certified,
        "grok_bregman_top_candidates": ranked,
        "certification_unchanged": True,            # certifier still the only gate
        "advisory_only": True,
    }
