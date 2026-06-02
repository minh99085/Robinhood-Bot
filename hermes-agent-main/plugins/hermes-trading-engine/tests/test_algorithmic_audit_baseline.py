"""Deterministic algorithm-inventory audit baseline.

Proves the inventory honestly reports active/disabled/absent algorithm paths,
that legacy cross-exchange arbitrage stays permanently disabled, that the
Chainlink scanner is present (added as a feature layer), and that the flagship
Polymarket Bregman arbitrage strategy is implemented + active.
"""

from __future__ import annotations

import pytest

from engine.training.algorithm_inventory import algorithm_inventory
from engine.training import PolymarketPaperTrainer, TrainingConfig

from tests._pmtrain_helpers import clean_live_env


def test_inventory_lists_active_core_paths():
    inv = algorithm_inventory()
    for name in ("probability", "scan", "ranking", "edge", "sizing", "risk",
                 "fill", "replay", "learner", "feedback"):
        assert name in inv["components"], name
        assert inv["components"][name]["status"] == "active", name


def test_inventory_legacy_arb_permanently_disabled():
    inv = algorithm_inventory()
    arb = inv["components"]["legacy_cross_exchange_arbitrage"]
    assert arb["status"] == "disabled"
    assert inv["legacy_arb_disabled"] is True


def test_inventory_chainlink_and_bregman_present():
    inv = algorithm_inventory()
    assert inv["chainlink_present"] is True          # Chainlink scanner added
    assert inv["bregman_present"] is True            # flagship Bregman arbitrage active
    assert "bregman_arbitrage_not_implemented" not in inv["gaps"]
    assert inv["components"]["bregman_arbitrage"]["status"] == "active"


def test_inventory_is_deterministic():
    assert algorithm_inventory() == algorithm_inventory()


def test_trainer_baseline_report_structure(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    rep = t.baseline_report()
    assert rep["paper_only"] is True
    assert rep["bregman_present"] is True and rep["bregman_status"] == "active"
    assert rep["legacy_arb_disabled"] is True
    assert "bregman" in rep
    assert "institutional_metrics" in rep and "algorithm_inventory" in rep
    assert "sharpe" in rep["institutional_metrics"]
