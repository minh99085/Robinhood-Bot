"""Baseline comparison tests (do_nothing / market_midpoint / naive_price_extreme
/ current_strategy)."""

from __future__ import annotations

import pytest

from engine.training import PolymarketPaperTrainer, TrainingConfig, BaselineComparator

from tests._pmtrain_helpers import clean_live_env, catalog, FakeResearch


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


def test_baseline_do_nothing_exists():
    names = {r["baseline_name"] for r in BaselineComparator().results()}
    assert "do_nothing" in names


def test_baseline_market_midpoint_exists():
    names = {r["baseline_name"] for r in BaselineComparator().results()}
    assert "market_midpoint" in names


def test_baseline_naive_price_extreme_exists():
    names = {r["baseline_name"] for r in BaselineComparator().results()}
    assert "naive_price_extreme" in names


def test_strategy_compared_against_baselines(env):
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=5,
                                              max_hold_ticks=3),
                               data_dir=env, signal_model=FakeResearch(fair=0.80))
    for _ in range(4):
        t.run_tick(catalog(10, bid=0.28, ask=0.30))
    t.finalize()
    res = {r["baseline_name"]: r for r in t.status()["baselines"]}
    assert set(res) == {"do_nothing", "market_midpoint", "naive_price_extreme",
                        "current_strategy"}
    # the OLD naive price-extreme rule trades far more often than the edge model
    assert res["naive_price_extreme"]["trade_count"] > res["current_strategy"]["trade_count"]
    assert res["do_nothing"]["trade_count"] == 0


def test_naive_extreme_trades_on_price_not_edge():
    b = BaselineComparator()
    # price extreme (0.70) -> naive trades; mild edge model would not just on price
    b.observe(yes_price=0.70, executable_price=0.71, p_market=0.705,
              min_net_edge=0.03, traded=False)
    res = {r["baseline_name"]: r for r in b.results()}
    assert res["naive_price_extreme"]["trade_count"] == 1
    assert res["current_strategy"]["trade_count"] == 0
