"""Portfolio risk constraints (TDD, deterministic, offline).

Quant scope: Risk Management & Portfolio Optimization. Additive event /
category / Bregman-bundle / total exposure caps, event-level netting, correlated
group caps, diversity, and concentration — never relaxes the mandatory gates.
"""

from __future__ import annotations

from engine.training.portfolio import (
    PortfolioLimits,
    PortfolioPosition,
    PortfolioRiskManager,
    PortfolioState,
)


def _state(*positions) -> PortfolioState:
    s = PortfolioState()
    for p in positions:
        s.add(p)
    return s


def _pos(notional, *, event="e1", category="politics", strategy="directional",
         side="BUY", bregman=False, chainlink=False) -> PortfolioPosition:
    return PortfolioPosition(strategy=strategy, category=category, event_group=event,
                             notional=notional, side=side, bregman=bregman,
                             chainlink_linked=chainlink)


_LIM = PortfolioLimits(max_total_exposure_usd=100.0, max_event_exposure_usd=20.0,
                       max_category_exposure_usd=40.0, max_bregman_bundle_exposure_usd=30.0,
                       max_daily_loss_usd=50.0, max_drawdown_usd=50.0,
                       exploration_budget_usd=20.0, diversity_target=3)


def test_event_exposure_cap_blocks():
    m = PortfolioRiskManager(_LIM)
    st = _state(_pos(18.0, event="e1"))
    ok, reason = m.check(notional=5.0, state=st, event_group="e1", category="politics")
    assert not ok and reason == "event_exposure_cap"


def test_category_exposure_cap_blocks():
    m = PortfolioRiskManager(_LIM)
    st = _state(_pos(18.0, event="e1", category="sports"),
                _pos(18.0, event="e2", category="sports"))
    ok, reason = m.check(notional=10.0, state=st, event_group="e3", category="sports")
    assert not ok and reason == "category_exposure_cap"


def test_total_exposure_cap_blocks():
    m = PortfolioRiskManager(_LIM)
    st = _state(*[_pos(19.0, event=f"e{i}", category=f"c{i}") for i in range(5)])  # 95
    ok, reason = m.check(notional=10.0, state=st, event_group="e9", category="c9")
    assert not ok and reason == "portfolio_total_exposure_cap"


def test_bregman_bundle_cap_blocks():
    m = PortfolioRiskManager(_LIM)
    st = _state(_pos(28.0, event="b1", category="crypto", strategy="bregman_arbitrage",
                     bregman=True))
    ok, reason = m.check(notional=5.0, state=st, event_group="b2", category="crypto",
                         strategy="bregman_arbitrage", bregman=True)
    assert not ok and reason == "bregman_bundle_exposure_cap"


def test_event_level_netting_offsets_opposing_sides():
    st = _state(_pos(10.0, event="e1", side="BUY"), _pos(4.0, event="e1", side="SELL"))
    assert st.event_exposure("e1") == 14.0          # gross
    assert st.net_by_event()["e1"] == 6.0           # netted


def test_diversity_and_concentration():
    st = _state(_pos(10.0, event="e1", category="a"), _pos(10.0, event="e2", category="b"),
                _pos(10.0, event="e3", category="c"))
    assert st.diversity() == 3
    # equal exposure across 3 events -> HHI = 3*(1/3)^2 = 0.333
    assert abs(st.concentration() - 1 / 3) < 1e-6


def test_clean_proposal_passes():
    m = PortfolioRiskManager(_LIM)
    ok, reason = m.check(notional=5.0, state=_state(_pos(5.0, event="e1")),
                         event_group="e2", category="politics")
    assert ok and reason == "ok"
