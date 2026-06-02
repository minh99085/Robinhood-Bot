"""Market-data freshness must block risk-gated proposals (Phase 2)."""

from __future__ import annotations

from engine.risk import MarketDataSnapshot, RiskCode, RiskContext, RiskEngine, RiskLimits
from engine.schemas import TradeProposal


def _pm_proposal() -> TradeProposal:
    return TradeProposal(strategy="polymarket", market="polymarket", symbol="mkt-1",
                         side="YES", notional=20.0, price=0.7, edge_after_costs=0.1)


def _ctx(md: MarketDataSnapshot) -> RiskContext:
    return RiskContext(equity=100_000.0, market_data=md)


def test_market_resolved_blocks_risk():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=True, resolved=True)))
    assert d.approved is False
    assert d.code == RiskCode.RESOLVED_MARKET  # "resolved_market"


def test_stale_orderbook_blocks_risk():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=True, stale=True)))
    assert d.approved is False
    assert d.code == RiskCode.STALE_MARKET_DATA  # "stale_market_data"


def test_missing_bbo_blocks_risk():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=False)))
    assert d.approved is False
    assert d.code == RiskCode.MISSING_BBO  # "missing_bbo"


def test_tick_size_dirty_blocks_risk():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=True, tick_size_dirty=True)))
    assert d.approved is False
    assert d.code == RiskCode.TICK_SIZE_CHANGED  # "tick_size_changed_requires_refresh"


def test_degraded_feed_blocks_risk():
    eng = RiskEngine(RiskLimits())
    for status in ("disconnected", "reconnecting", "degraded", "connecting"):
        d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
            required=True, status=status, bbo_present=True)))
        assert d.approved is False
        assert d.code == RiskCode.MARKET_DATA_DEGRADED  # "market_data_degraded"


def test_excessive_live_spread_blocks_risk():
    eng = RiskEngine(RiskLimits(max_spread=0.05))
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=True, spread=0.20)))
    assert d.approved is False
    assert d.code == RiskCode.EXCESSIVE_SPREAD  # "excessive_spread"


def test_fresh_market_data_is_approved():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=True, status="connected", bbo_present=True, stale=False,
        resolved=False, tick_size_dirty=False, unreliable=False, spread=0.01)))
    assert d.approved is True


def test_market_data_checks_skipped_when_not_required():
    # required=False (CLOB off / untracked market) -> Phase 1 behavior intact.
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), _ctx(MarketDataSnapshot(
        required=False, status="disconnected", bbo_present=False, stale=True,
        resolved=True, tick_size_dirty=True)))
    assert d.approved is True


def test_no_market_data_context_preserves_phase1():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_pm_proposal(), RiskContext(equity=100_000.0, market_data=None))
    assert d.approved is True
