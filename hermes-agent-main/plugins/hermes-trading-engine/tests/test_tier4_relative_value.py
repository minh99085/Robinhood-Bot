"""Tier-4 cross-market relative-value detector — advisory/telemetry-only, never trades."""

from __future__ import annotations

import time

import pytest

from engine.markets import universe_manager as um
from engine.training.relative_value import find_relative_value


def _mkt(mid, *, yes, negrisk=None, liq=2000.0):
    raw = {"id": mid, "question": f"q{mid}",
           "bestBid": round(yes - 0.01, 4), "bestAsk": round(yes + 0.01, 4),
           "liquidityNum": liq, "clobTokenIds": [f"{mid}a", f"{mid}b"]}
    if negrisk:
        raw["negRiskMarketID"] = negrisk
    return um.MarketRecord.from_raw(raw, now=time.time())


def test_detects_mutually_exclusive_overround():
    # 3 neg-risk siblings priced 0.5/0.4/0.4 -> YES sum 1.3 (>1) = collectively over-priced
    recs = [_mkt("m0", yes=0.5, negrisk="E1"), _mkt("m1", yes=0.4, negrisk="E1"),
            _mkt("m2", yes=0.4, negrisk="E1")]
    rep = find_relative_value(recs, min_mispricing=0.03)
    assert rep["rv_candidates_found"] == 1
    c = rep["top_candidates"][0]
    assert c["kind"] == "mutually_exclusive_overround"
    assert abs(c["yes_sum"] - 1.3) < 1e-6 and c["mispricing"] > 0
    assert c["advisory_only"] is True and rep["live_trading_enabled"] is False


def test_detects_underround_complete_set():
    recs = [_mkt("a", yes=0.3, negrisk="E2"), _mkt("b", yes=0.3, negrisk="E2")]
    rep = find_relative_value(recs, min_mispricing=0.03)
    assert rep["underround_count"] == 1
    assert rep["top_candidates"][0]["kind"] == "underround_complete_set"


def test_coherent_family_not_flagged():
    recs = [_mkt("a", yes=0.5, negrisk="E3"), _mkt("b", yes=0.5, negrisk="E3")]  # sum 1.0
    rep = find_relative_value(recs, min_mispricing=0.03)
    assert rep["rv_candidates_found"] == 0


def test_lone_market_ignored():
    rep = find_relative_value([_mkt("solo", yes=0.9)], min_mispricing=0.03)
    assert rep["families_examined"] == 0 and rep["rv_candidates_found"] == 0


def test_liquidity_filter():
    recs = [_mkt("a", yes=0.6, negrisk="E4", liq=10.0), _mkt("b", yes=0.6, negrisk="E4", liq=10.0)]
    rep = find_relative_value(recs, min_mispricing=0.03, min_family_liquidity_usd=1000.0)
    assert rep["rv_candidates_found"] == 0          # illiquid family skipped


def test_candidates_sorted_and_capped():
    recs = []
    for i in range(40):
        recs += [_mkt(f"e{i}a", yes=0.7, negrisk=f"E{i}"),
                 _mkt(f"e{i}b", yes=0.7, negrisk=f"E{i}")]   # sum 1.4 each
    rep = find_relative_value(recs, min_mispricing=0.03, max_candidates=10)
    assert len(rep["top_candidates"]) == 10
    scores = [c["score"] for c in rep["top_candidates"]]
    assert scores == sorted(scores, reverse=True)
