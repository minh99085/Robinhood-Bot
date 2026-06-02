"""Polymarket Bregman simplex-grouping tests (deterministic, offline).

Quant scope: Data Acquisition & Feature Engineering — verifies binary YES/NO
pairs, mutually-exclusive multi-outcome groups, exhaustive event groups, linked
markets, range buckets, and synthetic bundles are constructed + validated
correctly for downstream certification.
"""

from __future__ import annotations

from engine.markets import universe_manager as um
from engine.training.bregman_grouping import (
    SimplexGroup,
    SimplexLeg,
    build_binary_group,
    build_event_group,
    build_range_bucket_group,
    build_synthetic_bundle,
    group_markets,
    validate_simplex,
)

from tests._pmtrain_helpers import market

_NOW = 1_000_000.0


def _leg(outcome, ask, **kw):
    return SimplexLeg(market_id=kw.pop("market_id", "m"), outcome=outcome,
                      token_id=kw.pop("token_id", f"tok-{outcome}"), ask=ask,
                      depth_usd=kw.pop("depth_usd", 1000.0), **kw)


def _rec(i, threshold=None, *, group=None, bid=0.28, ask=0.30, complete=False):
    raw = market(i, bid=bid, ask=ask, category="crypto", group=group, now=_NOW)
    if threshold is not None:
        raw["question"] = f"Will price be above ${threshold}?"
    if complete:
        raw["negRiskComplete"] = True       # explicit full-outcome-set marker
    return um.MarketRecord.from_raw(raw, now=_NOW)


# --------------------------------------------------------------------------- #
def test_binary_group_has_two_legs_and_validates():
    grp = build_binary_group(_rec(0))
    assert grp.group_type == "binary_yes_no"
    assert len(grp.legs) == 2
    ok, reason = validate_simplex(grp)
    assert ok, reason
    # NO leg synthesized as 1 - yes_bid when no real NO book is supplied
    assert any(l.outcome == "NO" and l.synthetic_price for l in grp.legs)


def test_binary_group_uses_real_no_ask_when_supplied():
    grp = build_binary_group(_rec(0), no_ask=0.69)
    no_leg = [l for l in grp.legs if l.outcome == "NO"][0]
    assert no_leg.ask == 0.69 and not no_leg.synthetic_price


def test_validate_rejects_single_leg():
    grp = SimplexGroup(group_id="g", group_type="synthetic_bundle",
                       legs=[_leg("A", 0.5)])
    ok, reason = validate_simplex(grp)
    assert not ok and reason == "insufficient_legs"


def test_validate_rejects_duplicate_legs():
    grp = SimplexGroup(group_id="g", group_type="mutually_exclusive",
                       legs=[_leg("A", 0.5, token_id="t1"),
                             _leg("A", 0.5, token_id="t1")])
    ok, reason = validate_simplex(grp)
    assert not ok and reason == "duplicate_legs"


def test_validate_rejects_non_exhaustive():
    grp = SimplexGroup(group_id="g", group_type="mutually_exclusive",
                       legs=[_leg("A", 0.4, token_id="t1"),
                             _leg("B", 0.4, token_id="t2")],
                       exhaustive=False)
    ok, reason = validate_simplex(grp)
    assert not ok and reason == "not_exhaustive"


def test_exhaustive_event_group_from_records():
    recs = [_rec(0, group="elect"), _rec(1, group="elect"), _rec(2, group="elect")]
    grp = build_event_group(recs, group_id="event:elect")
    assert grp.group_type == "exhaustive_event"
    assert len(grp.legs) == 3
    assert grp.mutually_exclusive and grp.exhaustive
    ok, _ = validate_simplex(grp)
    assert ok
    # observed prices = each market's YES ask
    assert all(abs(p - 0.30) < 1e-9 for p in grp.observed_prices)


def test_group_markets_splits_events_and_binaries():
    recs = [_rec(0, group="elect", complete=True),            # complete 2-leg event
            _rec(1, group="elect", complete=True),
            _rec(2)]                                          # one standalone binary
    groups = group_markets(recs)
    types = sorted(g.group_type for g in groups)
    assert "exhaustive_event" in types
    assert "binary_yes_no" in types


def test_group_markets_without_completeness_marker_is_not_exhaustive():
    # an incomplete scan must NOT be labelled exhaustive (no false hedge)
    recs = [_rec(0, group="elect"), _rec(1, group="elect")]
    grp = group_markets(recs)[0]
    assert grp.group_type == "mutually_exclusive"
    assert grp.exhaustive is False
    ok, reason = validate_simplex(grp)
    assert not ok and reason == "not_exhaustive"


def test_range_bucket_group():
    grp = build_range_bucket_group("cpi", [
        {"label": "<2%", "ask": 0.2, "depth_usd": 500},
        {"label": "2-3%", "ask": 0.5, "depth_usd": 500},
        {"label": ">3%", "ask": 0.25, "depth_usd": 500}])
    assert grp.group_type == "range_buckets" and len(grp.legs) == 3
    ok, _ = validate_simplex(grp)
    assert ok
    assert abs(grp.implied_sum - 0.95) < 1e-9


def test_synthetic_bundle():
    grp = build_synthetic_bundle("bundle1", [
        _leg("X", 0.4, token_id="x"), _leg("Y", 0.4, token_id="y")])
    assert grp.group_type == "synthetic_bundle"
    ok, _ = validate_simplex(grp)
    assert ok


def test_linked_markets_group_via_group_type():
    grp = SimplexGroup(group_id="link:eth", group_type="linked_markets",
                       legs=[_leg("A", 0.45, token_id="a"),
                             _leg("B", 0.45, token_id="b")])
    ok, _ = validate_simplex(grp)
    assert ok and grp.group_type == "linked_markets"
