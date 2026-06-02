"""Polymarket event grouping for Bregman-style market structure.

Quant responsibility — *Bregman arbitrage market grouping* (structure detection
only; this module NEVER trades and NEVER constructs an executable arbitrage —
legacy cross-exchange arbitrage stays permanently disabled). It groups scanned
markets into correlated events so the ranker / probability stack can reason
about complete outcome sets:

* ``binary_complement``  — a single YES/NO market (two complementary tokens),
* ``mutually_exclusive`` — several markets in one event, at most one resolves YES,
* ``exhaustive``         — mutually-exclusive legs whose YES prices ~sum to 1,
* ``scalar_range``       — bucketed scalar/range outcomes (>=X, between A and B…),
* ``neg_risk``           — Polymarket neg-risk linked group,
* ``same_event``         — multiple linked markets we can't classify more tightly.

A group is *complete* when every leg exposes its CLOB tokens (so all legs are
executable in paper sim) and the structure is internally consistent. Bregman
suitability scores how usable a group is as a complete, tight, deep, clearly
resolving outcome set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .institutional_features import _as_float, _clamp, _log_scale, _SPREAD_REF, _DEPTH_FULL_USD

GROUP_TYPES = (
    "binary_complement", "mutually_exclusive", "exhaustive",
    "scalar_range", "neg_risk", "same_event",
)

# scalar/range phrasing — whole-word-ish so "between" matches but "betweenness"
# style false positives are unlikely on real questions.
_RANGE_RE = re.compile(
    r"\b(between|range|at least|at most|more than|less than|greater than|"
    r"fewer than|above|below|over/under|o/u|reach(?:es)?|exceed|scalar|"
    r"\d+\s*(?:to|-|–)\s*\d+)\b", re.IGNORECASE)

_EXHAUSTIVE_LO = 0.90
_EXHAUSTIVE_HI = 1.10


def _is_neg_risk(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    for k in ("negRisk", "neg_risk", "isNegRisk"):
        v = raw.get(k)
        if isinstance(v, bool) and v:
            return True
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"):
            return True
    return bool(raw.get("negRiskMarketID") or raw.get("negRiskMarketId"))


def _text(rec) -> str:
    raw = getattr(rec, "raw", None) or {}
    parts = [str(getattr(rec, "question", "") or ""),
             str(raw.get("groupItemTitle") or ""),
             str(raw.get("title") or "")]
    return " ".join(parts)


@dataclass
class EventGroup:
    """A detected correlated-market group.

    ``records`` preserves scan/rank order. ``leg_token_ids`` is the flattened
    set of CLOB tokens across legs (used to confirm every leg is executable).
    """

    group_key: str
    group_type: str
    records: list = field(default_factory=list)
    leg_token_ids: list = field(default_factory=list)
    yes_prices: list = field(default_factory=list)
    complete: bool = False
    all_tokens_available: bool = False
    n_legs: int = 0

    @property
    def market_ids(self) -> list:
        return [getattr(r, "market_id", "") for r in self.records]

    def to_dict(self) -> dict:
        return {
            "group_key": self.group_key, "group_type": self.group_type,
            "market_ids": self.market_ids, "n_legs": self.n_legs,
            "complete": self.complete,
            "all_tokens_available": self.all_tokens_available,
            "leg_token_count": len(self.leg_token_ids),
            "yes_price_sum": round(sum(p for p in self.yes_prices if p is not None), 4)
            if self.yes_prices else None,
        }


def _classify(records: list) -> str:
    """Pick the tightest group type that fits ``records``."""
    if any(_is_neg_risk(getattr(r, "raw", None) or {}) for r in records):
        return "neg_risk"
    if any(_RANGE_RE.search(_text(r)) for r in records):
        return "scalar_range"
    if len(records) == 1:
        rec = records[0]
        tokens = list(getattr(rec, "clob_token_ids", []) or [])
        if len(tokens) >= 2:
            return "binary_complement"
        return "same_event"
    prices = [_as_float(getattr(r, "yes_price", None)) for r in records]
    prices = [p for p in prices if p is not None]
    if len(prices) >= 2 and _EXHAUSTIVE_LO <= sum(prices) <= _EXHAUSTIVE_HI:
        return "exhaustive"
    return "mutually_exclusive"


def group_markets(records: list) -> list:
    """Group ranked ``MarketRecord``s into :class:`EventGroup`s by event key.

    Single markets become ``binary_complement`` (or ``same_event`` if they lack
    a complementary token pair). Grouping is stable: groups appear in the order
    their first member appears in ``records``.
    """
    order: list = []
    buckets: dict = {}
    for rec in records or []:
        key = str(getattr(rec, "group_key", None) or getattr(rec, "market_id", ""))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(rec)

    groups: list = []
    for key in order:
        recs = buckets[key]
        gtype = _classify(recs)
        leg_tokens: list = []
        all_tokens = True
        for r in recs:
            toks = [str(t) for t in (getattr(r, "clob_token_ids", []) or []) if t]
            if not toks:
                all_tokens = False
            leg_tokens.extend(toks)
        yes_prices = [_as_float(getattr(r, "yes_price", None)) for r in recs]

        if gtype == "binary_complement":
            complete = len(set(leg_tokens)) >= 2
        else:
            complete = all_tokens and len(recs) >= 2
        groups.append(EventGroup(
            group_key=key, group_type=gtype, records=list(recs),
            leg_token_ids=list(dict.fromkeys(leg_tokens)), yes_prices=yes_prices,
            complete=bool(complete), all_tokens_available=bool(all_tokens),
            n_legs=len(recs)))
    return groups


def bregman_suitability(group: EventGroup, *, oracle_relevance: float = 0.0) -> float:
    """Score (``0..1``) how usable ``group`` is as a Bregman outcome set.

    Weighted blend of: complete outcome group, all-leg token availability,
    executable depth, tight spread, settlement clarity, and (bounded) oracle
    relevance. Higher = a cleaner, more complete, more executable group.
    """
    recs = group.records or []
    if not recs:
        return 0.0
    spreads = [_as_float(getattr(r, "spread", None)) for r in recs]
    spreads = [s for s in spreads if s is not None]
    depths = [_as_float(getattr(r, "top_depth_usd", None)) or 0.0 for r in recs]
    ambig = []
    for r in recs:
        raw = getattr(r, "raw", None) or {}
        a = _as_float(raw.get("ambiguity"))
        if a is None:
            a = 0.0 if getattr(r, "has_resolution_text", False) else 0.5
        ambig.append(_clamp(a))

    tight_spread = (sum(_clamp(1.0 - s / _SPREAD_REF) for s in spreads) / len(spreads)
                    if spreads else 0.0)
    exec_depth = (sum(_log_scale(d, _DEPTH_FULL_USD) or 0.0 for d in depths) / len(depths)
                  if depths else 0.0)
    settlement_clarity = 1.0 - (sum(ambig) / len(ambig) if ambig else 0.5)

    score = (0.30 * (1.0 if group.complete else 0.0)
             + 0.20 * (1.0 if group.all_tokens_available else 0.0)
             + 0.20 * exec_depth
             + 0.20 * tight_spread
             + 0.10 * settlement_clarity)
    score = score + 0.10 * _clamp(oracle_relevance)
    return round(_clamp(score), 4)


def grouping_metrics(records: list, groups: Optional[list] = None) -> dict:
    """Group-coverage + structure stats over a scanned/ranked record set.

    ``group_coverage`` is the fraction of records that sit inside a *complete*
    group (binary complement with both tokens, or a multi-leg group with all
    legs executable) — the markets Bregman reasoning can actually use.
    """
    recs = list(records or [])
    groups = groups if groups is not None else group_markets(recs)
    n = len(recs)
    in_complete = 0
    by_type: dict = {t: 0 for t in GROUP_TYPES}
    complete_groups = 0
    for g in groups:
        by_type[g.group_type] = by_type.get(g.group_type, 0) + 1
        if g.complete:
            complete_groups += 1
            in_complete += g.n_legs
    return {
        "records": n,
        "groups_detected": len(groups),
        "complete_groups": complete_groups,
        "group_coverage": round(in_complete / n, 4) if n else 0.0,
        "by_type": by_type,
    }


def detection_precision(groups: list, labels: dict) -> float:
    """Precision of detected group types vs a ``{group_key: expected_type}`` map.

    Only groups whose key appears in ``labels`` are scored. Returns 1.0 when the
    label set is empty (nothing to disprove).
    """
    scored = [g for g in groups if g.group_key in labels]
    if not scored:
        return 1.0
    correct = sum(1 for g in scored if g.group_type == labels[g.group_key])
    return round(correct / len(scored), 4)
