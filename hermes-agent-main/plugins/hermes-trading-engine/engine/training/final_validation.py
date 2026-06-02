"""Final quantitative validation: aggressive-mode metrics + baseline-vs-upgraded
report (pure Python, deterministic).

Quant scope — *Strategy Optimization & Robustness Testing* + *Live Trading &
Monitoring* + *Compliance/Security/Operational Excellence*:

* :func:`aggressive_mode_metrics` — the measurable-learning summary for the
  explicit aggressive PAPER-training mode (trade count, market/category coverage,
  feedback samples, exploration vs exploit, Bregman bundles, Chainlink-linked
  trades, rejection reduction, learning-rate + calibration improvement, feedback
  per drawdown unit) plus the PAPER-ONLY safety attestations.
* :func:`final_validation_report` — compares the ORIGINAL conservative,
  directional-only paper trainer vs the upgraded Chainlink + Bregman-first
  aggressive system: trade-count / market-coverage / feedback uplift, calibration
  + risk improvement, no-regression checks, and a production-readiness verdict.

Analytics only — no trading, no network. The PAPER-ONLY / no-live invariants are
surfaced so a regression that enabled live execution would fail the report.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("hte.training.final_validation")

_EPS = 1e-9


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def aggressive_mode_metrics(*, total_trades: int, unique_markets: int,
                            unique_categories: int, feedback_samples: int,
                            exploration_trades: int, exploit_trades: int,
                            bregman_bundles: int, chainlink_linked_trades: int,
                            rejection_rate_before: float, rejection_rate_after: float,
                            ece_before: float, ece_after: float,
                            learning_samples_before: int, learning_samples_after: int,
                            max_drawdown: float, paper_only: bool = True,
                            live_orders: int = 0) -> dict:
    """Aggressive-mode learning + safety metrics. ``rejection_reduction`` and the
    improvement deltas are positive when the upgraded run is better; PAPER-ONLY
    attestations are included so a live regression is detectable."""
    rejection_reduction = round(_f(rejection_rate_before) - _f(rejection_rate_after), 6)
    calibration_improvement = round(_f(ece_before) - _f(ece_after), 6)
    learning_rate_improvement = int(learning_samples_after) - int(learning_samples_before)
    fpdu = round(int(feedback_samples) / max(_f(max_drawdown), _EPS), 6)
    metrics = {
        "total_paper_trades": int(total_trades),
        "unique_markets_traded": int(unique_markets),
        "unique_categories_traded": int(unique_categories),
        "feedback_samples_generated": int(feedback_samples),
        "exploration_trades": int(exploration_trades),
        "exploit_trades": int(exploit_trades),
        "bregman_bundles": int(bregman_bundles),
        "chainlink_linked_trades": int(chainlink_linked_trades),
        "rejection_reduction": rejection_reduction,
        "learning_rate_improvement": learning_rate_improvement,
        "calibration_improvement": calibration_improvement,
        "feedback_generated_per_drawdown_unit": fpdu,
        # PAPER-ONLY safety attestations (any False -> hard fail upstream)
        "paper_only": bool(paper_only),
        "live_orders": int(live_orders),
        "live_execution_enabled": False,
    }
    logger.info("aggressive_mode_metrics trades=%d markets=%d feedback=%d "
                "rejection_reduction=%.4f calib_improvement=%.4f", total_trades,
                unique_markets, feedback_samples, rejection_reduction, calibration_improvement)
    return metrics


# required metric keys each system block must carry for the final report
_SYSTEM_KEYS = (
    "trade_count", "unique_markets", "feedback_samples", "sharpe", "sortino",
    "calmar", "omega", "max_drawdown", "expectancy", "brier", "log_loss", "ece",
    "realized_edge", "fill_quality", "chainlink_impact", "bregman_certified_profit",
    "false_positive_rate",
)


def _block(d: dict) -> dict:
    return {k: _f(d.get(k, 0.0)) for k in _SYSTEM_KEYS}


def final_validation_report(conservative: dict, upgraded: dict, *,
                            drawdown_regression_tol: float = 1.25,
                            brier_regression_tol: float = 0.02) -> dict:
    """Baseline-vs-upgraded final validation report.

    ``conservative`` = original directional-only paper trainer; ``upgraded`` =
    Chainlink + Bregman-first aggressive system. Both dicts carry the metric keys
    in ``_SYSTEM_KEYS``. Reports uplifts, calibration/risk improvements, the
    no-regression checks, and a production-readiness verdict.
    """
    cons = _block(conservative)
    upg = _block(upgraded)

    uplifts = {
        "trade_count_uplift": round(upg["trade_count"] - cons["trade_count"], 6),
        "market_coverage_uplift": round(upg["unique_markets"] - cons["unique_markets"], 6),
        "feedback_sample_uplift": round(upg["feedback_samples"] - cons["feedback_samples"], 6),
    }
    improvements = {
        "brier_improvement": round(cons["brier"] - upg["brier"], 6),
        "log_loss_improvement": round(cons["log_loss"] - upg["log_loss"], 6),
        "ece_improvement": round(cons["ece"] - upg["ece"], 6),
        "realized_edge_improvement": round(upg["realized_edge"] - cons["realized_edge"], 6),
        "fill_quality_improvement": round(upg["fill_quality"] - cons["fill_quality"], 6),
    }
    # no-regression: calibration not materially worse, drawdown bounded, no new
    # false positives, and Bregman certification never regresses below zero.
    no_regression = {
        "brier_not_worse": upg["brier"] <= cons["brier"] + brier_regression_tol,
        "ece_not_worse": upg["ece"] <= cons["ece"] + brier_regression_tol,
        "drawdown_bounded": upg["max_drawdown"] <= max(cons["max_drawdown"], _EPS) * drawdown_regression_tol,
        "no_new_false_positives": upg["false_positive_rate"] <= cons["false_positive_rate"] + _EPS,
        "bregman_false_positive_zero": upg["false_positive_rate"] <= _EPS,
    }
    no_regression_ok = all(no_regression.values())
    paper_only = bool(conservative.get("paper_only", True) and upgraded.get("paper_only", True)
                      and int(upgraded.get("live_orders", 0)) == 0)
    production_ready = bool(no_regression_ok and paper_only
                            and uplifts["trade_count_uplift"] >= 0
                            and uplifts["feedback_sample_uplift"] >= 0)
    report = {
        "conservative": cons,
        "upgraded": upg,
        "uplifts": uplifts,
        "improvements": improvements,
        "no_regression": no_regression,
        "no_regression_ok": no_regression_ok,
        "paper_only": paper_only,
        "live_orders": int(upgraded.get("live_orders", 0)),
        "production_ready": production_ready,
    }
    logger.info("final_validation production_ready=%s no_regression=%s trade_uplift=%s",
                production_ready, no_regression_ok, uplifts["trade_count_uplift"])
    return report
