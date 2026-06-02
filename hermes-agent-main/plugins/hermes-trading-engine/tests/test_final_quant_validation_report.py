"""Final baseline-vs-upgraded quantitative validation report (TDD, deterministic).

Quant scope: Strategy Optimization & Robustness Testing + Compliance. Compares
the original conservative directional-only paper trainer vs the upgraded
Chainlink + Bregman-first aggressive system: uplifts, calibration/risk
improvement, no-regression checks, and a production-readiness verdict.
"""

from __future__ import annotations

from engine.training.final_validation import final_validation_report

_CONSERVATIVE = {
    "trade_count": 20, "unique_markets": 8, "feedback_samples": 18, "sharpe": 0.4,
    "sortino": 0.5, "calmar": 0.3, "omega": 1.1, "max_drawdown": 12.0, "expectancy": 0.01,
    "brier": 0.250, "log_loss": 0.73, "ece": 0.150, "realized_edge": 0.01,
    "fill_quality": 0.70, "chainlink_impact": 0.0, "bregman_certified_profit": 0.0,
    "false_positive_rate": 0.0, "paper_only": True, "live_orders": 0,
}
_UPGRADED = {
    "trade_count": 55, "unique_markets": 18, "feedback_samples": 60, "sharpe": 0.9,
    "sortino": 1.1, "calmar": 0.7, "omega": 1.4, "max_drawdown": 13.0, "expectancy": 0.02,
    "brier": 0.225, "log_loss": 0.64, "ece": 0.011, "realized_edge": 0.03,
    "fill_quality": 0.86, "chainlink_impact": 0.04, "bregman_certified_profit": 10.8,
    "false_positive_rate": 0.0, "paper_only": True, "live_orders": 0,
}


def test_report_reports_uplifts():
    rep = final_validation_report(_CONSERVATIVE, _UPGRADED)
    assert rep["uplifts"]["trade_count_uplift"] == 35
    assert rep["uplifts"]["market_coverage_uplift"] == 10
    assert rep["uplifts"]["feedback_sample_uplift"] == 42


def test_report_reports_calibration_and_edge_improvement():
    rep = final_validation_report(_CONSERVATIVE, _UPGRADED)
    assert rep["improvements"]["brier_improvement"] > 0
    assert rep["improvements"]["ece_improvement"] > 0
    assert rep["improvements"]["realized_edge_improvement"] > 0
    assert rep["improvements"]["fill_quality_improvement"] > 0


def test_report_passes_no_regression_and_is_production_ready():
    rep = final_validation_report(_CONSERVATIVE, _UPGRADED)
    assert rep["no_regression_ok"] is True
    assert rep["paper_only"] is True
    assert rep["production_ready"] is True


def test_report_carries_all_required_metrics():
    rep = final_validation_report(_CONSERVATIVE, _UPGRADED)
    for key in ("sharpe", "sortino", "calmar", "omega", "max_drawdown", "expectancy",
                "brier", "log_loss", "ece", "realized_edge", "fill_quality",
                "chainlink_impact", "bregman_certified_profit", "false_positive_rate"):
        assert key in rep["upgraded"] and key in rep["conservative"]


def test_report_flags_regression_when_upgraded_is_worse():
    bad = dict(_UPGRADED, max_drawdown=100.0, false_positive_rate=0.2, brier=0.40)
    rep = final_validation_report(_CONSERVATIVE, bad)
    assert rep["no_regression_ok"] is False
    assert rep["production_ready"] is False


def test_report_fails_production_ready_if_live_orders_present():
    leaked = dict(_UPGRADED, live_orders=1, paper_only=False)
    rep = final_validation_report(_CONSERVATIVE, leaked)
    assert rep["paper_only"] is False
    assert rep["production_ready"] is False
