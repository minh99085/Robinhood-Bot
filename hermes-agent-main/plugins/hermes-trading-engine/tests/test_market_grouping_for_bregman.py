"""Polymarket event-grouping tests for Bregman market structure (offline).

Covers binary complements, mutually exclusive / exhaustive event buckets,
scalar/range buckets, neg-risk groups, same-event linkage, completeness +
all-leg token availability, Bregman suitability scoring, group coverage, and
type-detection precision. Structure only — no trading is performed.
"""

from __future__ import annotations

import time

import pytest

from engine.markets import universe_manager as um
from engine.training.market_grouping import (
    GROUP_TYPES, bregman_suitability, detection_precision, group_markets,
    grouping_metrics)
from tests._pmtrain_helpers import clean_live_env, market


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def _mk(i, *, group=None, bid=0.32, ask=0.34, tokens=("a", "b"), **over):
    now = time.time()
    raw = market(i, bid=bid, ask=ask, group=group, now=now)
    raw["clobTokenIds"] = [f"tok{i}{t}" for t in tokens] if tokens else []
    raw.update(over)
    return um.MarketRecord.from_raw(raw, now=now)


def test_single_market_is_binary_complement_and_complete():
    groups = group_markets([_mk(0)])
    assert len(groups) == 1
    g = groups[0]
    assert g.group_type == "binary_complement"
    assert g.complete and g.all_tokens_available
    assert len(g.leg_token_ids) >= 2


def test_single_market_one_token_is_same_event_incomplete():
    g = group_markets([_mk(0, tokens=("a",))])[0]
    assert g.group_type == "same_event"
    assert not g.complete


def test_exhaustive_when_yes_prices_sum_to_one():
    # three legs in one event, each ~0.33 -> sum ~0.99 (a partition)
    recs = [_mk(i, group="evt-x", bid=0.32, ask=0.34) for i in range(3)]
    g = group_markets(recs)[0]
    assert g.n_legs == 3
    assert g.group_type == "exhaustive"
    assert g.complete and g.all_tokens_available


def test_mutually_exclusive_when_prices_do_not_partition():
    recs = [_mk(i, group="evt-y", bid=0.58, ask=0.62) for i in range(3)]  # ~0.6 each
    g = group_markets(recs)[0]
    assert g.group_type == "mutually_exclusive"


def test_scalar_range_detected_from_question_text():
    r = _mk(0, group="evt-z")
    r.raw["question"] = "Will the index close between 4000 and 4500?"
    r.question = r.raw["question"]
    g = group_markets([r, _mk(1, group="evt-z")])[0]
    assert g.group_type == "scalar_range"


def test_neg_risk_group_detected():
    r0 = _mk(0, group="evt-n")
    r0.raw["negRisk"] = True
    g = group_markets([r0, _mk(1, group="evt-n")])[0]
    assert g.group_type == "neg_risk"


def test_incomplete_group_when_a_leg_has_no_tokens():
    recs = [_mk(0, group="evt-i"), _mk(1, group="evt-i", tokens=None)]
    g = group_markets(recs)[0]
    assert not g.all_tokens_available
    assert not g.complete


def test_bregman_suitability_prefers_complete_tight_deep_group():
    complete = group_markets(
        [_mk(i, group="good", bid=0.32, ask=0.34, topDepthUsd=3000) for i in range(3)])[0]
    poor = group_markets(
        [_mk(0, group="bad", bid=0.20, ask=0.55, tokens=None, topDepthUsd=10)])[0]
    s_good = bregman_suitability(complete)
    s_bad = bregman_suitability(poor)
    assert 0.0 <= s_bad <= s_good <= 1.0
    assert s_good > s_bad


def test_oracle_relevance_lifts_suitability():
    g = group_markets([_mk(i, group="o", bid=0.32, ask=0.34) for i in range(3)])[0]
    assert bregman_suitability(g, oracle_relevance=1.0) > bregman_suitability(g)


def test_group_coverage_and_types_are_reported():
    recs = ([_mk(i, group="evt-c", bid=0.32, ask=0.34) for i in range(3)]
            + [_mk(50)])  # one standalone binary
    groups = group_markets(recs)
    gm = grouping_metrics(recs, groups)
    assert gm["records"] == 4
    assert gm["groups_detected"] == 2
    assert 0.0 < gm["group_coverage"] <= 1.0
    assert set(gm["by_type"]) == set(GROUP_TYPES)


def test_detection_precision_on_labeled_fixture():
    recs = ([_mk(i, group="ex", bid=0.32, ask=0.34) for i in range(3)]
            + [_mk(99)])
    groups = group_markets(recs)
    labels = {}
    for g in groups:
        labels[g.group_key] = "exhaustive" if g.n_legs == 3 else "binary_complement"
    assert detection_precision(groups, labels) == 1.0
    # empty label set -> nothing to disprove
    assert detection_precision(groups, {}) == 1.0
