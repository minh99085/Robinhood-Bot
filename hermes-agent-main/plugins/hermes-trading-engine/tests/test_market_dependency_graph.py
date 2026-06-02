"""Polymarket market dependency graph — nodes, typed edges, clusters, netting.

The graph models markets/outcomes as nodes and dependencies as typed edges
(same event, complement, mutually exclusive, exhaustive, range bucket, related
macro/crypto asset, correlated category) each carrying a confidence + ambiguity.
It feeds Bregman grouping, risk exposure netting, and aggressive diversification.
PAPER-ONLY; deterministic; offline.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.training.dependency_graph import (
    ClusterExposureNetter,
    EdgeType,
    MarketDependencyGraph,
    detect_asset,
    diversified_selection,
)


def rec(mid, *, group=None, category="crypto", question="", yes=0.5,
        tokens=("t1", "t2"), spread=0.02, depth=500.0, amb=None,
        has_text=True, raw_extra=None):
    raw = {"bestBid": yes - 0.01, "bestAsk": yes + 0.01}
    if amb is not None:
        raw["ambiguity"] = amb
    if raw_extra:
        raw.update(raw_extra)
    return SimpleNamespace(
        market_id=mid, group_key=group or mid, category=category, question=question,
        yes_price=yes, clob_token_ids=list(tokens), spread=spread, top_depth_usd=depth,
        has_resolution_text=has_text, book_age_s=1.0, end_ts=None, raw=raw)


# --- asset detection (Data Acquisition / Preprocessing) ---------------------
def test_detect_asset():
    assert detect_asset("Will Bitcoin close above $80k?") == "BTC"
    assert detect_asset("Will ETH reach a new ATH?") == "ETH"
    assert detect_asset("Will the Fed cut interest rates at the FOMC?") == "FED_RATES"
    assert detect_asset("Will the Lakers win the title?") == ""


# --- node + edge construction ----------------------------------------------
def test_nodes_created_for_each_market():
    g = MarketDependencyGraph.build([rec("m1"), rec("m2")])
    assert set(g.nodes) == {"m1", "m2"}


def test_binary_market_has_complement_edge():
    g = MarketDependencyGraph.build([rec("m1", tokens=("yes", "no"))])
    assert g.has_edge("m1", "m1", EdgeType.COMPLEMENT)


def test_same_event_and_mutually_exclusive_edges():
    g = MarketDependencyGraph.build([
        rec("a", group="evt1", yes=0.3), rec("b", group="evt1", yes=0.3),
        rec("c", group="evt1", yes=0.3)])
    assert g.has_edge("a", "b", EdgeType.SAME_EVENT)
    assert g.has_edge("a", "b", EdgeType.MUTUALLY_EXCLUSIVE)
    assert g.has_edge("b", "c", EdgeType.SAME_EVENT)


def test_exhaustive_edge_when_prices_sum_to_one_and_complete():
    g = MarketDependencyGraph.build([
        rec("a", group="evt2", yes=0.5, raw_extra={"outcomeCount": 2}),
        rec("b", group="evt2", yes=0.5, raw_extra={"outcomeCount": 2})])
    assert g.has_edge("a", "b", EdgeType.EXHAUSTIVE)


def test_range_bucket_edges():
    g = MarketDependencyGraph.build([
        rec("r1", group="evtr", question="Will BTC be between 70k and 80k?"),
        rec("r2", group="evtr", question="Will BTC be above 80k?")])
    assert g.has_edge("r1", "r2", EdgeType.RANGE_BUCKET)


def test_related_asset_edge_across_events():
    g = MarketDependencyGraph.build([
        rec("x", group="e1", question="Will Bitcoin top 80k in June?"),
        rec("y", group="e2", question="Will BTC dip below 60k in July?")])
    assert g.has_edge("x", "y", EdgeType.RELATED_ASSET)


def test_correlated_category_edge_across_events():
    g = MarketDependencyGraph.build([
        rec("p", group="e1", category="politics", question="Will candidate A win?"),
        rec("q", group="e2", category="politics", question="Will measure B pass?")])
    assert g.has_edge("p", "q", EdgeType.CORRELATED_CATEGORY)


def test_edge_confidence_and_ambiguity_bounded():
    g = MarketDependencyGraph.build([rec("a", group="e", amb=0.4),
                                     rec("b", group="e", amb=0.4)])
    for e in g.edges:
        assert 0.0 <= e.confidence <= 1.0
        assert 0.0 <= e.ambiguity <= 1.0
    assert 0.0 <= g.confidence() <= 1.0
    assert 0.0 <= g.ambiguity() <= 1.0


# --- clusters ---------------------------------------------------------------
def test_structural_clusters_group_same_event():
    g = MarketDependencyGraph.build([
        rec("a", group="e1"), rec("b", group="e1"),
        rec("c", group="e2"), rec("d", group="e2"), rec("e", group="e3")])
    clusters = g.clusters()  # structural (same-event family)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2, 2]


def test_correlated_clusters_merge_same_asset_across_events():
    g = MarketDependencyGraph.build([
        rec("btc1", group="e1", question="Will Bitcoin top 80k?"),
        rec("btc2", group="e2", question="Will BTC dip below 60k?"),
        rec("eth1", group="e3", question="Will Ethereum top 5k?")])
    cmap = g.cluster_map(correlated=True)
    # the two BTC markets share a correlated cluster; ETH is separate
    assert cmap["btc1"] == cmap["btc2"]
    assert cmap["eth1"] != cmap["btc1"]


def test_to_report_has_expected_keys():
    g = MarketDependencyGraph.build([rec("a", group="e"), rec("b", group="e")])
    rpt = g.to_report()
    for k in ("node_count", "edge_count", "edges_by_type", "clusters",
              "correlated_clusters", "confidence", "ambiguity"):
        assert k in rpt


# --- aggressive diversification --------------------------------------------
def test_diversified_selection_caps_per_cluster():
    # 4 BTC markets (one correlated cluster) + 2 singletons. max 1 per cluster.
    recs = [rec(f"btc{i}", group=f"eb{i}", question="Bitcoin price?") for i in range(4)]
    recs += [rec("sol", group="es", question="Solana price?"),
             rec("vote", group="ev", category="politics", question="Election winner?")]
    g = MarketDependencyGraph.build(recs)
    cands = [{"market_id": r.market_id, "score": 1.0} for r in recs]
    picked = diversified_selection(cands, g, max_per_cluster=1)
    ids = [c["market_id"] for c in picked]
    # only ONE of the 4 BTC markets may be picked (rest are the same cluster)
    assert sum(1 for i in ids if i.startswith("btc")) == 1
    assert "sol" in ids and "vote" in ids


# --- risk exposure netting: prevent correlated-cluster overexposure ---------
def test_netter_flags_concentrated_correlated_cluster():
    recs = [rec(f"btc{i}", group=f"eb{i}", question="Bitcoin price?") for i in range(5)]
    g = MarketDependencyGraph.build(recs)
    netter = ClusterExposureNetter(g, max_cluster_exposure_usd=30.0)
    positions = [{"market_id": f"btc{i}", "notional": 10.0, "side": "BUY"} for i in range(5)]
    over = netter.overexposed(positions)
    assert len(over) == 1                       # the single BTC cluster is over $30
    assert netter.would_breach(positions, "btc0", 5.0)


def test_netter_ok_when_diversified():
    recs = [rec("btc", group="e1", question="Bitcoin?"),
            rec("eth", group="e2", question="Ethereum?"),
            rec("vote", group="e3", category="politics", question="Election?")]
    g = MarketDependencyGraph.build(recs)
    netter = ClusterExposureNetter(g, max_cluster_exposure_usd=30.0)
    positions = [{"market_id": "btc", "notional": 10.0, "side": "BUY"},
                 {"market_id": "eth", "notional": 10.0, "side": "BUY"},
                 {"market_id": "vote", "notional": 10.0, "side": "BUY"}]
    assert netter.overexposed(positions) == []


def test_netting_credits_same_event_exhaustive_hedge():
    # 3-leg exhaustive event, all YES: gross 30, but at most one wins -> net < gross.
    recs = [rec(f"l{i}", group="evt", yes=0.33, raw_extra={"outcomeCount": 3}) for i in range(3)]
    g = MarketDependencyGraph.build(recs)
    netter = ClusterExposureNetter(g, max_cluster_exposure_usd=100.0)
    positions = [{"market_id": f"l{i}", "notional": 10.0, "side": "BUY"} for i in range(3)]
    exp = netter.cluster_exposures(positions)
    cl = next(iter(exp.values()))
    assert cl["gross"] == 30.0
    assert cl["net"] < cl["gross"]              # exhaustive hedge nets down
