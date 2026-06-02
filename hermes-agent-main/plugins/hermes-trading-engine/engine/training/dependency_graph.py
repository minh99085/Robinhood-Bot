"""Polymarket market dependency graph (deterministic, offline, PAPER-ONLY).

Models the scanned Polymarket universe as a typed graph so combinatorial Bregman
arbitrage grouping, portfolio exposure netting, and aggressive paper
diversification all reason over the SAME structure instead of ad-hoc per-tick
heuristics.

Nodes are markets (with their outcome tokens); edges are typed dependencies,
each carrying a confidence + ambiguity:

* ``same_event``           — markets sharing one Polymarket event.
* ``complement``           — a market's YES/NO outcome pair (intra-market).
* ``mutually_exclusive``   — same-event legs where at most one resolves YES.
* ``exhaustive``           — a mutually-exclusive set that is ALSO complete.
* ``range_bucket``         — contiguous numeric range buckets of one quantity.
* ``related_asset``        — markets on the same macro/crypto asset, cross-event.
* ``correlated_category``  — same-category markets, cross-event (soft).

Clusters: connected components over the structural edges (same-event family) are
the certifiable Bregman outcome sets; components over structural ∪ related-asset
are the *correlated* clusters used for exposure netting + diversification.

Quant scope documented here:

* Data Acquisition & Ingestion — built from the scanner's normalized records.
* Preprocessing / Feature Engineering — asset detection + edge typing.
* Probabilistic Modeling — exhaustive groups price onto a simplex.
* Signal Generation — feeds candidate ranking / active-learning diversity.
* Bregman arbitrage — :func:`engine.training.bregman_grouping.groups_from_graph`
  builds combinatorial simplex groups from structural clusters.
* Risk / Portfolio Optimization — :class:`ClusterExposureNetter` nets same-event
  hedges + caps correlated-cluster exposure.
* Backtesting / Robustness — deterministic, tie-broken by market id.
* CLOB v2 simulation — only structural metadata; never sizes/places an order.
* Monitoring — :meth:`MarketDependencyGraph.to_report` artifact.
* Compliance / Security — no secrets, no network; public asset keywords only.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from engine.markets.universe_manager import _as_float, detect_asset

from .market_grouping import _RANGE_RE

__all__ = [
    "EdgeType", "GraphNode", "GraphEdge", "MarketDependencyGraph",
    "ClusterExposureNetter", "diversified_selection", "detect_asset",
]

_EXHAUSTIVE_LO, _EXHAUSTIVE_HI = 0.90, 1.10


class EdgeType:
    SAME_EVENT = "same_event"
    COMPLEMENT = "complement"
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"
    EXHAUSTIVE = "exhaustive"
    RANGE_BUCKET = "range_bucket"
    RELATED_ASSET = "related_asset"
    CORRELATED_CATEGORY = "correlated_category"

    ALL = (SAME_EVENT, COMPLEMENT, MUTUALLY_EXCLUSIVE, EXHAUSTIVE, RANGE_BUCKET,
           RELATED_ASSET, CORRELATED_CATEGORY)
    # Same-event family — the certifiable Bregman outcome set.
    STRUCTURAL = frozenset({SAME_EVENT, MUTUALLY_EXCLUSIVE, EXHAUSTIVE, RANGE_BUCKET})
    # Soft correlation (used to widen exposure/diversity clusters, not grouping).
    CORRELATION = frozenset({RELATED_ASSET, CORRELATED_CATEGORY})


@dataclass
class GraphNode:
    market_id: str
    category: str = ""
    group_key: str = ""
    asset: str = ""
    question: str = ""
    yes_price: Optional[float] = None
    tokens: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"market_id": self.market_id, "category": self.category,
                "group_key": self.group_key, "asset": self.asset,
                "yes_price": self.yes_price, "token_count": len(self.tokens)}


@dataclass
class GraphEdge:
    src: str
    dst: str
    edge_type: str
    confidence: float = 0.0
    ambiguity: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "edge_type": self.edge_type,
                "confidence": round(self.confidence, 4),
                "ambiguity": round(self.ambiguity, 4), "meta": dict(self.meta)}


def _clamp01(x) -> float:
    return max(0.0, min(1.0, float(x or 0.0)))


def _text(rec) -> str:
    raw = getattr(rec, "raw", None) or {}
    return " ".join(str(x) for x in (
        getattr(rec, "question", "") or "", raw.get("groupItemTitle") or "",
        raw.get("title") or "") if x)


def _node_ambiguity(rec) -> float:
    raw = getattr(rec, "raw", None) or {}
    a = _as_float(raw.get("ambiguity"))
    if a is None:
        a = 0.0 if getattr(rec, "has_resolution_text", False) else 0.5
    return _clamp01(a)


def _explicit_event(rec) -> bool:
    raw = getattr(rec, "raw", None) or {}
    return any(raw.get(k) for k in ("negRiskMarketID", "negRiskMarketId", "eventId",
                                    "event_id", "conditionId"))


def _event_exhaustive(recs: list) -> tuple:
    """Return (exhaustive, confidence). Conservative: require a completeness
    signal (outcomeCount/negRiskComplete) for high confidence; the YES-price-sum
    heuristic only yields a lower-confidence exhaustive flag."""
    for r in recs:
        raw = getattr(r, "raw", None) or {}
        if raw.get("negRiskComplete") or raw.get("exhaustive") or raw.get("complete_set"):
            return True, 0.9
        oc = raw.get("outcomeCount") or raw.get("outcome_count")
        try:
            if oc is not None and int(oc) == len(recs):
                return True, 0.9
        except (TypeError, ValueError):
            pass
    prices = [_as_float(getattr(r, "yes_price", None)) for r in recs]
    prices = [p for p in prices if p is not None]
    if len(prices) >= 2 and _EXHAUSTIVE_LO <= sum(prices) <= _EXHAUSTIVE_HI:
        return True, 0.6
    return False, 0.0


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic root = lexicographically smaller id
            lo, hi = (ra, rb) if ra <= rb else (rb, ra)
            self.parent[hi] = lo


class MarketDependencyGraph:
    def __init__(self):
        self.nodes: dict = {}
        self.edges: list = []
        self._adj: dict = {}   # (a,b) sorted -> set(edge_types)

    # -- construction --------------------------------------------------------
    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.market_id] = node

    def add_edge(self, src: str, dst: str, edge_type: str, confidence: float = 0.0,
                 ambiguity: float = 0.0, meta: Optional[dict] = None) -> None:
        self.edges.append(GraphEdge(src=src, dst=dst, edge_type=edge_type,
                                    confidence=_clamp01(confidence),
                                    ambiguity=_clamp01(ambiguity), meta=meta or {}))
        key = (src, dst) if src <= dst else (dst, src)
        self._adj.setdefault(key, set()).add(edge_type)

    @classmethod
    def build(cls, records, *, correlation_edges: bool = True,
              max_correlation_degree: int = 64) -> "MarketDependencyGraph":
        g = cls()
        info: dict = {}
        for r in records or []:
            mid = str(getattr(r, "market_id", "") or "")
            if not mid or mid in info:
                continue
            tokens = [str(t) for t in (getattr(r, "clob_token_ids", []) or []) if t]
            node = GraphNode(
                market_id=mid, category=str(getattr(r, "category", "") or ""),
                group_key=str(getattr(r, "group_key", None) or mid),
                asset=detect_asset(_text(r)), question=str(getattr(r, "question", "") or ""),
                yes_price=_as_float(getattr(r, "yes_price", None)), tokens=tokens)
            g.add_node(node)
            info[mid] = {"rec": r, "amb": _node_ambiguity(r), "text": _text(r),
                         "node": node}

        # complement (intra-market YES/NO pair)
        for mid, i in info.items():
            if len(set(i["node"].tokens)) >= 2:
                g.add_edge(mid, mid, EdgeType.COMPLEMENT, confidence=1.0, ambiguity=i["amb"])

        # same-event family
        by_event: dict = {}
        for mid, i in info.items():
            by_event.setdefault(i["node"].group_key, []).append(mid)
        for ev, mids in by_event.items():
            if len(mids) < 2:
                continue
            recs_ev = [info[m]["rec"] for m in mids]
            is_range = any(_RANGE_RE.search(info[m]["text"]) for m in mids)
            exhaustive, exh_conf = _event_exhaustive(recs_ev)
            explicit = any(_explicit_event(info[m]["rec"]) for m in mids)
            se_conf = 0.9 if explicit else 0.7
            for a, b in itertools.combinations(sorted(mids), 2):
                amb = (info[a]["amb"] + info[b]["amb"]) / 2.0
                g.add_edge(a, b, EdgeType.SAME_EVENT, se_conf, amb)
                g.add_edge(a, b, EdgeType.MUTUALLY_EXCLUSIVE, se_conf, amb)
                if exhaustive:
                    g.add_edge(a, b, EdgeType.EXHAUSTIVE, exh_conf, amb)
                if is_range:
                    g.add_edge(a, b, EdgeType.RANGE_BUCKET, 0.7, amb)

        if correlation_edges:
            # related_asset (cross-event, same underlying)
            by_asset: dict = {}
            for mid, i in info.items():
                if i["node"].asset:
                    by_asset.setdefault(i["node"].asset, []).append(mid)
            for asset, mids in by_asset.items():
                for a, b in itertools.combinations(sorted(mids)[:max_correlation_degree], 2):
                    if info[a]["node"].group_key == info[b]["node"].group_key:
                        continue
                    amb = (info[a]["amb"] + info[b]["amb"]) / 2.0
                    g.add_edge(a, b, EdgeType.RELATED_ASSET, 0.7, amb, {"asset": asset})
            # correlated_category (cross-event, soft; skip same-asset pairs)
            by_cat: dict = {}
            for mid, i in info.items():
                if i["node"].category:
                    by_cat.setdefault(i["node"].category, []).append(mid)
            for cat, mids in by_cat.items():
                for a, b in itertools.combinations(sorted(mids)[:max_correlation_degree], 2):
                    if info[a]["node"].group_key == info[b]["node"].group_key:
                        continue
                    if info[a]["node"].asset and info[a]["node"].asset == info[b]["node"].asset:
                        continue
                    amb = (info[a]["amb"] + info[b]["amb"]) / 2.0
                    g.add_edge(a, b, EdgeType.CORRELATED_CATEGORY, 0.3, amb, {"category": cat})
        return g

    # -- queries -------------------------------------------------------------
    def has_edge(self, a: str, b: str, edge_type: Optional[str] = None) -> bool:
        key = (a, b) if a <= b else (b, a)
        types = self._adj.get(key)
        if types is None:
            return False
        return True if edge_type is None else edge_type in types

    def edge_types_between(self, a: str, b: str) -> set:
        key = (a, b) if a <= b else (b, a)
        return set(self._adj.get(key, set()))

    def neighbors(self, market_id: str, edge_types=None) -> set:
        out: set = set()
        for e in self.edges:
            if e.src == e.dst:
                continue
            if edge_types is not None and e.edge_type not in edge_types:
                continue
            if e.src == market_id:
                out.add(e.dst)
            elif e.dst == market_id:
                out.add(e.src)
        return out

    def _components(self, edge_types) -> list:
        uf = _UnionFind(list(self.nodes))
        for e in self.edges:
            if e.src == e.dst:
                continue
            if e.edge_type in edge_types and e.src in self.nodes and e.dst in self.nodes:
                uf.union(e.src, e.dst)
        comps: dict = {}
        for m in self.nodes:
            comps.setdefault(uf.find(m), set()).add(m)
        return list(comps.values())

    def clusters(self, edge_types=None) -> list:
        return self._components(edge_types or EdgeType.STRUCTURAL)

    def correlated_clusters(self) -> list:
        return self._components(EdgeType.STRUCTURAL | {EdgeType.RELATED_ASSET})

    def cluster_map(self, *, correlated: bool = False) -> dict:
        comps = self.correlated_clusters() if correlated else self.clusters()
        out: dict = {}
        for comp in comps:
            cid = min(comp)
            for m in comp:
                out[m] = cid
        return out

    def cluster_of(self, market_id: str, *, correlated: bool = False) -> str:
        return self.cluster_map(correlated=correlated).get(market_id, market_id)

    def same_event_groups(self) -> list:
        return self.clusters()

    def cluster_is_exhaustive(self, market_ids) -> bool:
        ids = set(market_ids)
        return any(e.edge_type == EdgeType.EXHAUSTIVE and e.src in ids and e.dst in ids
                   for e in self.edges)

    # -- scores --------------------------------------------------------------
    def confidence(self) -> float:
        if not self.edges:
            return 0.0
        return round(sum(e.confidence for e in self.edges) / len(self.edges), 4)

    def ambiguity(self) -> float:
        if not self.edges:
            return 0.0
        return round(sum(e.ambiguity for e in self.edges) / len(self.edges), 4)

    def grouping_precision(self, labels: dict) -> float:
        """Precision of structural same-event grouping vs a ``{market_id: event}``
        label map. A labelled market is correct when its structural cluster's
        labelled members exactly equal the set of markets sharing its label."""
        if not labels:
            return 1.0
        cmap = self.cluster_map()
        by_label: dict = {}
        for m, lbl in labels.items():
            by_label.setdefault(lbl, set()).add(m)
        comp_members: dict = {}
        for m, cid in cmap.items():
            comp_members.setdefault(cid, set()).add(m)
        correct = 0
        for m, lbl in labels.items():
            cid = cmap.get(m)
            members = comp_members.get(cid, {m})
            labelled = {x for x in members if x in labels}
            if labelled == by_label[lbl]:
                correct += 1
        return round(correct / len(labels), 4)

    def to_report(self) -> dict:
        by_type: dict = {t: 0 for t in EdgeType.ALL}
        for e in self.edges:
            by_type[e.edge_type] = by_type.get(e.edge_type, 0) + 1
        assets: dict = {}
        for n in self.nodes.values():
            if n.asset:
                assets[n.asset] = assets.get(n.asset, 0) + 1
        struct = sorted([sorted(c) for c in self.clusters()])
        corr = sorted([sorted(c) for c in self.correlated_clusters()])
        return {
            "node_count": len(self.nodes), "edge_count": len(self.edges),
            "edges_by_type": by_type, "clusters": struct,
            "correlated_clusters": corr, "cluster_count": len(struct),
            "correlated_cluster_count": len(corr),
            "confidence": self.confidence(), "ambiguity": self.ambiguity(),
            "assets": assets,
        }


def diversified_selection(candidates: list, graph: MarketDependencyGraph, *,
                          max_per_cluster: int = 2, correlated: bool = True) -> list:
    """Pick candidates spread across graph clusters (aggressive diversification).

    Orders by descending ``score`` / ``feedback_value`` (tie-break market_id) and
    caps picks per correlated cluster so aggressive mode does not repeatedly trade
    one correlated cluster."""
    cmap = graph.cluster_map(correlated=correlated)
    ordered = sorted(
        candidates,
        key=lambda c: (-float(c.get("score", c.get("feedback_value", 0.0)) or 0.0),
                       str(c.get("market_id", ""))))
    per: dict = {}
    out: list = []
    for c in ordered:
        mid = str(c.get("market_id", ""))
        cid = cmap.get(mid, mid)
        if per.get(cid, 0) >= max_per_cluster:
            continue
        per[cid] = per.get(cid, 0) + 1
        out.append(c)
    return out


class ClusterExposureNetter:
    """Net same-event hedges + cap correlated-cluster exposure (Risk / Portfolio
    Optimization). Gross is the capital at risk; net credits exhaustive same-event
    hedges (at most one leg wins). Overexposure is flagged on GROSS so correlated
    cross-event positions (not truly hedged) cannot quietly concentrate risk."""

    _LONG = ("BUY", "YES", "UP", "LONG")

    def __init__(self, graph: MarketDependencyGraph, *,
                 max_cluster_exposure_usd: float = 50.0, correlated: bool = True):
        self.graph = graph
        self.cap = float(max_cluster_exposure_usd)
        self._corr = graph.cluster_map(correlated=correlated)
        self._struct = graph.cluster_map(correlated=False)
        self._exhaustive_struct = {
            self._struct.get(e.src) for e in graph.edges if e.edge_type == EdgeType.EXHAUSTIVE
        } | {
            self._struct.get(e.dst) for e in graph.edges if e.edge_type == EdgeType.EXHAUSTIVE
        }

    def _cluster_id(self, market_id: str) -> str:
        return self._corr.get(str(market_id), str(market_id))

    def cluster_exposures(self, positions: list) -> dict:
        clusters: dict = {}
        for p in positions:
            clusters.setdefault(self._cluster_id(p.get("market_id")), []).append(p)
        out: dict = {}
        for cid, ps in clusters.items():
            gross = sum(abs(float(p.get("notional", 0.0) or 0.0)) for p in ps)
            sub: dict = {}
            for p in ps:
                sid = self._struct.get(str(p.get("market_id")), str(p.get("market_id")))
                sub.setdefault(sid, []).append(p)
            net = 0.0
            for sid, sps in sub.items():
                sg = sum(abs(float(p.get("notional", 0.0) or 0.0)) for p in sps)
                all_long = all(str(p.get("side", "BUY")).upper() in self._LONG for p in sps)
                if sid in self._exhaustive_struct and len(sps) >= 2 and all_long:
                    net += max(abs(float(p.get("notional", 0.0) or 0.0)) for p in sps)
                else:
                    net += sg
            out[cid] = {"gross": round(gross, 6), "net": round(net, 6),
                        "markets": sorted(str(p.get("market_id")) for p in ps),
                        "exceeds_cap": gross > self.cap}
        return out

    def overexposed(self, positions: list) -> list:
        return [cid for cid, v in self.cluster_exposures(positions).items()
                if v["gross"] > self.cap]

    def would_breach(self, positions: list, market_id: str, notional: float,
                     side: str = "BUY") -> bool:
        cid = self._cluster_id(market_id)
        cur = sum(abs(float(p.get("notional", 0.0) or 0.0)) for p in positions
                  if self._cluster_id(p.get("market_id")) == cid)
        return (cur + abs(float(notional or 0.0))) > self.cap
