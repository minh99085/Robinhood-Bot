"""Polymarket constraint discovery (PAPER ONLY, pure, deterministic).

Turns Polymarket market metadata + order books into typed constraint groups that
Bregman can certify. It discovers:

* **binary complement** — a single YES/NO market (sum == 1),
* **mutually exclusive** — same-event outcomes where at most one is true,
* **collectively exhaustive** — same-event outcomes where at least one is true,
* **MECE** — a partition (exactly one true, e.g. neg-risk multi-candidate),
* **range buckets** — disjoint numeric ranges of one variable (exactly one true),
* **scalar-threshold** — monotone ``>= K`` markets → implication chain,
* **hierarchy / parent-child** — children imply the parent.

It is **conservative**: when metadata is insufficient to prove a relationship it
emits ``INSUFFICIENT_METADATA`` rather than inventing a constraint. Every market
that cannot join a group carries a typed skip reason.

Bregman receives **normalized executable prices** per outcome — best bid/ask,
midpoint, spread, depth (USD + shares), fee (bps), and the book timestamp +
staleness — via :class:`NormalizedQuote`.

Quant responsibilities (full chain)
-----------------------------------
* **Data acquisition & ingestion** — read-only market metadata + books (polymarket
  gamma/CLOB v2 shape) are passed in; this module performs no I/O.
* **Preprocessing / features** — normalizes quotes + clusters markets by event;
  records metadata/book coverage + typed skips.
* **Statistical / probabilistic modeling** — calibrated probabilities may rank
  groups upstream; discovery itself is metadata-driven, not model-driven.
* **Bregman signal generation** — emits typed constraint groups + executable
  quotes for projection/certification (Bregman priority).
* **Risk / portfolio** — stale/illiquid groups are flagged so risk can veto.
* **Backtesting / simulation** — discovery metrics feed the audit + benchmarks.
* **Robustness** — same-event clustering avoids correlated double-counting.
* **CLOB v2 execution** — quotes carry depth + spread for executable sizing.
* **Live monitoring** — group-type counts, coverage, and skip reasons are emitted
  every cycle.
* **Compliance / security / ops** — PAPER-only; never invents constraints; no
  wallet/order path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .constraint_graph import (
    _PMAX,
    _PMIN,
    ConstraintGraph,
    Outcome,
    RelationType,
    SKIP_DEGENERATE_PRICE,
    SKIP_INSUFFICIENT_OUTCOMES,
    SKIP_MARKET_INACTIVE,
    SKIP_MISSING_QUOTES,
    SKIP_NO_DEPTH,
    SKIP_NO_ORDERBOOK,
    SKIP_NON_NUMERIC_PRICE,
    _is_active,
    _to_float,
    parse_list_field,
)

logger = logging.getLogger("hte.arbitrage.discovery")

# Paper-only reference depth (USD) when a market exposes no book depth, so a valid
# market is still normalized for training rather than dropped as NO_DEPTH.
DEFAULT_PAPER_DEPTH_USD = 100.0

# Additional typed skip reasons specific to discovery.
SKIP_INSUFFICIENT_METADATA = "insufficient_metadata"
SKIP_MALFORMED_GROUP = "malformed_group"
SKIP_UNKNOWN_GROUP_KIND = "unknown_group_kind"

DISCOVERY_SKIP_REASONS = frozenset({
    SKIP_MARKET_INACTIVE, SKIP_NO_ORDERBOOK, SKIP_INSUFFICIENT_OUTCOMES,
    SKIP_NON_NUMERIC_PRICE, SKIP_MISSING_QUOTES, SKIP_NO_DEPTH,
    SKIP_DEGENERATE_PRICE, SKIP_INSUFFICIENT_METADATA, SKIP_MALFORMED_GROUP,
    SKIP_UNKNOWN_GROUP_KIND,
    # precise outcome diagnostics (non-contradictory)
    "non_numeric_outcome_prices", "outcome_price_count_mismatch",
    "missing_outcome_prices", "duplicate_outcome_labels",
    "incomplete_multway_family", "ambiguous_multway_group",
})

# Explicit market-level group kinds -> graph relation + builder.
_KIND_TO_RELATION = {
    "mutually_exclusive": RelationType.MUTUALLY_EXCLUSIVE,
    "collectively_exhaustive": RelationType.COLLECTIVELY_EXHAUSTIVE,
    "mece": RelationType.MECE,
    "range": RelationType.RANGE,
    "scalar_threshold": RelationType.CROSS_MARKET_IMPLIES,
    "hierarchy": RelationType.HIERARCHY,
    "complement": RelationType.COMPLEMENT,
}


@dataclass
class NormalizedQuote:
    """Normalized executable price for one outcome (what Bregman consumes)."""

    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    depth_usd: float
    depth_shares: float
    fee_bps: float
    ts_ms: Optional[int]
    stale: bool

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class DiscoveredGroup:
    """A typed constraint group discovered from market metadata + books."""

    group_id: str
    event_id: str
    relation: str            # RelationType value
    outcome_ids: list
    n_outcomes: int
    source: str              # binary | explicit | negrisk | range | scalar_threshold | hierarchy
    quotes: dict = field(default_factory=dict)   # outcome_id -> NormalizedQuote
    # structured identity (prevents the confusing raw_market_ids=['x','x'] for a
    # single market's YES/NO pair): distinct market ids vs distinct outcome/token ids.
    market_ids: list = field(default_factory=list)
    outcome_labels: list = field(default_factory=list)
    outcome_prices: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"group_id": self.group_id, "event_id": self.event_id,
                "relation": self.relation, "outcome_ids": list(self.outcome_ids),
                "n_outcomes": self.n_outcomes, "source": self.source,
                "market_ids": list(self.market_ids),
                "outcome_labels": list(self.outcome_labels),
                "outcome_prices": list(self.outcome_prices),
                "quotes": {k: v.to_dict() for k, v in self.quotes.items()}}


@dataclass
class DiscoveryResult:
    groups: list
    skipped: list
    metrics: dict
    graph: ConstraintGraph
    normalized_quotes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"groups": [g.to_dict() for g in self.groups],
                "skipped": list(self.skipped), "metrics": dict(self.metrics)}


def _event_key(m: dict) -> Optional[str]:
    for k in ("event_id", "eventId", "eventSlug", "negRiskMarketID", "negRiskMarketId"):
        v = m.get(k)
        if v:
            return str(v)
    return None


def _group_kind(m: dict) -> Optional[str]:
    k = m.get("group_kind") or m.get("groupKind")
    if k:
        return str(k).lower()
    if m.get("negRisk") or m.get("neg_risk"):
        return "mutually_exclusive"
    return None


def _normalize_quote(m: dict, *, price: float, now_ms: int, max_book_age_ms: int,
                     fee_bps: float) -> "tuple[Optional[NormalizedQuote], Optional[str]]":
    """Build a NormalizedQuote for a market's YES side; return (quote, skip_reason)."""
    bid, ask = _to_float(m.get("bestBid")), _to_float(m.get("bestAsk"))
    if bid is None or ask is None:
        # fall back to mid-price book-less quote only if explicitly allowed
        ask = ask if ask is not None else price
        bid = bid if bid is not None else price
    if ask is None or ask <= 0 or bid is None or bid <= 0:
        return None, SKIP_MISSING_QUOTES
    depth_usd = _to_float(m.get("topDepthUsd")) or 0.0
    if depth_usd <= 0:
        return None, SKIP_NO_DEPTH
    mid = round((bid + ask) / 2.0, 6)
    spread = round(ask - bid, 6)
    ts = m.get("bookUpdatedTs") or m.get("book_updated_ts")
    ts_ms = None
    stale = False
    if ts is not None:
        try:
            ts_f = float(ts)
            ts_ms = int(ts_f * 1000) if ts_f < 1e12 else int(ts_f)
            stale = (now_ms - ts_ms) > max_book_age_ms
        except (TypeError, ValueError):
            ts_ms = None
    q = NormalizedQuote(best_bid=bid, best_ask=ask, mid=mid, spread=spread,
                        depth_usd=depth_usd, depth_shares=depth_usd / max(ask, _PMIN),
                        fee_bps=float(fee_bps), ts_ms=ts_ms, stale=bool(stale))
    return q, None


def _yes_outcome(m: dict, *, now_ms: int, max_book_age_ms: int, fee_bps: float
                 ) -> "tuple[Optional[Outcome], Optional[NormalizedQuote], Optional[str]]":
    """Normalize a cluster market's YES outcome (id, price, quote)."""
    mid = m.get("id") or m.get("market_id") or "?"
    price = _to_float(m.get("price"))
    if price is None:
        # gamma encodes outcomePrices as a JSON STRING -> parse robustly (top cause
        # of false non_numeric_price skips on the cluster path).
        prices = parse_list_field(m.get("outcomePrices"))
        price = _to_float(prices[0]) if prices else None
    if price is None:
        return None, None, SKIP_NON_NUMERIC_PRICE
    if not (0.0 < price < 1.0):
        return None, None, SKIP_DEGENERATE_PRICE
    q, err = _normalize_quote(m, price=price, now_ms=now_ms,
                              max_book_age_ms=max_book_age_ms, fee_bps=fee_bps)
    if err:
        return None, None, err
    tokens = m.get("clobTokenIds") or []
    oid = str(tokens[0]) if tokens else f"{mid}:yes"
    out = Outcome(id=oid, market_id=str(mid), label=str(m.get("question", ""))[:60],
                  price=price, ask=q.best_ask, bid=q.best_bid, ask_depth=q.depth_shares)
    return out, q, None


def discover_constraints(markets: Iterable[dict], *, now_ms: Optional[int] = None,
                         max_book_age_ms: int = 60_000, fee_bps: float = 0.0,
                         min_depth_usd: float = 1.0) -> DiscoveryResult:
    """Discover typed constraint groups from Polymarket markets (pure, conservative).

    Returns a :class:`DiscoveryResult` with the built graph, the discovered groups,
    typed skips (incl. ``INSUFFICIENT_METADATA``), normalized quotes, and the
    required metrics (groups discovered/scanned, group-type counts, average
    outcomes per group, malformed rejected, metadata + book coverage, skip reasons).
    Never invents a constraint when metadata is insufficient.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    markets = list(markets or [])
    total = len(markets)
    graph = ConstraintGraph()
    groups: list[DiscoveredGroup] = []
    skipped: list[dict] = []
    norm_quotes: dict = {}
    grouped_ids: set = set()
    fresh_book_ids: set = set()
    malformed = 0

    def skip(mid: str, reason: str, detail="", **extra) -> None:
        rec = {"market_id": str(mid), "reason": reason, "detail": detail}
        if extra:
            rec.update(extra)
        skipped.append(rec)

    # 1) cluster by event (solo bucket per market when no event key)
    by_event: dict = {}
    order: list = []
    for m in markets:
        ev = _event_key(m)
        key = ev or f"__solo__:{m.get('id', id(m))}"
        if key not in by_event:
            by_event[key] = []
            order.append(key)
        by_event[key].append(m)

    for key in order:
        ms = by_event[key]
        is_solo = key.startswith("__solo__:")
        kind = _group_kind(ms[0]) if ms else None

        # --- standalone market (binary complement or explicit outcomes/relation) ---
        # A single market (even one tagged with an event) with no relationship kind
        # is a standalone binary complement, NOT an insufficient-metadata cluster.
        if len(ms) == 1 and kind is None:
            m = ms[0]
            g = _discover_standalone(m, graph, now_ms=now_ms,
                                     max_book_age_ms=max_book_age_ms, fee_bps=fee_bps,
                                     min_depth_usd=min_depth_usd, skip=skip)
            if g is not None:
                groups.append(g)
                grouped_ids.add(str(m.get("id", "?")))
                for oid, q in g.quotes.items():
                    norm_quotes[oid] = q.to_dict()
                    if not q.stale:
                        fresh_book_ids.add(str(m.get("id", "?")))
            continue

        # --- same-event cluster ---
        event_id = key
        if kind is None:
            # cluster with no relationship metadata -> never invent
            for m in ms:
                skip(m.get("id", "?"), SKIP_INSUFFICIENT_METADATA,
                     f"event {event_id}: no group_kind/negRisk")
            continue
        relation = _KIND_TO_RELATION.get(kind)
        if relation is None:
            for m in ms:
                skip(m.get("id", "?"), SKIP_UNKNOWN_GROUP_KIND, kind)
            continue

        g = _discover_cluster(event_id, ms, relation, kind, graph, now_ms=now_ms,
                              max_book_age_ms=max_book_age_ms, fee_bps=fee_bps, skip=skip)
        if g is None:
            malformed += 1
            continue
        groups.append(g)
        for m in ms:
            grouped_ids.add(str(m.get("id", "?")))
        for oid, q in g.quotes.items():
            norm_quotes[oid] = q.to_dict()
        for m in ms:
            qm = g.quotes.get(_first_token(m))
            if qm is not None and not qm.stale:
                fresh_book_ids.add(str(m.get("id", "?")))

    # 2) metrics
    type_counts: dict = {}
    n_out_total = 0
    for g in groups:
        type_counts[g.relation] = type_counts.get(g.relation, 0) + 1
        n_out_total += g.n_outcomes
    skip_reasons: dict = {}
    reason_samples: dict = {}
    for s in skipped:
        r = s["reason"]
        skip_reasons[r] = skip_reasons.get(r, 0) + 1
        if r not in reason_samples:
            reason_samples[r] = {"market_id": s.get("market_id"),
                                 "detail": s.get("detail"),
                                 "market_ids": s.get("market_ids"),
                                 "outcome_labels": s.get("outcome_labels")}
    # precise price/outcome diagnostics (replace the contradictory bare counts).
    price_reason_keys = (SKIP_NON_NUMERIC_PRICE, "non_numeric_outcome_prices",
                         "outcome_price_count_mismatch", "missing_outcome_prices")
    non_numeric_price_count = sum(skip_reasons.get(k, 0) for k in price_reason_keys)
    insufficient_outcomes_count = skip_reasons.get(SKIP_INSUFFICIENT_OUTCOMES, 0)
    malformed_group_count = (malformed + skip_reasons.get(SKIP_MALFORMED_GROUP, 0)
                             + skip_reasons.get("duplicate_outcome_labels", 0))
    priced_groups = len(groups)
    price_attempts = priced_groups + non_numeric_price_count
    parsed_price_success_rate = round(priced_groups / price_attempts, 4) \
        if price_attempts else 1.0
    metrics = {
        "normalized_markets": len(grouped_ids),
        "sample_skipped_market_ids": [s["market_id"] for s in skipped[:20]],
        "groups_discovered": len(groups),
        "groups_scanned": len(graph.constraints()),
        "group_type_counts": type_counts,
        "avg_outcomes_per_group": round(n_out_total / len(groups), 4) if groups else 0.0,
        "malformed_groups_rejected": malformed,
        "metadata_coverage": round(len(grouped_ids) / total, 4) if total else 0.0,
        "book_coverage": round(len(fresh_book_ids) / total, 4) if total else 0.0,
        "skip_reasons": skip_reasons,
        "skip_reason_samples": reason_samples,
        "non_numeric_price_count": non_numeric_price_count,
        "insufficient_outcomes_count": insufficient_outcomes_count,
        "malformed_group_count": malformed_group_count,
        "parsed_price_success_rate": parsed_price_success_rate,
        "markets_seen": total,
    }
    logger.info("constraint discovery: discovered=%d scanned=%d malformed=%d "
                "metadata_cov=%.2f book_cov=%.2f", metrics["groups_discovered"],
                metrics["groups_scanned"], malformed, metrics["metadata_coverage"],
                metrics["book_coverage"])
    return DiscoveryResult(groups=groups, skipped=skipped, metrics=metrics,
                           graph=graph, normalized_quotes=norm_quotes)


def _first_token(m: dict) -> str:
    tokens = m.get("clobTokenIds") or []
    return str(tokens[0]) if tokens else f"{m.get('id', '?')}:yes"


def _discover_standalone(m: dict, graph: ConstraintGraph, *, now_ms: int,
                         max_book_age_ms: int, fee_bps: float, min_depth_usd: float,
                         skip) -> Optional[DiscoveredGroup]:
    mid = str(m.get("id") or m.get("market_id") or "?")
    if not _is_active(m):
        skip(mid, SKIP_MARKET_INACTIVE)
        return None

    explicit = m.get("outcomes")
    # ONLY treat `outcomes` as the structured explicit format when it is a list of
    # DICTS (internal/test shape). Polymarket gamma encodes `outcomes` as a JSON
    # STRING of LABELS (e.g. '["Yes","No"]') -> that must NOT be mistaken for
    # "insufficient outcomes" (the old bug reported len(string)=13 "outcomes").
    explicit_is_dicts = (isinstance(explicit, list) and explicit
                         and isinstance(explicit[0], dict))
    if explicit_is_dicts:
        if len(explicit) < 2:
            skip(mid, SKIP_INSUFFICIENT_OUTCOMES, f"{len(explicit)} outcomes")
            return None
        parsed: list[Outcome] = []
        quotes: dict = {}
        for idx, o in enumerate(explicit):
            price = _to_float(o.get("price"))
            ask = _to_float(o.get("ask")) if o.get("ask") is not None else price
            bid = _to_float(o.get("bid"))
            depth = _to_float(o.get("ask_depth"))
            if price is None or ask is None:
                skip(mid, SKIP_NON_NUMERIC_PRICE)
                return None
            if not (0.0 < price < 1.0):
                skip(mid, SKIP_DEGENERATE_PRICE)
                return None
            if depth is None or depth <= 0:
                skip(mid, SKIP_NO_DEPTH)
                return None
            oid = str(o.get("id") or f"{mid}:{idx}")
            parsed.append(Outcome(id=oid, market_id=mid, label=str(o.get("label", "")),
                                  price=price, ask=ask, bid=bid, ask_depth=depth))
            quotes[oid] = NormalizedQuote(
                best_bid=bid, best_ask=ask,
                mid=round((bid + ask) / 2.0, 6) if bid is not None else ask,
                spread=round(ask - bid, 6) if bid is not None else None,
                depth_usd=round(depth * ask, 4), depth_shares=depth, fee_bps=fee_bps,
                ts_ms=None, stale=False)
        relation = str(m.get("relation") or
                       ("complement" if len(parsed) == 2 else "mece")).lower()
        rel = _KIND_TO_RELATION.get(relation)
        if rel is None:
            skip(mid, SKIP_UNKNOWN_GROUP_KIND, relation)
            return None
        graph.add_outcomes(parsed)
        _add_relation(graph, rel, [p.id for p in parsed])
        return DiscoveredGroup(group_id=mid, event_id=mid, relation=rel.value,
                               outcome_ids=[p.id for p in parsed], n_outcomes=len(parsed),
                               source="explicit", quotes=quotes, market_ids=[mid],
                               outcome_labels=[p.label for p in parsed],
                               outcome_prices=[p.price for p in parsed])

    # polymarket gamma shape -> binary complement OR multi-way MECE (gamma encodes
    # outcomes/outcomePrices/clobTokenIds as JSON STRINGS). Parse robustly + emit a
    # PRECISE diagnostic (never the contradictory bare insufficient_outcomes).
    if not m.get("enableOrderBook", True):
        skip(mid, SKIP_NO_ORDERBOOK)
        return None
    labels = parse_list_field(m.get("outcomes"))
    prices = parse_list_field(m.get("outcomePrices"))
    tokens = parse_list_field(m.get("clobTokenIds"))
    from .price_parsing import analyze_outcomes
    diag = analyze_outcomes(labels, prices, tokens)
    if diag["reason"] is not None:
        skip(mid, diag["reason"], detail=diag, market_ids=[mid],
             outcome_labels=[str(x) for x in labels[:16]])
        return None
    # multi-way single market (e.g. neg-risk candidate list) -> MECE group
    if len(prices) > 2:
        g = _build_multiway_standalone(m, mid, labels, prices, tokens, graph,
                                       now_ms=now_ms, max_book_age_ms=max_book_age_ms,
                                       fee_bps=fee_bps, min_depth_usd=min_depth_usd, skip=skip)
        return g
    p_yes, p_no = _to_float(prices[0]), _to_float(prices[1])
    if p_yes is None or p_no is None:
        skip(mid, SKIP_NON_NUMERIC_PRICE, detail=diag, market_ids=[mid])
        return None
    if not (0.0 < p_yes < 1.0) or not (0.0 < p_no < 1.0):
        skip(mid, SKIP_DEGENERATE_PRICE)
        return None
    # token ids fall back to synthetic YES/NO ids when absent (paper-only)
    y = str(tokens[0]) if len(tokens) >= 1 else f"{mid}:yes"
    n = str(tokens[1]) if len(tokens) >= 2 else f"{mid}:no"
    # quotes: prefer live bid/ask, else derive a reference quote from prices so a
    # valid market is normalized (degraded, never silently dropped).
    bid, ask = _to_float(m.get("bestBid")), _to_float(m.get("bestAsk"))
    if bid is None or ask is None or ask <= 0 or bid <= 0:
        bid, ask = p_yes, p_yes
    depth_usd = _to_float(m.get("topDepthUsd"))
    if depth_usd is None or depth_usd <= 0:
        depth_usd = _to_float(m.get("liquidityNum")) or DEFAULT_PAPER_DEPTH_USD
    if depth_usd < min_depth_usd:
        skip(mid, SKIP_NO_DEPTH, f"${depth_usd}")
        return None
    yes_ask, no_ask = ask, max(_PMIN, min(_PMAX, 1.0 - bid))
    ts = m.get("bookUpdatedTs")
    ts_ms = int(float(ts) * 1000) if (ts is not None and float(ts) < 1e12) else (
        int(ts) if ts is not None else None)
    stale = bool(ts_ms is not None and (now_ms - ts_ms) > max_book_age_ms)
    graph.add_outcomes([
        Outcome(id=y, market_id=mid, label="YES", price=p_yes, ask=yes_ask,
                bid=bid, ask_depth=depth_usd / max(yes_ask, _PMIN)),
        Outcome(id=n, market_id=mid, label="NO", price=p_no, ask=no_ask,
                bid=max(_PMIN, 1.0 - ask), ask_depth=depth_usd / max(no_ask, _PMIN)),
    ])
    graph.add_complement(y, n)
    quotes = {
        y: NormalizedQuote(bid, ask, round((bid + ask) / 2, 6), round(ask - bid, 6),
                           depth_usd, depth_usd / max(yes_ask, _PMIN), fee_bps, ts_ms, stale),
        n: NormalizedQuote(max(_PMIN, 1.0 - ask), no_ask, round(1.0 - (bid + ask) / 2, 6),
                           round(ask - bid, 6), depth_usd, depth_usd / max(no_ask, _PMIN),
                           fee_bps, ts_ms, stale),
    }
    return DiscoveredGroup(group_id=mid, event_id=mid, relation=RelationType.COMPLEMENT.value,
                           outcome_ids=[y, n], n_outcomes=2, source="binary", quotes=quotes,
                           market_ids=[mid], outcome_labels=["YES", "NO"],
                           outcome_prices=[p_yes, p_no])


def _build_multiway_standalone(m: dict, mid: str, labels: list, prices: list,
                               tokens: list, graph: ConstraintGraph, *, now_ms: int,
                               max_book_age_ms: int, fee_bps: float,
                               min_depth_usd: float, skip) -> Optional[DiscoveredGroup]:
    """Build a MECE group from a SINGLE market exposing many outcomes (e.g. a
    neg-risk candidate list encoded as parallel outcomes/prices/tokens arrays).

    Distinct outcome/token ids are recorded so the report never shows a single
    market's many outcomes as if they were separate markets. Completeness is NOT
    fabricated — degenerate/illiquid legs are rejected with a typed reason."""
    pf = [_to_float(p) for p in prices]
    if any(p is None for p in pf):
        skip(mid, SKIP_NON_NUMERIC_PRICE, market_ids=[mid]); return None
    if any(not (0.0 < p < 1.0) for p in pf):
        skip(mid, SKIP_DEGENERATE_PRICE); return None
    depth_usd = _to_float(m.get("topDepthUsd"))
    if depth_usd is None or depth_usd <= 0:
        depth_usd = _to_float(m.get("liquidityNum")) or DEFAULT_PAPER_DEPTH_USD
    if depth_usd < min_depth_usd:
        skip(mid, SKIP_NO_DEPTH, f"${depth_usd}"); return None
    outs: list[Outcome] = []
    quotes: dict = {}
    oids: list = []
    for idx, (lab, p) in enumerate(zip(labels or [""] * len(pf), pf)):
        oid = str(tokens[idx]) if idx < len(tokens) else f"{mid}:{idx}"
        ask = p
        outs.append(Outcome(id=oid, market_id=mid, label=str(lab)[:60], price=p,
                            ask=ask, bid=p, ask_depth=depth_usd / max(ask, _PMIN)))
        quotes[oid] = NormalizedQuote(p, ask, p, 0.0, depth_usd,
                                      depth_usd / max(ask, _PMIN), fee_bps, None, False)
        oids.append(oid)
    if len({o.id for o in outs}) != len(outs):
        skip(mid, "duplicate_outcome_labels", market_ids=[mid]); return None
    graph.add_outcomes(outs)
    graph.add_mece(oids)
    return DiscoveredGroup(group_id=mid, event_id=mid, relation=RelationType.MECE.value,
                           outcome_ids=oids, n_outcomes=len(oids), source="multiway",
                           quotes=quotes, market_ids=[mid],
                           outcome_labels=[str(l)[:60] for l in (labels or [])],
                           outcome_prices=pf)


def _discover_cluster(event_id: str, ms: list, relation: RelationType, kind: str,
                      graph: ConstraintGraph, *, now_ms: int, max_book_age_ms: int,
                      fee_bps: float, skip) -> Optional[DiscoveredGroup]:
    """Discover one same-event cluster group from its member markets."""
    parsed: list[Outcome] = []
    quotes: dict = {}
    # order scalar-threshold markets by threshold (ascending) for the implication chain
    if kind == "scalar_threshold":
        ms = sorted(ms, key=lambda m: _to_float(m.get("threshold")) or 0.0)
    for m in ms:
        if not _is_active(m):
            skip(m.get("id", "?"), SKIP_MARKET_INACTIVE)
            continue
        out, q, err = _yes_outcome(m, now_ms=now_ms, max_book_age_ms=max_book_age_ms,
                                   fee_bps=fee_bps)
        if err:
            skip(m.get("id", "?"), err)
            continue
        parsed.append(out)
        quotes[out.id] = q
    if len(parsed) < 2:
        skip(event_id, SKIP_MALFORMED_GROUP, f"{len(parsed)} usable outcomes in cluster")
        return None
    ids = [p.id for p in parsed]
    graph.add_outcomes(parsed)

    if kind == "hierarchy":
        # parent = market with role "parent"; children = the rest
        parent = next((m for m in ms if str(m.get("role", "")).lower() == "parent"), None)
        if parent is None:
            skip(event_id, SKIP_INSUFFICIENT_METADATA, "hierarchy without a parent role")
            return None
        parent_id = _first_token(parent)
        child_ids = [i for i in ids if i != parent_id]
        if not child_ids:
            skip(event_id, SKIP_MALFORMED_GROUP, "hierarchy without children")
            return None
        graph.add_hierarchy(parent_id, child_ids)
    elif kind == "scalar_threshold":
        # monotone >= K: higher threshold implies lower threshold
        for higher, lower in zip(ids[1:], ids[:-1]):
            graph.add_cross_market_implies(higher, lower)
    else:
        _add_relation(graph, relation, ids)

    return DiscoveredGroup(group_id=event_id, event_id=event_id, relation=relation.value,
                           outcome_ids=ids, n_outcomes=len(ids),
                           source=("negrisk" if kind == "mutually_exclusive"
                                   and (ms[0].get("negRisk") or ms[0].get("neg_risk"))
                                   else kind), quotes=quotes,
                           market_ids=[p.market_id for p in parsed],
                           outcome_labels=[p.label for p in parsed],
                           outcome_prices=[p.price for p in parsed])


def _add_relation(graph: ConstraintGraph, relation: RelationType, ids: list) -> None:
    if relation == RelationType.COMPLEMENT and len(ids) == 2:
        graph.add_complement(ids[0], ids[1])
    elif relation == RelationType.MECE:
        graph.add_mece(ids)
    elif relation == RelationType.RANGE:
        graph.add_range(ids)
    elif relation == RelationType.MUTUALLY_EXCLUSIVE:
        graph.add_mutually_exclusive(ids)
    elif relation == RelationType.COLLECTIVELY_EXHAUSTIVE:
        graph.add_collectively_exhaustive(ids)
    else:  # fallback: treat as MECE partition
        graph.add_mece(ids)
