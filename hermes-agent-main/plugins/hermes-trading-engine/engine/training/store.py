"""TrainingStore — idempotent SQLite persistence for Polymarket paper training.

Owns its own DB file (``polymarket_training.sqlite3``) in the data dir so it
never touches or wipes existing engine tables. All migrations are
``CREATE TABLE IF NOT EXISTS`` (idempotent). No secrets are stored. PAPER ONLY.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

_SCHEMA = {
    "polymarket_training_runs": """
        CREATE TABLE IF NOT EXISTS polymarket_training_runs (
            run_id TEXT PRIMARY KEY, started_ts_ms INTEGER, stopped_ts_ms INTEGER,
            mode TEXT, config_hash TEXT, status TEXT, notes TEXT)""",
    "polymarket_scan_metrics": """
        CREATE TABLE IF NOT EXISTS polymarket_scan_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts_ms INTEGER,
            markets_fetched INTEGER, markets_scanned INTEGER, markets_ranked INTEGER,
            tier_a_count INTEGER, tier_b_count INTEGER, scan_ms REAL,
            candidates_per_second REAL, rejected_by_reason_json TEXT)""",
    "polymarket_candidates": """
        CREATE TABLE IF NOT EXISTS polymarket_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts_ms INTEGER,
            market_id TEXT, asset_id TEXT, question TEXT, category TEXT, status TEXT,
            quality_score REAL, rank INTEGER, tier TEXT, rejection_reason TEXT,
            features_json TEXT)""",
    "polymarket_edge_diagnostics": """
        CREATE TABLE IF NOT EXISTS polymarket_edge_diagnostics (
            diagnostics_id TEXT PRIMARY KEY, run_id TEXT, ts_ms INTEGER,
            market_id TEXT, asset_id TEXT, outcome TEXT, side TEXT, p_market REAL,
            p_model REAL, p_research REAL, p_raw REAL, p_final REAL, shrink_factor REAL,
            executable_price REAL, spread REAL, depth REAL, gross_edge REAL,
            net_edge REAL, uncertainty_band REAL, decision TEXT, no_trade_reason TEXT,
            payload_json TEXT)""",
    "polymarket_learning_events": """
        CREATE TABLE IF NOT EXISTS polymarket_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts_ms INTEGER,
            event_type TEXT, market_id TEXT, asset_id TEXT, diagnostics_id TEXT,
            order_id TEXT, fill_id TEXT, payload_json TEXT)""",
    "polymarket_bucket_stats": """
        CREATE TABLE IF NOT EXISTS polymarket_bucket_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts_ms INTEGER,
            bucket_type TEXT, bucket_name TEXT, sample_count INTEGER, trade_count INTEGER,
            win_rate REAL, pnl REAL, brier REAL, log_loss REAL, ece REAL,
            avg_markout REAL, reliability_score REAL, payload_json TEXT)""",
    "polymarket_baseline_results": """
        CREATE TABLE IF NOT EXISTS polymarket_baseline_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts_ms INTEGER,
            baseline_name TEXT, trade_count INTEGER, pnl REAL, drawdown REAL,
            brier REAL, log_loss REAL, ece REAL, payload_json TEXT)""",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


class TrainingStore:
    def __init__(self, data_dir, filename: str = "polymarket_training.sqlite3"):
        self.path = Path(data_dir) / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        cur = self._conn.cursor()
        for ddl in _SCHEMA.values():
            cur.execute(ddl)
        self._conn.commit()

    def tables(self) -> list:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'polymarket_%'")
        return sorted(r[0] for r in cur.fetchall())

    # -- inserts -------------------------------------------------------------
    def record_run(self, run_id, mode, config_hash, status="running", notes="") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO polymarket_training_runs "
            "(run_id, started_ts_ms, stopped_ts_ms, mode, config_hash, status, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, _now_ms(), None, mode, config_hash, status, notes))
        self._conn.commit()

    def stop_run(self, run_id, status="stopped") -> None:
        self._conn.execute(
            "UPDATE polymarket_training_runs SET stopped_ts_ms=?, status=? WHERE run_id=?",
            (_now_ms(), status, run_id))
        self._conn.commit()

    def record_scan(self, run_id, m: dict) -> None:
        self._conn.execute(
            "INSERT INTO polymarket_scan_metrics (run_id, ts_ms, markets_fetched, "
            "markets_scanned, markets_ranked, tier_a_count, tier_b_count, scan_ms, "
            "candidates_per_second, rejected_by_reason_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, _now_ms(), m.get("markets_fetched"), m.get("markets_scanned"),
             m.get("markets_ranked"), m.get("tier_a_count"), m.get("tier_b_count"),
             m.get("scan_ms"), m.get("candidates_per_second"),
             json.dumps(m.get("rejected_by_reason", {}))))
        self._conn.commit()

    def record_diagnostics(self, run_id, d: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO polymarket_edge_diagnostics (diagnostics_id, run_id, "
            "ts_ms, market_id, asset_id, outcome, side, p_market, p_model, p_research, "
            "p_raw, p_final, shrink_factor, executable_price, spread, depth, gross_edge, "
            "net_edge, uncertainty_band, decision, no_trade_reason, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["diagnostics_id"], run_id, d["ts_ms"], d["market_id"], d["asset_id"],
             d["outcome"], d["side"], d["p_market"], d["p_model"], d["p_research"],
             d["p_raw"], d["p_final"], d["shrink_factor"], d["executable_price"],
             d["spread"], d["depth"], d["gross_edge"], d["net_edge"],
             d["uncertainty_band"], d["decision"], d["no_trade_reason"],
             json.dumps(d.get("payload", {}))))
        self._conn.commit()

    def record_learning_event(self, run_id, event_type, **kw) -> None:
        self._conn.execute(
            "INSERT INTO polymarket_learning_events (run_id, ts_ms, event_type, "
            "market_id, asset_id, diagnostics_id, order_id, fill_id, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, _now_ms(), event_type, kw.get("market_id"), kw.get("asset_id"),
             kw.get("diagnostics_id"), kw.get("order_id"), kw.get("fill_id"),
             json.dumps(kw.get("payload", {}))))
        self._conn.commit()

    def record_baseline(self, run_id, r: dict) -> None:
        self._conn.execute(
            "INSERT INTO polymarket_baseline_results (run_id, ts_ms, baseline_name, "
            "trade_count, pnl, drawdown, payload_json) VALUES (?,?,?,?,?,?,?)",
            (run_id, _now_ms(), r["baseline_name"], r.get("trade_count"), r.get("pnl"),
             r.get("drawdown"), json.dumps(r)))
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
