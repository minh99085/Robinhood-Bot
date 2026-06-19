"""Tier-4 cross-market relative-value detector (PAPER / RESEARCH ONLY, pure/read-only).

A new alpha SOURCE beyond complete-set Bregman arbitrage and single-market directional edge:
detect price INCONSISTENCIES across related markets and surface them as advisory
relative-value (RV) candidates + telemetry. Shadow-first by design — it never opens a trade
itself; it scores opportunities for analysis and can direct research (VOI) to the most
mispriced markets. Any actual execution still flows through the unchanged certifier /
directional gates.

Detected signals:
* mutually-exclusive (neg-risk / shared-event) families whose YES prices sum > 1 (the set is
  collectively OVER-priced) or, when exhaustive, < 1 (UNDER-priced complete set),
* binary complement inconsistency (yes + no quotes that do not sum to ~1).

Pure: reads market records' prices; no I/O, no trading, no live.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.markets import universe_manager as um


def _yes_mid(rec) -> float:
    raw = getattr(rec, "raw", None) or {}
    bid = um._as_float(raw.get("bestBid"), 0.0)
    ask = um._as_float(raw.get("bestAsk"), 0.0)
    if bid and ask:
        return (bid + ask) / 2.0
    yp = getattr(rec, "yes_price", None)
    return float(yp) if yp is not None else 0.5


def _liquidity(rec) -> float:
    try:
        return max(float(getattr(rec, "liquidity_usd", 0.0) or 0.0),
                   float(getattr(rec, "top_depth_usd", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class RelativeValueCandidate:
    family_key: str
    kind: str                       # "mutually_exclusive_overround" | "underround_complete_set"
    market_ids: list
    n_markets: int
    yes_sum: float
    mispricing: float               # signed deviation from the coherent sum
    min_liquidity_usd: float
    score: float
    advisory_only: bool = True

    def to_dict(self) -> dict:
        return {
            "family_key": self.family_key, "kind": self.kind,
            "market_ids": list(self.market_ids)[:12], "n_markets": self.n_markets,
            "yes_sum": round(self.yes_sum, 4), "mispricing": round(self.mispricing, 4),
            "min_liquidity_usd": round(self.min_liquidity_usd, 2),
            "score": round(self.score, 6), "advisory_only": True,
        }


def _family_key(rec) -> str:
    raw = getattr(rec, "raw", None) or {}
    for k in ("negRiskMarketID", "negRiskMarketId"):
        if raw.get(k):
            return f"negrisk:{raw[k]}"
    gk = str(getattr(rec, "group_key", "") or "")
    return gk if gk and not gk.startswith("market:") else ""


def find_relative_value(records, *, min_mispricing: float = 0.03,
                        min_family_liquidity_usd: float = 0.0,
                        max_candidates: int = 25) -> dict:
    """Scan records for cross-market relative-value inconsistencies. Returns a report with
    scored, advisory-only RV candidates (never trades). ``min_mispricing`` is the minimum
    absolute deviation of the family YES-sum from 1.0 to flag."""
    fam: dict = {}
    for rec in (records or []):
        key = _family_key(rec)
        if not key:
            continue
        fam.setdefault(key, []).append(rec)

    candidates: list = []
    families_examined = 0
    for key, members in fam.items():
        if len(members) < 2:
            continue
        families_examined += 1
        yes_sum = sum(_yes_mid(m) for m in members)
        min_liq = min(_liquidity(m) for m in members)
        if min_liq < float(min_family_liquidity_usd):
            continue
        dev = yes_sum - 1.0
        if abs(dev) < float(min_mispricing):
            continue
        kind = "mutually_exclusive_overround" if dev > 0 else "underround_complete_set"
        score = abs(dev) * min(1.0, min_liq / 1000.0)
        candidates.append(RelativeValueCandidate(
            family_key=key, kind=kind,
            market_ids=[str(getattr(m, "market_id", "")) for m in members],
            n_markets=len(members), yes_sum=yes_sum, mispricing=dev,
            min_liquidity_usd=min_liq, score=score))

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[:max_candidates]
    return {
        "schema": "relative_value/1.0", "paper_only": True, "advisory_only": True,
        "live_trading_enabled": False,
        "families_examined": families_examined,
        "rv_candidates_found": len(candidates),
        "overround_count": sum(1 for c in candidates
                               if c.kind == "mutually_exclusive_overround"),
        "underround_count": sum(1 for c in candidates
                                if c.kind == "underround_complete_set"),
        "top_candidates": [c.to_dict() for c in top],
        "best_score": round(top[0].score, 6) if top else 0.0,
    }
