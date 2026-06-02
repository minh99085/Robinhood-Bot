"""Graph-fed Bregman simplex grouping.

The dependency graph's structural same-event clusters drive combinatorial
SimplexGroup construction so the certification engine sees complete, all-leg
outcome sets. Exhaustive events become certifiable exhaustive groups; incomplete
events stay mutually-exclusive (never mislabelled as a full hedge). PAPER-ONLY.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.training.bregman_grouping import groups_from_graph, validate_simplex
from engine.training.dependency_graph import MarketDependencyGraph


def rec(mid, *, group=None, yes=0.5, tokens=None, raw_extra=None):
    raw = {"bestBid": yes - 0.01, "bestAsk": yes + 0.01}
    if raw_extra:
        raw.update(raw_extra)
    return SimpleNamespace(
        market_id=mid, group_key=group or mid, category="crypto", question="",
        yes_price=yes, clob_token_ids=list(tokens or [f"{mid}_y", f"{mid}_n"]),
        spread=0.02, top_depth_usd=500.0, has_resolution_text=True,
        book_age_s=1.0, end_ts=None, raw=raw)


def test_exhaustive_event_builds_certifiable_group():
    recs = [rec("a", group="E", yes=0.34, raw_extra={"outcomeCount": 3}),
            rec("b", group="E", yes=0.33, raw_extra={"outcomeCount": 3}),
            rec("c", group="E", yes=0.33, raw_extra={"outcomeCount": 3})]
    g = MarketDependencyGraph.build(recs)
    groups = groups_from_graph(g, recs)
    ev = [grp for grp in groups if grp.group_type == "exhaustive_event"]
    assert len(ev) == 1
    grp = ev[0]
    assert len(grp.legs) == 3                       # combinatorial: all legs present
    assert grp.exhaustive is True
    ok, reason = validate_simplex(grp)
    assert ok, reason


def test_incomplete_event_stays_mutually_exclusive():
    recs = [rec("a", group="E2", yes=0.2), rec("b", group="E2", yes=0.2)]
    g = MarketDependencyGraph.build(recs)
    groups = groups_from_graph(g, recs)
    me = [grp for grp in groups if grp.group_type == "mutually_exclusive"]
    assert len(me) == 1
    assert me[0].exhaustive is False                # never mislabelled as a full hedge


def test_singleton_builds_binary_group():
    recs = [rec("solo", tokens=("y", "n"))]
    g = MarketDependencyGraph.build(recs)
    groups = groups_from_graph(g, recs, include_binary=True)
    assert any(grp.group_type == "binary_yes_no" and len(grp.legs) == 2
               for grp in groups)


def test_group_market_ids_match_graph_cluster():
    recs = [rec("a", group="E", yes=0.34, raw_extra={"outcomeCount": 3}),
            rec("b", group="E", yes=0.33, raw_extra={"outcomeCount": 3}),
            rec("c", group="E", yes=0.33, raw_extra={"outcomeCount": 3})]
    g = MarketDependencyGraph.build(recs)
    grp = [gr for gr in groups_from_graph(g, recs) if gr.group_type == "exhaustive_event"][0]
    leg_markets = {l.market_id for l in grp.legs}
    assert leg_markets == {"a", "b", "c"}


def test_grouping_precision_against_labels():
    recs = [rec("a", group="E", yes=0.34, raw_extra={"outcomeCount": 3}),
            rec("b", group="E", yes=0.33, raw_extra={"outcomeCount": 3}),
            rec("c", group="E", yes=0.33, raw_extra={"outcomeCount": 3}),
            rec("d", group="F", yes=0.2), rec("e", group="F", yes=0.2)]
    g = MarketDependencyGraph.build(recs)
    groups = groups_from_graph(g, recs)
    labels = {"event:E": "exhaustive_event", "event:F": "mutually_exclusive"}
    scored = [gr for gr in groups if gr.group_id in labels]
    correct = sum(1 for gr in scored if gr.group_type == labels[gr.group_id])
    precision = correct / len(scored) if scored else 0.0
    assert precision == 1.0
