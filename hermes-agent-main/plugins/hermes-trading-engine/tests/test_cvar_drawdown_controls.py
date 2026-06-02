"""CVaR + drawdown budget controls (TDD, deterministic, offline).

Quant scope: Risk Management & Portfolio Optimization + Strategy Optimization &
Robustness Testing. Expected shortfall (CVaR), max drawdown, the drawdown
budget, and the daily-loss circuit.
"""

from __future__ import annotations

from engine.training.portfolio import (
    PortfolioLimits,
    PortfolioPosition,
    PortfolioRiskManager,
    PortfolioState,
    cvar,
    max_drawdown,
)


def test_cvar_expected_shortfall_on_known_series():
    # 10 returns; worst 10% tail (1 obs) is -0.20 -> CVaR(0.90) = 0.20
    rets = [0.05, 0.02, -0.01, 0.03, -0.20, 0.01, 0.04, -0.02, 0.06, 0.00]
    assert cvar(rets, alpha=0.90) == 0.20


def test_cvar_zero_when_no_losses():
    assert cvar([0.01, 0.02, 0.03], alpha=0.95) == 0.0
    assert cvar([], alpha=0.95) == 0.0


def test_cvar_averages_tail():
    # worst 50% tail of [-0.4,-0.2,0.1,0.3] is [-0.4,-0.2] -> mean loss 0.30
    assert cvar([-0.4, -0.2, 0.1, 0.3], alpha=0.5) == 0.30


def test_max_drawdown_peak_to_trough():
    assert max_drawdown([100, 110, 105, 90, 95]) == 20.0
    assert max_drawdown([100, 101, 102]) == 0.0
    assert max_drawdown([]) == 0.0


def test_drawdown_budget_blocks_new_risk():
    lim = PortfolioLimits(max_drawdown_usd=50.0)
    m = PortfolioRiskManager(lim)
    ok, reason = m.check(notional=5.0, state=PortfolioState(), drawdown=60.0)
    assert not ok and reason == "drawdown_budget"


def test_daily_loss_cap_blocks_new_risk():
    lim = PortfolioLimits(max_daily_loss_usd=50.0)
    m = PortfolioRiskManager(lim)
    ok, reason = m.check(notional=5.0, state=PortfolioState(), day_pnl=-55.0)
    assert not ok and reason == "daily_loss_cap"


def test_portfolio_report_has_expected_shortfall_and_drawdown():
    st = PortfolioState()
    st.add(PortfolioPosition(notional=10.0, event_group="e1", category="a"))
    st.add(PortfolioPosition(notional=10.0, event_group="e2", category="b", bregman=True))
    rep = PortfolioRiskManager(PortfolioLimits()).portfolio_report(
        st, day_pnl=-5.0, returns=[-0.2, 0.1, 0.0, 0.05],
        equity_curve=[100, 105, 90], exploration_used=4.0,
        worst_case_leg_failure=2.5, feedback_events=8)
    for key in ("gross_exposure", "net_exposure", "event_exposure", "strategy_exposure",
                "bregman_exposure", "chainlink_linked_exposure", "expected_shortfall",
                "max_drawdown", "worst_case_leg_failure", "concentration",
                "aggressive_exploration_budget_used", "feedback_generated_per_risk_unit"):
        assert key in rep
    assert rep["gross_exposure"] == 20.0
    assert rep["bregman_exposure"] == 10.0
    assert rep["expected_shortfall"] > 0.0
    assert rep["max_drawdown"] == 15.0
    assert rep["feedback_generated_per_risk_unit"] == 0.4    # 8 / 20
