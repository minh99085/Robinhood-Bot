"""Tests for walk-forward validation, ablations, and Bregman-improvement proof."""

from __future__ import annotations

from engine.backtest import combinatorial_purged_cv, metrics_from_returns
from engine.replay.robustness import bootstrap_ci, walk_forward_windows
from engine.validation_contract import (
    ABLATION_COMPONENTS,
    bregman_improvement,
    walk_forward_report,
)


def test_walk_forward_report_bundles_windows_ci_regimes():
    rets = [0.01, 0.02, -0.01, 0.03, 0.0, 0.02, -0.02, 0.04, 0.01, 0.02, 0.0, 0.03]
    obs = [{"regime": "trending_up"}, {"regime": "chop"}, {"regime": "trending_up"}]
    rep = walk_forward_report(rets, train=4, test=2, regime_observations=obs)
    assert rep["n_returns"] == len(rets)
    assert rep["windows"] >= 1
    assert rep["bootstrap_ci"]["lo"] <= rep["bootstrap_ci"]["point"] <= rep["bootstrap_ci"]["hi"]
    assert rep["regime_buckets"]["trending_up"] == 2


def test_walk_forward_no_lookahead_windows():
    for w in walk_forward_windows(100, train=40, test=20):
        assert w.test_start >= w.train_end


def test_purged_cv_available_for_validation():
    splits = combinatorial_purged_cv(60, k=6, test_groups=2, embargo=2)
    assert len(splits) == 15
    for s in splits:
        assert not (set(s["test_idx"]) & set(s["train_idx"]))


def test_metrics_from_returns_bundle():
    m = metrics_from_returns([0.02, -0.01, 0.03, 0.01, -0.005, 0.02])
    for k in ("sharpe", "sortino", "calmar", "max_drawdown"):
        assert k in m


# --- Bregman improvement proof ----------------------------------------------
def test_bregman_improves_after_cost_metrics():
    baseline = {"sharpe": 0.5, "sortino": 0.6, "calmar": 0.4, "max_drawdown": 0.20,
                "calibration_adjusted_ev": 0.01}
    with_bregman = {"sharpe": 1.2, "sortino": 1.4, "calmar": 1.1, "max_drawdown": 0.12,
                    "calibration_adjusted_ev": 0.05}
    imp = bregman_improvement(baseline, with_bregman)
    assert imp["improves"] is True
    assert imp["per_metric"]["sharpe"]["better"] is True
    assert imp["per_metric"]["max_drawdown"]["better"] is True   # lower DD is better


def test_bregman_no_improvement_when_regresses():
    baseline = {"sharpe": 1.0, "sortino": 1.0, "calmar": 1.0, "max_drawdown": 0.10,
                "calibration_adjusted_ev": 0.05}
    worse = {"sharpe": 1.5, "sortino": 1.0, "calmar": 1.0, "max_drawdown": 0.25,
             "calibration_adjusted_ev": 0.05}   # drawdown got worse
    imp = bregman_improvement(baseline, worse)
    assert imp["improves"] is False
    assert imp["any_worse"] is True


def test_ablation_components_cover_required_set():
    assert set(ABLATION_COMPONENTS) == {
        "bregman", "chainlink_fast_btc", "calibration", "news_grok",
        "fill_realism", "risk_throttles"}


def test_bootstrap_ci_is_seeded_deterministic():
    rets = [0.01, 0.02, -0.01, 0.03, 0.0]
    assert bootstrap_ci(rets, seed=11) == bootstrap_ci(rets, seed=11)
