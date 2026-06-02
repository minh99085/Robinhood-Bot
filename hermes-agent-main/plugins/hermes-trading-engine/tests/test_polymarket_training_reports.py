"""Report + storage tests for the Polymarket training engine v2."""

from __future__ import annotations

import pytest

from engine.training import PolymarketPaperTrainer, TrainingConfig, TrainingStore
from engine.training.reports import write_reports, RECOMMENDATIONS

from tests._pmtrain_helpers import clean_live_env, catalog, FakeResearch


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


def _run(env):
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=5,
                                              max_hold_ticks=3),
                               data_dir=env, signal_model=FakeResearch(fair=0.80))
    for _ in range(4):
        t.run_tick(catalog(10, bid=0.28, ask=0.30))
    t.finalize()
    return t


def test_training_report_created(env, tmp_path):
    out = write_reports(_run(env), out_root=tmp_path / "rep")
    expected = {"summary.json", "report.md", "candidates.csv", "edge_diagnostics.csv",
                "orders.csv", "fills.csv", "learning.csv", "bucket_stats.csv",
                "baselines.csv", "no_trade_reasons.csv", "calibration.csv"}
    assert expected.issubset(set(out["files"]))
    assert out["recommendation"] in RECOMMENDATIONS


def test_training_report_contains_safety_statement(env, tmp_path):
    out = write_reports(_run(env), out_root=tmp_path / "rep")
    md = (tmp_path / "rep" / out["run_id"] / "report.md").read_text()
    assert "PAPER ONLY" in md
    assert "arbitrage disabled" in md.lower()


def test_edge_bucket_pnl_report_created(env, tmp_path):
    out = write_reports(_run(env), out_root=tmp_path / "rep")
    assert "bucket_stats.csv" in out["files"]
    text = (tmp_path / "rep" / out["run_id"] / "bucket_stats.csv").read_text()
    assert "edge" in text or "calibration" in text


def test_calibration_bucket_report_created(env, tmp_path):
    out = write_reports(_run(env), out_root=tmp_path / "rep")
    assert "calibration.csv" in out["files"]


def test_baselines_report_created(env, tmp_path):
    out = write_reports(_run(env), out_root=tmp_path / "rep")
    text = (tmp_path / "rep" / out["run_id"] / "baselines.csv").read_text()
    assert "naive_price_extreme" in text and "current_strategy" in text


def test_storage_migrations_idempotent(env):
    s1 = TrainingStore(env)
    tables1 = s1.tables()
    s1.migrate()  # running again must not error or change schema
    s1.migrate()
    s2 = TrainingStore(env)  # re-open same DB
    assert s2.tables() == tables1
    expected = {"polymarket_training_runs", "polymarket_scan_metrics",
                "polymarket_candidates", "polymarket_edge_diagnostics",
                "polymarket_learning_events", "polymarket_bucket_stats",
                "polymarket_baseline_results"}
    assert expected.issubset(set(tables1))


def test_storage_records_run_and_diagnostics(env):
    s = TrainingStore(env)
    s.record_run("run-x", "paper_train", "hash")
    s.record_diagnostics("run-x", {
        "diagnostics_id": "d1", "ts_ms": 1, "market_id": "m", "asset_id": "a",
        "outcome": "YES", "side": "BUY", "p_market": 0.5, "p_model": None,
        "p_research": 0.6, "p_raw": 0.55, "p_final": 0.52, "shrink_factor": 0.2,
        "executable_price": 0.5, "spread": 0.02, "depth": 100, "gross_edge": 0.02,
        "net_edge": 0.01, "uncertainty_band": 0.02, "decision": "no_trade",
        "no_trade_reason": "edge_too_low", "payload": {}})
    cur = s._conn.execute("SELECT COUNT(*) FROM polymarket_edge_diagnostics")
    assert cur.fetchone()[0] == 1
