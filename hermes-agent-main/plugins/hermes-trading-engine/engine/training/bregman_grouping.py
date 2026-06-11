"""Polymarket simplex grouping for Bregman arbitrage (deterministic, offline).

Quant scope — *Data Acquisition & Ingestion* + *Data Preprocessing & Feature
Engineering* + *Signal Generation*: organize Polymarket markets into the
mutually-exclusive / exhaustive outcome groups whose executable prices should
price onto a probability simplex. Group shapes supported:

* ``binary_yes_no``     — a single market's YES + NO outcome tokens.
* ``mutually_exclusive``— several markets that cannot all resolve YES.
* ``exhaustive_event``  — a mutually-exclusive group that is ALSO complete
                          (exactly one leg resolves YES, paying $1).
* ``linked_markets``    — markets linked by a shared Chainlink oracle feed.
* ``range_buckets``     — contiguous numeric range buckets of one quantity.
* ``synthetic_bundle``  — an explicitly-declared bundle of legs.

Only ``exhaustive`` + ``mutually_exclusive`` groups can be certified as
fully-hedged "buy the complete set" arbitrage. Everything here is pure data
shaping — no execution, no sizing, no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("hte.training.bregman_grouping")

GROUP_TYPES = ("binary_yes_no", "mutually_exclusive", "exhaustive_event",
               "linked_markets", "range_buckets", "synthetic_bundle")

_DEFAULT_TICK = 0.001


@dataclass
class SimplexLeg:
    """One executable leg (an outcome token that pays $1 if it resolves YES)."""

    market_id: str
    outcome: str
    token_id: str = ""
    ask: Optional[float] = None       # executable BUY price (best ask)
    bid: Optional[float] = None       # best bid (for spread checks)
    depth_usd: float = 0.0            # top-of-book executable (ASK-side) depth (USD)
    visible_ask_depth_usd: Optional[float] = None   # real CLOB ask-side depth (buy)
    visible_bid_depth_usd: Optional[float] = None   # real CLOB bid-side depth (sell)
    hydrated_from_clob: bool = False  # True when a REAL CLOB book populated this leg
    tick_size: float = _DEFAULT_TICK
    fresh_book: bool = True
    stale: bool = False
    tick_size_dirty: bool = False
    ambiguity_score: float = 0.0
    chainlink_no_trade: bool = False
    chainlink_relevant: bool = True   # True when unlinked OR linked+relevant
    synthetic_price: bool = False     # ask derived (e.g. 1 - bid), not a real book
    accepting_orders: bool = True     # False when the market has closed / halted
    book_age_s: Optional[float] = None  # age of the quote (drives stale-book score)

    @property
    def spread(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return max(0.0, float(self.ask) - float(self.bid))
        return None

    @property
    def executable(self) -> bool:
        return (self.ask is not None and self.ask > 0.0 and self.fresh_book
                and not self.stale and not self.tick_size_dirty)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


@dataclass
class SimplexGroup:
    """A set of legs that should price onto a probability simplex."""

    group_id: str
    group_type: str
    legs: list[SimplexLeg]
    mutually_exclusive: bool = True
    exhaustive: bool = True
    payout: float = 1.0               # winning leg pays this (USD per share)
    meta: dict = field(default_factory=dict)

    @property
    def observed_prices(self) -> list[float]:
        """Executable BUY prices per leg (0.0 when a leg has no executable ask)."""
        return [float(l.ask) if (l.ask is not None and l.ask > 0) else 0.0
                for l in self.legs]

    @property
    def implied_sum(self) -> float:
        return sum(self.observed_prices)

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "group_type": self.group_type,
            "mutually_exclusive": self.mutually_exclusive,
            "exhaustive": self.exhaustive, "payout": self.payout,
            "implied_sum": round(self.implied_sum, 6),
            "legs": [l.to_dict() for l in self.legs], "meta": dict(self.meta),
        }


def validate_simplex(group: SimplexGroup) -> tuple[bool, str]:
    """Validate a group's STRUCTURE for buy-the-complete-set certification.

    Returns ``(ok, reason)``. A group is structurally valid when it has at least
    two legs, no duplicate outcome tokens, and is both mutually-exclusive and
    exhaustive (so exactly one leg pays the payout). This is a structural check
    only — executability/cost checks live in the certification engine.
    """
    if group.group_type not in GROUP_TYPES:
        return False, f"unknown_group_type:{group.group_type}"
    if len(group.legs) < 2:
        return False, "insufficient_legs"
    tokens = [l.token_id or f"{l.market_id}:{l.outcome}" for l in group.legs]
    if len(set(tokens)) != len(tokens):
        return False, "duplicate_legs"
    if not group.mutually_exclusive:
        return False, "not_mutually_exclusive"
    if not group.exhaustive:
        return False, "not_exhaustive"
    if group.payout <= 0:
        return False, "non_positive_payout"
    return True, "ok"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _rec_attr(rec, name, default=None):
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def _best_ask(rec) -> Optional[float]:
    """Best ask via the ONE canonical price parser (no reference/mid fallback)."""
    from engine.arbitrage.price_parsing import parse_price
    raw = _rec_attr(rec, "raw", {}) or {}
    a = parse_price(raw.get("bestAsk"))
    return a if (a and a > 0) else None


def _best_bid(rec) -> Optional[float]:
    from engine.arbitrage.price_parsing import parse_price
    raw = _rec_attr(rec, "raw", {}) or {}
    b = parse_price(raw.get("bestBid"))
    return b if (b and b > 0) else None


def _fresh(rec, *, max_age_s: float = 30.0) -> bool:
    age = _rec_attr(rec, "book_age_s", None)
    bid, ask = _best_bid(rec), _best_ask(rec)
    if not (bid and ask):
        return False
    if age is not None and float(age) > max_age_s:
        return False
    return True


def build_binary_group(rec, *, no_ask: Optional[float] = None,
                       group_id: Optional[str] = None) -> SimplexGroup:
    """Build a YES/NO binary group from a single market record.

    The NO leg's executable ask uses ``no_ask`` when supplied (a real NO-token
    book); otherwise it is conservatively synthesized as ``1 - best_bid_yes``
    (buying NO ≈ selling YES at the YES bid) and flagged ``synthetic_price``.
    """
    market_id = str(_rec_attr(rec, "market_id", "") or "")
    tokens = list(_rec_attr(rec, "clob_token_ids", []) or [])
    yes_tok = tokens[0] if tokens else f"{market_id}:YES"
    no_tok = tokens[1] if len(tokens) > 1 else f"{market_id}:NO"
    yes_ask = _best_ask(rec)
    yes_bid = _best_bid(rec)
    fresh = _fresh(rec)
    depth = float(_rec_attr(rec, "top_depth_usd", 0.0) or 0.0)
    amb = 0.0
    raw = _rec_attr(rec, "raw", {}) or {}
    try:
        amb = float(raw.get("ambiguity")) if raw.get("ambiguity") not in (None, "") else 0.0
    except (TypeError, ValueError):
        amb = 0.0
    synthetic = no_ask is None
    no_price = no_ask if no_ask is not None else (
        (1.0 - yes_bid) if yes_bid is not None else None)
    book_age = _rec_attr(rec, "book_age_s", None)   # reconcile with MarketRecord
    legs = [
        SimplexLeg(market_id=market_id, outcome="YES", token_id=str(yes_tok),
                   ask=yes_ask, bid=yes_bid, depth_usd=depth, fresh_book=fresh,
                   stale=not fresh, ambiguity_score=amb, book_age_s=book_age),
        SimplexLeg(market_id=market_id, outcome="NO", token_id=str(no_tok),
                   ask=no_price, bid=None, depth_usd=depth, fresh_book=fresh,
                   stale=not fresh, ambiguity_score=amb, synthetic_price=synthetic,
                   book_age_s=book_age),
    ]
    return SimplexGroup(group_id=group_id or f"binary:{market_id}",
                        group_type="binary_yes_no", legs=legs,
                        mutually_exclusive=True, exhaustive=True,
                        meta=_group_meta([rec]))


def _market_question(rec) -> str:
    raw = _rec_attr(rec, "raw", {}) or {}
    return str(_rec_attr(rec, "question", None) or raw.get("question")
               or raw.get("title") or _rec_attr(rec, "title", "") or "")


def _declared_outcome_count(recs: list):
    for rec in recs:
        raw = _rec_attr(rec, "raw", {}) or {}
        oc = raw.get("outcomeCount") or raw.get("outcome_count")
        try:
            if oc is not None:
                return int(oc)
        except (TypeError, ValueError):
            continue
    return None


def _group_meta(recs: list) -> dict:
    """Read-only metadata carried on a group for completeness DIAGNOSTICS only
    (question text + declared outcome count). Never affects certification."""
    return {
        "question": _market_question(recs[0]) if recs else "",
        "outcome_count": _declared_outcome_count(recs),
        "leg_market_ids": [str(_rec_attr(r, "market_id", "") or "") for r in recs],
    }


def build_event_group(records: list, *, group_id: str, group_type: str = "exhaustive_event",
                      exhaustive: bool = True, mutually_exclusive: bool = True,
                      chainlink=None, now: Optional[float] = None) -> SimplexGroup:
    """Build a multi-outcome event group: each record contributes one YES leg.

    Exactly one outcome resolves YES (paying $1) when the group is exhaustive +
    mutually-exclusive — the classic "buy the complete set" Polymarket structure.
    """
    legs: list[SimplexLeg] = []
    for rec in records:
        market_id = str(_rec_attr(rec, "market_id", "") or "")
        tokens = list(_rec_attr(rec, "clob_token_ids", []) or [])
        yes_tok = tokens[0] if tokens else f"{market_id}:YES"
        fresh = _fresh(rec)
        raw = _rec_attr(rec, "raw", {}) or {}
        try:
            amb = float(raw.get("ambiguity")) if raw.get("ambiguity") not in (None, "") else 0.0
        except (TypeError, ValueError):
            amb = 0.0
        cl_no_trade, cl_relevant = False, True
        if chainlink is not None:
            try:
                sig = chainlink.signal_for_market(rec, now=now)
                cl_no_trade = bool(sig.no_trade and sig.feed_key is not None)
                cl_relevant = sig.feed_key is None or not sig.no_trade
            except Exception:  # noqa: BLE001 — chainlink must never break grouping
                logger.debug("chainlink relevance failed for %s", market_id, exc_info=True)
        legs.append(SimplexLeg(
            market_id=market_id, outcome="YES", token_id=str(yes_tok),
            ask=_best_ask(rec), bid=_best_bid(rec),
            depth_usd=float(_rec_attr(rec, "top_depth_usd", 0.0) or 0.0),
            fresh_book=fresh, stale=not fresh, ambiguity_score=amb,
            book_age_s=_rec_attr(rec, "book_age_s", None),
            chainlink_no_trade=cl_no_trade, chainlink_relevant=cl_relevant))
    return SimplexGroup(group_id=group_id, group_type=group_type, legs=legs,
                        mutually_exclusive=mutually_exclusive, exhaustive=exhaustive,
                        meta=_group_meta(records))


def _group_is_exhaustive(recs: list) -> bool:
    """Conservatively decide if an event group covers ALL outcomes.

    A "buy the complete set" hedge is only valid when the scanned legs are the
    FULL outcome set. We therefore require an explicit completeness signal — a
    ``negRiskComplete`` / ``exhaustive`` marker, or a leg count matching a
    declared ``outcomeCount`` — and default to ``False`` otherwise so an
    incomplete scan is never mislabelled as a full hedge.
    """
    n = len(recs)
    # explicit completeness markers (any member declaring the set complete proves it)
    _MARKERS = ("negRiskComplete", "neg_risk_complete", "exhaustive", "complete_set",
                "is_complete", "isComplete", "mece", "collectively_exhaustive")
    # declared-count fields; the set is complete when a declared outcome/market count
    # equals the number of grouped legs (never inferred from prices = no fabrication).
    _COUNTS = ("outcomeCount", "outcome_count", "marketCount", "market_count",
               "numOutcomes", "num_outcomes", "seriesLength", "series_length")
    for rec in recs:
        raw = _rec_attr(rec, "raw", {}) or {}
        if any(raw.get(m) for m in _MARKERS):
            return True
        for k in _COUNTS:
            try:
                v = raw.get(k)
                if v is not None and int(v) == n:
                    return True
            except (TypeError, ValueError):
                continue
        # event-level completeness: events[0] declares its full market/outcome count
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            ev = events[0]
            for k in _COUNTS:
                try:
                    v = ev.get(k)
                    if v is not None and int(v) == n:
                        return True
                except (TypeError, ValueError):
                    continue
            mkts = ev.get("markets")
            if isinstance(mkts, list) and mkts and len(mkts) == n:
                return True
    return False


def _is_fallback_key(gk: str, rec) -> bool:
    """True when a record's ``group_key`` is the per-market fallback (no explicit
    shared event id) — these orphans are candidates for normalized-family linking."""
    mid = str(_rec_attr(rec, "market_id", "") or "")
    return (not gk) or gk == mid or gk.startswith("market:")


def group_markets(records: list, *, chainlink=None, now: Optional[float] = None,
                  include_binary: bool = True, family_fallback: bool = True
                  ) -> list[SimplexGroup]:
    """Group market records into simplex groups by their event ``group_key``.

    Records sharing a non-degenerate ``group_key`` form a mutually-exclusive
    event group; it is marked ``exhaustive`` ONLY when a completeness signal is
    present (see :func:`_group_is_exhaustive`) so an incomplete scan can never be
    certified as a full hedge.

    When ``family_fallback`` is set, ORPHAN singletons (markets with only the
    per-market fallback key) are additionally linked by a normalized event-family
    key (slug/title/category/expiry — see :mod:`engine.training.bregman_text`) so
    sibling outcomes of the same event are grouped instead of scanned as isolated
    binaries. Family-linked groups are ``mutually_exclusive`` but stay
    ``exhaustive=False`` unless completeness is independently proven — grouping is
    improved, completeness is NEVER fabricated. Remaining singletons optionally
    become binary YES/NO groups.
    """
    by_key: dict[str, list] = {}
    orphans: list = []
    for rec in records:
        gk = str(_rec_attr(rec, "group_key", "") or _rec_attr(rec, "market_id", ""))
        if family_fallback and _is_fallback_key(gk, rec):
            orphans.append(rec)
        else:
            by_key.setdefault(gk, []).append(rec)

    # link orphans by normalized event-family key (improves discovery, not exhaustiveness)
    if family_fallback and orphans:
        from engine.training.bregman_text import event_family_key
        fam: dict[str, list] = {}
        for rec in orphans:
            try:
                fk = event_family_key(rec)
            except Exception:  # noqa: BLE001 — family inference must never break grouping
                fk = None
            key = fk if fk else f"market:{_rec_attr(rec, 'market_id', '') or id(rec)}"
            fam.setdefault(key, []).append(rec)
        for fk, recs in fam.items():
            by_key.setdefault(fk, []).extend(recs)
    elif orphans:
        for rec in orphans:
            gk = str(_rec_attr(rec, "group_key", "") or _rec_attr(rec, "market_id", ""))
            by_key.setdefault(gk, []).append(rec)

    groups: list[SimplexGroup] = []
    for gk, recs in by_key.items():
        if len(recs) >= 2:
            groups.append(build_event_group(
                recs, group_id=f"event:{gk}",
                group_type="exhaustive_event" if _group_is_exhaustive(recs) else "mutually_exclusive",
                exhaustive=_group_is_exhaustive(recs), mutually_exclusive=True,
                chainlink=chainlink, now=now))
        elif include_binary:
            groups.append(build_binary_group(recs[0], group_id=f"binary:{gk}"
                                             if gk and not gk.startswith("market:") else None))
    logger.debug("group_markets built %d groups from %d records", len(groups), len(records))
    return groups


def groups_from_graph(graph, records: list, *, chainlink=None, now: Optional[float] = None,
                      include_binary: bool = True) -> list[SimplexGroup]:
    """Build simplex groups from a :class:`MarketDependencyGraph`'s structural
    clusters (combinatorial Bregman grouping).

    Each same-event cluster becomes one event group whose legs are ALL cluster
    members (so a multi-leg hedge is assembled combinatorially rather than only
    from a single ``group_key``). A cluster is marked ``exhaustive`` only when the
    graph carries an EXHAUSTIVE edge among its members, so an incomplete event is
    never mislabelled as a full hedge. Singletons optionally become binary groups.
    """
    by_id = {}
    for r in records or []:
        mid = str(_rec_attr(r, "market_id", "") or "")
        if mid and mid not in by_id:
            by_id[mid] = r

    groups: list[SimplexGroup] = []
    for cluster in graph.same_event_groups():
        members = sorted(cluster)
        recs = [by_id[m] for m in members if m in by_id]
        if not recs:
            continue
        if len(recs) >= 2:
            exhaustive = graph.cluster_is_exhaustive(members)
            gk = str(_rec_attr(recs[0], "group_key", "") or members[0])
            groups.append(build_event_group(
                recs, group_id=f"event:{gk}",
                group_type="exhaustive_event" if exhaustive else "mutually_exclusive",
                exhaustive=exhaustive, mutually_exclusive=True,
                chainlink=chainlink, now=now))
        elif include_binary:
            groups.append(build_binary_group(recs[0]))
    return groups


def build_range_bucket_group(market_id: str, buckets: list[dict], *,
                             group_id: Optional[str] = None) -> SimplexGroup:
    """Build an exhaustive group from contiguous numeric range buckets.

    ``buckets``: list of ``{"label","ask","bid","depth_usd","token_id"}`` covering
    the full range of one quantity (exactly one bucket resolves YES).
    """
    legs = [SimplexLeg(
        market_id=market_id, outcome=str(b.get("label", f"bucket{i}")),
        token_id=str(b.get("token_id", f"{market_id}:b{i}")),
        ask=b.get("ask"), bid=b.get("bid"),
        depth_usd=float(b.get("depth_usd", 0.0) or 0.0),
        fresh_book=bool(b.get("fresh_book", True)),
        stale=bool(b.get("stale", False)),
        ambiguity_score=float(b.get("ambiguity_score", 0.0) or 0.0))
        for i, b in enumerate(buckets)]
    return SimplexGroup(group_id=group_id or f"range:{market_id}",
                        group_type="range_buckets", legs=legs,
                        mutually_exclusive=True, exhaustive=True)


def group_after_cost_edge(cost_per_set: float, *, payout: float = 1.0) -> float:
    """After-cost edge per complete set for a Bregman group: ``payout − cost_per_set``.

    Profitability-governor input (Bregman priority): a complete-set group is only
    worth trading when buying one share of every leg costs strictly less than the
    $1 payout AFTER the certifier's fee/slippage/tick-up loading. Pure helper."""
    return round(float(payout) - float(cost_per_set), 8)


def build_synthetic_bundle(group_id: str, legs: list[SimplexLeg], *,
                           exhaustive: bool = True,
                           mutually_exclusive: bool = True) -> SimplexGroup:
    """Wrap explicitly-declared legs as a synthetic Polymarket bundle."""
    return SimplexGroup(group_id=group_id, group_type="synthetic_bundle", legs=legs,
                        mutually_exclusive=mutually_exclusive, exhaustive=exhaustive)
