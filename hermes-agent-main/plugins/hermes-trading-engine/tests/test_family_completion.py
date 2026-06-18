"""Priority-1: targeted event-family completion.

Polymarket event payloads embed the full sibling set (with real clobTokenIds) in
raw['events'][0]['markets']. expand_event_families appends authoritative records for the
siblings missing from the scan slice — carrying only token ids + shared event context
(no fabricated prices) — so multi-outcome MECE families assemble and become certifiable.
"""

from __future__ import annotations

import time

import pytest

from engine.markets import universe_manager as um
from engine.training.family_completion import expand_event_families
from engine.training.bregman_grouping import group_markets, _group_is_exhaustive


def _sibling(mid, tok, label):
    return {"id": mid, "clobTokenIds": [f"{tok}A", f"{tok}B"], "question": label,
            "groupItemTitle": label, "outcomePrices": ["0.30", "0.70"]}


def _event_markets(n):
    return [_sibling(f"m{i}", f"tok{i}", f"Outcome {i}") for i in range(n)]


def _scanned_member(i, *, event_id, all_markets, now):
    # one scanned sibling that embeds the FULL event (with every sibling market listed)
    raw = dict(_sibling(f"m{i}", f"tok{i}", f"Outcome {i}"))
    raw["events"] = [{"id": event_id, "slug": event_id, "markets": all_markets}]
    raw["bestAsk"] = 0.30
    raw["bestBid"] = 0.28
    raw["liquidityNum"] = 500.0
    return um.MarketRecord.from_raw(raw, now=now)


def test_missing_siblings_are_appended_from_event_metadata():
    now = time.time()
    full = _event_markets(4)                     # event declares 4 outcomes
    scanned = [_scanned_member(0, event_id="E1", all_markets=full, now=now)]  # only 1 scanned
    out, tel = expand_event_families(scanned, now=now)
    assert tel["family_completion_families_with_gap"] == 1
    assert tel["family_completion_missing_siblings_added"] == 3   # m1,m2,m3 added
    assert tel["family_completion_records_out"] == 4
    ids = {r.market_id for r in out}
    assert ids == {"m0", "m1", "m2", "m3"}


def test_completed_family_assembles_and_is_exhaustive():
    now = time.time()
    full = _event_markets(3)
    scanned = [_scanned_member(0, event_id="E2", all_markets=full, now=now)]
    out, _ = expand_event_families(scanned, now=now)
    groups = group_markets(out, include_binary=False)
    fam = max(groups, key=lambda g: len(g.legs))
    assert len(fam.legs) == 3                     # all siblings assembled into ONE family
    assert fam.exhaustive is True                 # declared(3) == legs(3) -> certifiable


def test_no_fabricated_prices_on_added_legs():
    now = time.time()
    full = _event_markets(3)
    scanned = [_scanned_member(0, event_id="E3", all_markets=full, now=now)]
    out, _ = expand_event_families(scanned, now=now)
    added = [r for r in out if r.market_id in ("m1", "m2")]
    # synthesized records carry real token ids but NO order book (no bestBid/bestAsk)
    for r in added:
        assert r.clob_token_ids and len(r.clob_token_ids) >= 1
        assert (r.raw.get("bestAsk") in (None, "", 0, 0.0)) or r.yes_price is not None
        # crucially: no bestBid/bestAsk injected -> leg ask stays unset until hydration
        assert "bestAsk" not in r.raw and "bestBid" not in r.raw


def test_caps_bound_new_records():
    now = time.time()
    full = _event_markets(10)
    scanned = [_scanned_member(0, event_id="E4", all_markets=full, now=now)]
    out, tel = expand_event_families(scanned, now=now, max_total_new=3, max_per_family=8)
    assert tel["family_completion_missing_siblings_added"] == 3
    assert tel["family_completion_capped"] is True


def test_per_family_cap():
    now = time.time()
    full = _event_markets(10)
    scanned = [_scanned_member(0, event_id="E5", all_markets=full, now=now)]
    out, tel = expand_event_families(scanned, now=now, max_total_new=40, max_per_family=2)
    assert tel["family_completion_missing_siblings_added"] == 2


def test_low_liquidity_family_skipped():
    now = time.time()
    full = _event_markets(3)
    raw = dict(_sibling("m0", "tok0", "Outcome 0"))
    raw["events"] = [{"id": "E6", "slug": "E6", "markets": full}]
    raw["liquidityNum"] = 5.0                      # illiquid family
    scanned = [um.MarketRecord.from_raw(raw, now=now)]
    out, tel = expand_event_families(scanned, now=now, min_family_liquidity_usd=100.0)
    assert tel["family_completion_skipped_low_liquidity"] == 1
    assert tel["family_completion_missing_siblings_added"] == 0


def test_lone_market_without_event_siblings_is_noop():
    now = time.time()
    raw = dict(_sibling("solo", "tokS", "Solo"))   # no 'events' embedded
    scanned = [um.MarketRecord.from_raw(raw, now=now)]
    out, tel = expand_event_families(scanned, now=now)
    assert tel["family_completion_families_examined"] == 0
    assert len(out) == 1


def test_already_complete_family_adds_nothing():
    now = time.time()
    full = _event_markets(3)
    scanned = [_scanned_member(i, event_id="E7", all_markets=full, now=now) for i in range(3)]
    out, tel = expand_event_families(scanned, now=now)
    assert tel["family_completion_missing_siblings_added"] == 0
    assert tel["family_completion_families_with_gap"] == 0
