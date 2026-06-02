"""Bad-market graylist / blacklist memory + live-readiness block.

Quant scope — *Live Trading & Monitoring* + *Compliance/Security/Operational
Excellence*: proves markets with repeated negative after-cost expectancy,
ambiguous settlement, poor fill quality, excessive spread drag, or bad labels are
graylisted then blacklisted; aggressive paper may explore graylisted markets only
with a tiny labeled size; and live-readiness BLOCKS them. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.profitability_governor import (
    MarketQualityMemory, ProfitabilityGovernor, STATE_BLACKLIST, STATE_CLEAN,
    STATE_GRAYLIST)


def test_repeated_negative_after_cost_graylists_then_blacklists():
    mem = MarketQualityMemory(graylist_threshold=3, blacklist_threshold=6)
    assert mem.state("m1") == STATE_CLEAN
    for _ in range(3):
        mem.record("m1", after_cost_expectancy=-0.01)
    assert mem.state("m1") == STATE_GRAYLIST
    for _ in range(3):
        mem.record("m1", after_cost_expectancy=-0.02)
    assert mem.state("m1") == STATE_BLACKLIST
    assert mem.is_blacklisted("m1")


@pytest.mark.parametrize("kw,reason", [
    ({"ambiguous": True}, "ambiguous_settlement"),
    ({"fill_quality": 0.1}, "poor_fill_quality"),
    ({"spread_drag": 0.05}, "excessive_spread_drag"),
    ({"bad_label": True}, "bad_labels"),
])
def test_each_bad_condition_adds_a_strike(kw, reason):
    mem = MarketQualityMemory(graylist_threshold=1)
    mem.record("m2", after_cost_expectancy=0.01, **kw)
    assert mem.state("m2") == STATE_GRAYLIST
    assert reason in mem.reasons("m2")


def test_clean_market_stays_clean():
    mem = MarketQualityMemory(graylist_threshold=3)
    for _ in range(5):
        mem.record("m3", after_cost_expectancy=0.02, fill_quality=1.0)
    assert mem.state("m3") == STATE_CLEAN


def test_aggressive_explores_graylist_tiny_but_live_blocks():
    gov = ProfitabilityGovernor(memory=MarketQualityMemory(graylist_threshold=2))
    # drive the market onto the graylist
    for _ in range(2):
        gov.memory.record("g1", after_cost_expectancy=-0.01)
    costs = dict(fee=0.001, spread=0.004, slippage=0.0025, ambiguity=0.0,
                 stale=0.0, evidence=0.0, calibration=0.0, liquidity=0.0)
    aggressive = gov.evaluate(market_id="g1", strategy="directional", gross_edge=0.05,
                              cost_components=costs, liquidity_usd=50000.0, spread=0.01,
                              market_type="binary", time_to_resolution_s=7 * 86400.0,
                              aggressive=True)
    conservative = gov.evaluate(market_id="g1", strategy="directional", gross_edge=0.05,
                                cost_components=costs, liquidity_usd=50000.0, spread=0.01,
                                market_type="binary", time_to_resolution_s=7 * 86400.0,
                                aggressive=False)
    # aggressive paper may explore with a tiny labeled size ...
    assert aggressive.timing == "tiny_exploration"
    assert aggressive.exploration_label == "graylisted_market"
    # ... but the market is NEVER live-ready while graylisted
    assert aggressive.live_ready is False
    assert conservative.timing == "skip"
    assert conservative.live_ready is False


def test_negative_after_cost_market_is_never_live_ready():
    gov = ProfitabilityGovernor()
    costs = dict(fee=0.001, spread=0.03, slippage=0.02, ambiguity=0.01,
                 stale=0.0, evidence=0.0, calibration=0.0, liquidity=0.0)
    v = gov.evaluate(market_id="neg", strategy="directional", gross_edge=0.02,
                     cost_components=costs, liquidity_usd=50000.0, spread=0.04,
                     market_type="binary", time_to_resolution_s=7 * 86400.0)
    assert v.after_cost["net_edge"] <= 0.0
    assert v.live_ready is False
