"""Baseline report wiring: trainer.baseline_report() + the script --baseline-report
flags (deterministic, offline)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.training import PolymarketPaperTrainer, TrainingConfig
from tests._pmtrain_helpers import clean_live_env, catalog, FakeResearch

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


def test_baseline_report_after_paper_run(env):
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=5,
                                              max_hold_ticks=2),
                               data_dir=env, signal_model=FakeResearch(fair=0.80))
    for _ in range(3):
        t.run_tick(catalog(10, bid=0.28, ask=0.30))
    t.finalize()
    rep = t.baseline_report()
    inst = rep["institutional_metrics"]
    assert inst["trade_count"] > 0
    assert inst["decision_count"] > 0
    assert "strategy_attribution" in inst
    assert rep["chainlink_present"] is True
    assert rep["bregman_status"] == "active"


def _run_script(rel, *args):
    import tempfile
    e = dict(os.environ)
    e["PYTHONPATH"] = str(PLUGIN)
    e["HTE_DATA_DIR"] = tempfile.mkdtemp()
    return subprocess.run([sys.executable, str(PLUGIN / rel), *args],
                          cwd=str(PLUGIN), env=e, capture_output=True, text=True, timeout=120)


def test_training_report_script_baseline_flag():
    r = _run_script("scripts/polymarket_training_report.py", "--baseline-report")
    assert r.returncode == 0
    assert "Algorithm Inventory" in r.stdout
    assert "flagship Polymarket Bregman arbitrage" in r.stdout


def test_run_replay_script_baseline_flag():
    r = _run_script("scripts/run_replay.py", "--baseline-report")
    assert r.returncode == 0
    assert "algorithm_inventory" in r.stdout
    assert "bregman_arbitrage_present" in r.stdout
