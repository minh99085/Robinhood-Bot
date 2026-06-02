"""Same-event grouping precision via the dependency graph.

The graph's structural clusters recover same-event outcome sets; this measures
grouping precision against a labelled fixture and checks complement / range /
mutually-exclusive / exhaustive classification. PAPER-ONLY; offline.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.training.dependency_graph import EdgeType, MarketDependencyGraph


def rec(mid, *, group=None, category="crypto", question="", yes=0.5,
        tokens=("t1", "t2"), raw_extra=None):
    raw = {"bestBid": yes - 0.01, "bestAsk": yes + 0.01}
    if raw_extra:
        raw.update(raw_extra)
    return SimpleNamespace(
        market_id=mid, group_key=group or mid, category=category, question=question,
        yes_price=yes, clob_token_ids=list(tokens), spread=0.02, top_depth_usd=500.0,
        has_resolution_text=True, book_age_s=1.0, end_ts=None, raw=raw)


def _fixture():
    # Three events with known structure + two unrelated singletons.
    return [
        # exhaustive 3-way (complete; prices ~1)
        rec("e1a", group="E1", yes=0.34, raw_extra={"outcomeCount": 3}),
        rec("e1b", group="E1", yes=0.33, raw_extra={"outcomeCount": 3}),
        rec("e1c", group="E1", yes=0.33, raw_extra={"outcomeCount": 3}),
        # mutually-exclusive but NOT complete (no completeness signal; prices < 1)
        rec("e2a", group="E2", yes=0.2),
        rec("e2b", group="E2", yes=0.2),
        # range-bucket event
        rec("e3a", group="E3", question="Will CPI be between 2% and 3%?"),
        rec("e3b", group="E3", question="Will CPI be above 3%?"),
        # singletons
        rec("s1", group="S1"), rec("s2", group="S2"),
    ]


def test_same_event_clusters_match_groups():
    g = MarketDependencyGraph.build(_fixture())
    groups = g.same_event_groups()
    # map market -> its group set
    by_market = {}
    for grp in groups:
        for m in grp:
            by_market[m] = frozenset(grp)
    assert by_market["e1a"] == frozenset({"e1a", "e1b", "e1c"})
    assert by_market["e2a"] == frozenset({"e2a", "e2b"})
    assert by_market["e3a"] == frozenset({"e3a", "e3b"})
    assert by_market["s1"] == frozenset({"s1"})


def test_complement_for_binary_singletons():
    g = MarketDependencyGraph.build([rec("s1", tokens=("yes", "no"))])
    assert g.has_edge("s1", "s1", EdgeType.COMPLEMENT)


def test_exhaustive_vs_mutually_exclusive_classification():
    g = MarketDependencyGraph.build(_fixture())
    # E1 is exhaustive (complete), E2 is only mutually-exclusive
    assert g.has_edge("e1a", "e1b", EdgeType.EXHAUSTIVE)
    assert g.has_edge("e2a", "e2b", EdgeType.MUTUALLY_EXCLUSIVE)
    assert not g.has_edge("e2a", "e2b", EdgeType.EXHAUSTIVE)


def test_range_bucket_classification():
    g = MarketDependencyGraph.build(_fixture())
    assert g.has_edge("e3a", "e3b", EdgeType.RANGE_BUCKET)


def test_grouping_precision_on_fixture():
    g = MarketDependencyGraph.build(_fixture())
    labels = {  # market_id -> expected event group key
        "e1a": "E1", "e1b": "E1", "e1c": "E1",
        "e2a": "E2", "e2b": "E2", "e3a": "E3", "e3b": "E3",
        "s1": "S1", "s2": "S2"}
    precision = g.grouping_precision(labels)
    assert precision == 1.0
