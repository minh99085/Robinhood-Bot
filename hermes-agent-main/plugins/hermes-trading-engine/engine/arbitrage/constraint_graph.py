"""Market constraint graph for Bregman coherence arbitrage (PAPER ONLY, pure).

A constraint graph holds binary prediction-market *outcomes* (each with a
market-implied probability and order-book ask/bid + depth) and the logical
*constraints* relating them:

* ``COMPLEMENT``                 — exactly one of {a, b} is true (sum == 1).
* ``MUTUALLY_EXCLUSIVE``         — at most one is true (sum <= 1).
* ``COLLECTIVELY_EXHAUSTIVE``    — at least one is true (sum >= 1).
* ``MECE`` / ``RANGE``           — exactly one of a partition is true (sum == 1).
* ``HIERARCHY``                  — children imply the parent (child <= parent).
* ``CROSS_MARKET_EQUIV``         — equal probability across markets (a == b).
* ``CROSS_MARKET_IMPLIES``       — implication across markets (a <= b).

The graph compiles to projection *primitives* (sum / equal / implies) consumed by
:mod:`engine.arbitrage.bregman_projection`, and enumerates feasible 0/1 *atoms*
(world states) for the relations whose worst-case payoff is certifiable.

Quant responsibilities (this module)
------------------------------------
See :data:`QUANT_RESPONSIBILITIES`. Researcher/analyst define relationships;
developer encodes them here; trader consumes only certified outputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

logger = logging.getLogger("hte.arbitrage.graph")

_PMIN, _PMAX = 1e-6, 1.0 - 1e-6

QUANT_RESPONSIBILITIES: dict[str, str] = {
    "quant_analyst": "Defines which market relationships hold (MECE/complement/"
                     "range/hierarchy/cross-market) and audits universe quality.",
    "quant_researcher": "Validates the coherence model + projection assumptions; "
                        "sets incoherence/edge thresholds.",
    "quant_developer": "Owns this graph + projection + certificate code; type-safe, "
                       "deterministic, tested.",
    "trader": "Consumes ONLY certified, fill-feasible opportunities; never trades "
              "an uncertified candidate.",
}


class RelationType(str, Enum):
    COMPLEMENT = "complement"
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"
    COLLECTIVELY_EXHAUSTIVE = "collectively_exhaustive"
    MECE = "mece"
    RANGE = "range"
    HIERARCHY = "hierarchy"
    CROSS_MARKET_EQUIV = "cross_market_equiv"
    CROSS_MARKET_IMPLIES = "cross_market_implies"


# Relations whose worst-case payoff is enumerable + certifiable as a buy-set arb.
EXACTLY_ONE_RELATIONS = frozenset({
    RelationType.COMPLEMENT, RelationType.MECE, RelationType.RANGE})


@dataclass
class Outcome:
    """A single binary outcome (e.g. a market's YES or a range bucket)."""

    id: str
    market_id: str = ""
    label: str = ""
    price: float = 0.0          # market-implied probability (mid)
    ask: Optional[float] = None  # price to BUY one share (pays $1 if true)
    bid: Optional[float] = None  # price to SELL one share
    ask_depth: float = 0.0       # shares available at the ask
    bid_depth: float = 0.0

    def buy_price(self) -> float:
        return float(self.ask if self.ask is not None else self.price)

    def sell_price(self) -> float:
        return float(self.bid if self.bid is not None else self.price)


@dataclass
class Constraint:
    """A logical relationship over a set of outcome ids."""

    type: RelationType
    outcome_ids: list[str]
    op: str = "=="              # for sum-type relations: <=, >=, ==
    rhs: float = 1.0
    parent_id: Optional[str] = None  # hierarchy parent
    meta: dict = field(default_factory=dict)


@dataclass
class Primitive:
    """A projection primitive with a closed-form KL projection."""

    kind: str                  # "sum" | "equal" | "implies"
    ids: list[str]
    op: str = "=="             # for "sum"
    rhs: float = 1.0


class ConstraintGraph:
    """Holds outcomes + constraints; compiles primitives + feasible atoms."""

    def __init__(self) -> None:
        self._outcomes: dict[str, Outcome] = {}
        self._constraints: list[Constraint] = []

    # -- construction --------------------------------------------------------
    def add_outcome(self, outcome: Outcome) -> Outcome:
        self._outcomes[outcome.id] = outcome
        return outcome

    def add_outcomes(self, outcomes: Iterable[Outcome]) -> None:
        for o in outcomes:
            self.add_outcome(o)

    def get(self, outcome_id: str) -> Optional[Outcome]:
        return self._outcomes.get(outcome_id)

    def outcomes(self) -> list[Outcome]:
        return list(self._outcomes.values())

    def constraints(self) -> list[Constraint]:
        return list(self._constraints)

    def add_constraint(self, constraint: Constraint) -> Constraint:
        self._constraints.append(constraint)
        return constraint

    # convenience builders
    def add_complement(self, a: str, b: str) -> Constraint:
        return self.add_constraint(Constraint(RelationType.COMPLEMENT, [a, b], "==", 1.0))

    def add_mece(self, ids: list[str]) -> Constraint:
        return self.add_constraint(Constraint(RelationType.MECE, list(ids), "==", 1.0))

    def add_range(self, ids: list[str]) -> Constraint:
        return self.add_constraint(Constraint(RelationType.RANGE, list(ids), "==", 1.0))

    def add_mutually_exclusive(self, ids: list[str]) -> Constraint:
        return self.add_constraint(
            Constraint(RelationType.MUTUALLY_EXCLUSIVE, list(ids), "<=", 1.0))

    def add_collectively_exhaustive(self, ids: list[str]) -> Constraint:
        return self.add_constraint(
            Constraint(RelationType.COLLECTIVELY_EXHAUSTIVE, list(ids), ">=", 1.0))

    def add_hierarchy(self, parent: str, children: list[str]) -> Constraint:
        return self.add_constraint(
            Constraint(RelationType.HIERARCHY, list(children), parent_id=parent))

    def add_cross_market_equiv(self, a: str, b: str) -> Constraint:
        return self.add_constraint(Constraint(RelationType.CROSS_MARKET_EQUIV, [a, b]))

    def add_cross_market_implies(self, a: str, b: str) -> Constraint:
        return self.add_constraint(Constraint(RelationType.CROSS_MARKET_IMPLIES, [a, b]))

    # -- compilation ---------------------------------------------------------
    def price_vector(self) -> dict[str, float]:
        """Map outcome id -> market-implied probability (clipped to (0,1))."""
        return {oid: min(_PMAX, max(_PMIN, float(o.price)))
                for oid, o in self._outcomes.items()}

    def to_primitives(self) -> list[Primitive]:
        """Compile constraints into projection primitives (sum/equal/implies)."""
        prims: list[Primitive] = []
        for c in self._constraints:
            t = c.type
            if t in (RelationType.COMPLEMENT, RelationType.MECE, RelationType.RANGE):
                prims.append(Primitive("sum", list(c.outcome_ids), "==", 1.0))
            elif t == RelationType.MUTUALLY_EXCLUSIVE:
                prims.append(Primitive("sum", list(c.outcome_ids), "<=", 1.0))
            elif t == RelationType.COLLECTIVELY_EXHAUSTIVE:
                prims.append(Primitive("sum", list(c.outcome_ids), ">=", 1.0))
            elif t == RelationType.CROSS_MARKET_EQUIV:
                prims.append(Primitive("equal", list(c.outcome_ids)))
            elif t == RelationType.CROSS_MARKET_IMPLIES:
                prims.append(Primitive("implies", list(c.outcome_ids[:2])))
            elif t == RelationType.HIERARCHY and c.parent_id:
                for child in c.outcome_ids:
                    prims.append(Primitive("implies", [child, c.parent_id]))
        return prims

    def certifiable_constraints(self) -> list[Constraint]:
        """Constraints whose worst-case payoff can be enumerated + certified."""
        return [c for c in self._constraints if c.type in EXACTLY_ONE_RELATIONS]

    def feasible_atoms(self, constraint: Constraint) -> list[dict[str, int]]:
        """Enumerate feasible 0/1 world states over a constraint's outcomes.

        Returns ``[]`` for relations that are not finitely enumerable here
        (collectively-exhaustive, hierarchy) — those are not buy-set certifiable.
        """
        ids = list(constraint.outcome_ids)
        t = constraint.type
        if t in EXACTLY_ONE_RELATIONS:  # exactly one true
            return [{j: (1 if j == i else 0) for j in ids} for i in ids]
        if t == RelationType.MUTUALLY_EXCLUSIVE:  # at most one true (+ all-zero)
            atoms = [{j: (1 if j == i else 0) for j in ids} for i in ids]
            atoms.append({j: 0 for j in ids})
            return atoms
        if t == RelationType.CROSS_MARKET_EQUIV:  # a == b
            return [{ids[0]: 1, ids[1]: 1}, {ids[0]: 0, ids[1]: 0}]
        if t == RelationType.CROSS_MARKET_IMPLIES:  # a -> b
            a, b = ids[0], ids[1]
            return [{a: 0, b: 0}, {a: 0, b: 1}, {a: 1, b: 1}]
        return []

    # -- validation ----------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of structural issues (empty = clean)."""
        issues: list[str] = []
        for c in self._constraints:
            for oid in c.outcome_ids:
                if oid not in self._outcomes:
                    issues.append(f"constraint {c.type.value} references unknown outcome {oid}")
            if c.parent_id and c.parent_id not in self._outcomes:
                issues.append(f"hierarchy references unknown parent {c.parent_id}")
        for oid, o in self._outcomes.items():
            if not (0.0 <= float(o.price) <= 1.0):
                issues.append(f"outcome {oid} price {o.price} not in [0,1]")
        return issues


# --------------------------------------------------------------------------- #
# Market -> constraint-graph ingestion (PAPER ONLY).
#
# Typed skip reasons: every market that cannot form a valid constraint group is
# recorded with one of these so the scan is fully auditable (no silent drops).
# --------------------------------------------------------------------------- #
SKIP_MARKET_INACTIVE = "market_inactive"
SKIP_NO_ORDERBOOK = "no_orderbook"
SKIP_INSUFFICIENT_OUTCOMES = "insufficient_outcomes"
SKIP_NON_NUMERIC_PRICE = "non_numeric_price"
SKIP_MISSING_QUOTES = "missing_quotes"
SKIP_NO_DEPTH = "no_depth"
SKIP_DEGENERATE_PRICE = "degenerate_price"
SKIP_NO_RELATION = "no_relationship"

# precise outcome-diagnostic reasons (replace the contradictory bare counts).
SKIP_NON_NUMERIC_OUTCOME_PRICES = "non_numeric_outcome_prices"
SKIP_OUTCOME_PRICE_COUNT_MISMATCH = "outcome_price_count_mismatch"
SKIP_MISSING_OUTCOME_PRICES = "missing_outcome_prices"
SKIP_DUPLICATE_OUTCOME_LABELS = "duplicate_outcome_labels"
SKIP_INCOMPLETE_MULTIWAY = "incomplete_multway_family"
SKIP_AMBIGUOUS_MULTIWAY = "ambiguous_multway_group"

SKIP_REASONS = frozenset({
    SKIP_MARKET_INACTIVE, SKIP_NO_ORDERBOOK, SKIP_INSUFFICIENT_OUTCOMES,
    SKIP_NON_NUMERIC_PRICE, SKIP_MISSING_QUOTES, SKIP_NO_DEPTH,
    SKIP_DEGENERATE_PRICE, SKIP_NO_RELATION,
    SKIP_NON_NUMERIC_OUTCOME_PRICES, SKIP_OUTCOME_PRICE_COUNT_MISMATCH,
    SKIP_MISSING_OUTCOME_PRICES, SKIP_DUPLICATE_OUTCOME_LABELS,
    SKIP_INCOMPLETE_MULTIWAY, SKIP_AMBIGUOUS_MULTIWAY,
})

_RELATION_BUILDERS = {
    "complement": "add_complement", "mece": "add_mece", "range": "add_range",
}


def _to_float(v) -> Optional[float]:
    """Parse a price/number from a float or a Polymarket string (robust, pure).

    Accepts raw floats; numeric strings (``"0.42"``); ``$``/``%`` formatted
    (``"$0.42"`` / ``"42%"`` -> 0.42); thousands separators (``"1,234.5"``);
    JSON-list-encoded singletons (``"[0.42]"``); and treats blank / ``"None"`` /
    ``"null"`` / ``"nan"`` as missing. Delegates to the shared robust parser."""
    from .price_parsing import parse_price
    return parse_price(v)


def parse_list_field(v):
    """Robustly parse a Polymarket list field (PAPER ingestion normalization).

    Polymarket gamma encodes ``outcomePrices`` / ``clobTokenIds`` as JSON STRINGS
    (e.g. ``'["0.41","0.59"]'``), not lists — the #1 cause of ``non_numeric_price``
    / ``insufficient_outcomes`` skips. Accepts a list (returned as-is), a JSON
    string, or a comma-separated string; returns ``[]`` on failure. Pure."""
    if isinstance(v, list):
        return v
    if isinstance(v, (int, float)):
        return [v]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            import json as _json
            out = _json.loads(s)
            return out if isinstance(out, list) else [out]
        except Exception:  # noqa: BLE001
            return [x.strip().strip('"').strip("'") for x in s.strip("[]").split(",")
                    if x.strip()]
    return []


def _is_active(m: dict) -> bool:
    if m.get("closed") or m.get("archived"):
        return False
    if m.get("active") is False:
        return False
    if m.get("acceptingOrders") is False:
        return False
    return True


def build_constraint_graph(markets: Iterable[dict], *, min_depth_usd: float = 1.0
                           ) -> "tuple[ConstraintGraph, list[dict]]":
    """Build a :class:`ConstraintGraph` from market dicts (PAPER ONLY, pure).

    Accepts two shapes per market:

    * **Explicit**: ``{"id", "outcomes": [{"id","price","ask","ask_depth","bid"?}],
      "relation": "complement"|"mece"|"range"}`` — used by tests + cross-market
      groups.
    * **Polymarket binary** (polymarket-client v2 gamma shape): ``outcomePrices``
      (2 strings) + ``clobTokenIds`` + ``bestBid``/``bestAsk`` + ``topDepthUsd``
      → a YES/NO ``COMPLEMENT`` (NO ask ≈ ``1 - bestBid``; depth ``usd/price``).

    Returns ``(graph, skipped)`` where every skipped market carries a typed
    ``reason`` from :data:`SKIP_REASONS` (no silent drops). Deterministic; never
    performs I/O, never trades.
    """
    graph = ConstraintGraph()
    skipped: list[dict] = []

    def skip(mid: str, reason: str, detail: str = "") -> None:
        skipped.append({"market_id": str(mid), "reason": reason, "detail": detail})

    for m in markets or []:
        mid = m.get("id") or m.get("market_id") or "?"
        if not _is_active(m):
            skip(mid, SKIP_MARKET_INACTIVE, "closed/archived/inactive")
            continue

        explicit = m.get("outcomes")
        explicit_is_dicts = (isinstance(explicit, list) and explicit
                             and isinstance(explicit[0], dict))
        if explicit_is_dicts:
            if len(explicit) < 2:
                skip(mid, SKIP_INSUFFICIENT_OUTCOMES, f"{len(explicit)} outcomes")
                continue
            parsed: list[Outcome] = []
            bad = None
            for idx, o in enumerate(explicit):
                price = _to_float(o.get("price"))
                ask = _to_float(o.get("ask")) if o.get("ask") is not None else price
                depth = _to_float(o.get("ask_depth"))
                if price is None or ask is None:
                    bad = SKIP_NON_NUMERIC_PRICE
                    break
                if not (0.0 < price < 1.0):
                    bad = SKIP_DEGENERATE_PRICE
                    break
                if depth is None or depth <= 0:
                    bad = SKIP_NO_DEPTH
                    break
                parsed.append(Outcome(
                    id=str(o.get("id") or f"{mid}:{idx}"), market_id=str(mid),
                    label=str(o.get("label", "")), price=price, ask=ask,
                    bid=_to_float(o.get("bid")), ask_depth=depth))
            if bad:
                skip(mid, bad)
                continue
            relation = str(m.get("relation") or
                           ("complement" if len(parsed) == 2 else "mece")).lower()
            builder = _RELATION_BUILDERS.get(relation)
            if builder is None:
                skip(mid, SKIP_NO_RELATION, relation)
                continue
            graph.add_outcomes(parsed)
            if builder == "add_complement":
                graph.add_complement(parsed[0].id, parsed[1].id)
            else:
                getattr(graph, builder)([p.id for p in parsed])
            continue

        # --- Polymarket binary shape (gamma encodes list fields as JSON strings) ---
        if not m.get("enableOrderBook", True):
            skip(mid, SKIP_NO_ORDERBOOK)
            continue
        prices = parse_list_field(m.get("outcomePrices"))
        tokens = parse_list_field(m.get("clobTokenIds"))
        if len(prices) < 2 or len(tokens) < 2:
            skip(mid, SKIP_INSUFFICIENT_OUTCOMES, f"{len(prices)} prices / {len(tokens)} tokens")
            continue
        p_yes, p_no = _to_float(prices[0]), _to_float(prices[1])
        if p_yes is None or p_no is None:
            skip(mid, SKIP_NON_NUMERIC_PRICE)
            continue
        if not (0.0 < p_yes < 1.0) or not (0.0 < p_no < 1.0):
            skip(mid, SKIP_DEGENERATE_PRICE)
            continue
        best_bid, best_ask = _to_float(m.get("bestBid")), _to_float(m.get("bestAsk"))
        if best_bid is None or best_ask is None or best_ask <= 0 or best_bid <= 0:
            skip(mid, SKIP_MISSING_QUOTES)
            continue
        depth_usd = _to_float(m.get("topDepthUsd")) or 0.0
        if depth_usd < min_depth_usd:
            skip(mid, SKIP_NO_DEPTH, f"${depth_usd}")
            continue
        yes_ask = best_ask
        no_ask = max(_PMIN, min(_PMAX, 1.0 - best_bid))  # buy NO ≈ 1 - YES bid
        graph.add_outcomes([
            Outcome(id=str(tokens[0]), market_id=str(mid), label="YES", price=p_yes,
                    ask=yes_ask, ask_depth=depth_usd / max(yes_ask, _PMIN)),
            Outcome(id=str(tokens[1]), market_id=str(mid), label="NO", price=p_no,
                    ask=no_ask, ask_depth=depth_usd / max(no_ask, _PMIN)),
        ])
        graph.add_complement(str(tokens[0]), str(tokens[1]))

    logger.info("build_constraint_graph: scanned=%d skipped=%d",
                len(graph.constraints()), len(skipped))
    return graph, skipped
