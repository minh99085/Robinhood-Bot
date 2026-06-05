"""Tests for engine.fill_realism (realistic-fill scoring + fantasy rejection)."""

from __future__ import annotations

from engine.fill_realism import (
    FillModel,
    arbitrage_execution_costs,
    assess_fill,
    fill_realism_score,
    is_fantasy_fill,
    spread_frac,
    walk_book,
)


def test_arbitrage_execution_costs_spread_slippage_fantasy():
    legs = [{"ask": 0.40, "bid": 0.38, "requested_shares": 100, "available_depth": 100},
            {"ask": 0.40, "bid": 0.38, "requested_shares": 100, "available_depth": 5}]
    out = arbitrage_execution_costs(legs, slippage_bps=100)
    # half-spread per leg = 0.01 -> 0.02 total
    assert abs(out["spread_cost_per_set"] - 0.02) < 1e-9
    # slippage = 1% of (0.40+0.40)=0.008
    assert abs(out["slippage_cost_per_set"] - 0.008) < 1e-9
    # leg 2 requests 100 but only 5 available -> 1 fantasy fill
    assert out["fantasy_fills_rejected"] == 1


def test_arbitrage_execution_costs_clean():
    legs = [{"ask": 0.5, "bid": 0.5, "requested_shares": 10, "available_depth": 100}]
    out = arbitrage_execution_costs(legs)
    assert out["spread_cost_per_set"] == 0.0
    assert out["fantasy_fills_rejected"] == 0


def test_spread_frac_basic():
    assert spread_frac(0.49, 0.51) > 0
    assert abs(spread_frac(0.49, 0.51) - (0.02 / 0.5)) < 1e-9
    assert spread_frac(0, 0) == 0.0
    assert spread_frac(0.6, 0.4) == 0.0  # crossed -> 0


def test_walk_book_partial_and_full():
    levels = [(0.50, 100), (0.51, 100)]
    filled, avg = walk_book(150, levels)
    assert filled == 150
    # 100@0.50 + 50@0.51
    assert abs(avg - ((100 * 0.50 + 50 * 0.51) / 150)) < 1e-9
    filled2, _ = walk_book(500, levels)
    assert filled2 == 200  # only 200 available -> partial


def test_realistic_fill_passes():
    r = assess_fill(requested_size=50, ask=0.50, ask_depth=1000, bid=0.49)
    assert r.fantasy is False
    assert r.filled_size == 50
    assert r.depth_ratio == 1.0
    assert r.fees > 0


def test_insufficient_depth_is_fantasy():
    r = assess_fill(requested_size=1000, ask=0.50, ask_depth=10, bid=0.49)
    assert r.fantasy is True
    assert "insufficient_depth" in r.reason
    assert r.filled_size == 10  # only what the book had


def test_excessive_spread_is_fantasy():
    r = assess_fill(requested_size=10, ask=0.60, ask_depth=1000, bid=0.40,
                    model=FillModel(max_spread=0.10))
    assert r.fantasy is True
    assert "spread_too_wide" in r.reason


def test_excessive_slippage_is_fantasy():
    # deep size walks into a far worse level -> high slippage
    levels = [(0.50, 1), (0.80, 1000)]
    r = assess_fill(requested_size=100, ask=0.50, levels=levels, bid=0.49,
                    model=FillModel(max_slippage_frac=0.02, min_depth_ratio=0.0,
                                    min_fill_score=0.0))
    assert r.slippage_frac > 0.02
    assert r.fantasy is True
    assert "slippage_too_high" in r.reason


def test_score_monotonic_in_depth():
    m = FillModel()
    lo = fill_realism_score(0.2, 0.0, 0.0, m)
    hi = fill_realism_score(1.0, 0.0, 0.0, m)
    assert hi > lo
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0


def test_is_fantasy_helper_matches_assess():
    assert is_fantasy_fill(requested_size=1e9, ask=0.5, ask_depth=1) is True
    assert is_fantasy_fill(requested_size=1, ask=0.5, ask_depth=1000, bid=0.49) is False


def test_zero_size_is_fantasy():
    r = assess_fill(requested_size=0, ask=0.5, ask_depth=100)
    assert r.fantasy is True
    assert "non_positive_size" in r.reason


def test_fill_result_serializes():
    r = assess_fill(requested_size=10, ask=0.5, ask_depth=100, bid=0.49)
    d = r.to_dict()
    assert d["filled_size"] == 10 and "score" in d and "fantasy" in d
