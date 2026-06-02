"""Flagship Bregman arbitrage wired into the PAPER trainer (deterministic).

Quant scope: Bregman arbitrage priority + Risk Management + Live Monitoring.
Verifies the trainer scans + certifies Bregman opportunities, opens a
fully-hedged set THROUGH the RiskEngine + paper broker (never bypassing risk),
prioritizes them, and reports them — all PAPER ONLY. Also confirms an incomplete
(non-exhaustive) group is never opened.
"""

from __future__ import annotations

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0


def _event_records(asks, *, group="elect", complete=True):
    recs = []
    for i, ask in enumerate(asks):
        raw = market(i, bid=round(ask - 0.02, 4), ask=ask, liq=20_000, depth=2000,
                     category="crypto", group=group, now=_NOW)
        if complete:
            raw["negRiskComplete"] = True
        recs.append(um.MarketRecord.from_raw(raw, now=_NOW))
    return recs


def _trainer(tmp_path, monkeypatch):
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=8),
                                  data_dir=tmp_path)


def test_trainer_opens_certified_bregman_hedge_through_risk(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    recs = _event_records([0.28, 0.30, 0.30])      # YES asks sum 0.88 -> arb
    risk_before = t.risk.approvals
    opened = t._run_bregman(recs, _NOW)
    assert opened == 1
    assert t.bregman_sets_opened == 1
    # every leg was approved by the deterministic RiskEngine (not bypassed)
    assert t.risk.approvals - risk_before == len(recs)
    bregman_positions = [p for p in t.positions if p.strategy == "bregman"]
    assert len(bregman_positions) == len(recs)
    assert all(p.group_key.startswith("event:") for p in bregman_positions)


def test_trainer_skips_non_exhaustive_group(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    recs = _event_records([0.28, 0.30, 0.30], complete=False)   # not certified-exhaustive
    opened = t._run_bregman(recs, _NOW)
    assert opened == 0
    assert t.bregman_sets_opened == 0


def test_trainer_skips_overround_event(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    recs = _event_records([0.40, 0.40, 0.40])      # sum 1.20 -> no edge
    opened = t._run_bregman(recs, _NOW)
    assert opened == 0
    assert t.bregman_opportunity_count == 0


def test_trainer_bregman_summary_and_baseline_report(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t._run_bregman(_event_records([0.28, 0.30, 0.30]), _NOW)
    summ = t.bregman_summary()
    assert summ["enabled"] is True and summ["execution_enabled"] is True
    assert summ["opportunity_count"] >= 1
    assert summ["sets_opened"] == 1
    assert "last_scan_metrics" in summ
    rep = t.baseline_report()
    assert rep["bregman_status"] == "active" and rep["bregman_present"] is True
    assert rep["bregman"]["sets_opened"] == 1


def test_trainer_bregman_disabled_opens_nothing(tmp_path, monkeypatch):
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", bregman_execution_enabled=False),
        data_dir=tmp_path)
    opened = t._run_bregman(_event_records([0.28, 0.30, 0.30]), _NOW)
    assert opened == 0
    # still scanned + reported as an opportunity (flagship visibility)
    assert t.bregman_opportunity_count >= 1
