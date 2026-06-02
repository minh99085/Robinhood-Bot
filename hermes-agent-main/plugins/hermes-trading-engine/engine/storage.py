"""SQLite persistence for the paper-trading engine.

Stores portfolio equity snapshots, pulse-round bets, position trades, and the
prediction/outcome history used for probability calibration. Single-file DB
under the mounted /data volume.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL,
                    market TEXT,
                    symbol TEXT,
                    side TEXT,
                    qty REAL,
                    price REAL,
                    stake REAL,
                    status TEXT,
                    pnl REAL,
                    rationale TEXT,
                    meta TEXT
                );
                CREATE TABLE IF NOT EXISTS equity (
                    ts REAL PRIMARY KEY,
                    equity REAL,
                    realized REAL,
                    unrealized REAL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL,
                    p_raw REAL,
                    outcome INTEGER
                );
                CREATE TABLE IF NOT EXISTS raw_market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    source TEXT,
                    event_type TEXT,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    payload_json TEXT,
                    inserted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    bids_json TEXT,
                    asks_json TEXT,
                    best_bid TEXT,
                    best_ask TEXT,
                    spread TEXT,
                    midpoint TEXT,
                    tick_size TEXT
                );
                CREATE TABLE IF NOT EXISTS orderbook_deltas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    side TEXT,
                    price TEXT,
                    size TEXT,
                    action TEXT,
                    best_bid TEXT,
                    best_ask TEXT
                );
                CREATE TABLE IF NOT EXISTS market_data_health (
                    source TEXT PRIMARY KEY,
                    status TEXT,
                    last_message_ts_ms INTEGER,
                    reconnect_count INTEGER,
                    parse_errors INTEGER,
                    subscribed_asset_count INTEGER,
                    stale_asset_count INTEGER,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    event_type TEXT,
                    payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_raw_market_events_ts ON raw_market_events(ts_ms);
                CREATE INDEX IF NOT EXISTS idx_market_events_ts ON market_events(ts_ms);
                CREATE INDEX IF NOT EXISTS idx_ob_snapshots_asset ON orderbook_snapshots(asset_id, ts_ms);
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT UNIQUE,
                    broker_order_id TEXT,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    outcome TEXT,
                    side TEXT,
                    order_type TEXT,
                    limit_price TEXT,
                    quantity TEXT,
                    notional TEXT,
                    time_in_force TEXT,
                    status TEXT,
                    source TEXT,
                    proposal_id TEXT,
                    venue_kind TEXT,
                    parent_client_order_id TEXT,
                    risk_decision_json TEXT,
                    reject_reason TEXT,
                    created_ts_ms INTEGER,
                    updated_ts_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fill_id TEXT UNIQUE,
                    client_order_id TEXT,
                    broker_order_id TEXT,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    side TEXT,
                    price TEXT,
                    quantity TEXT,
                    notional TEXT,
                    fee TEXT,
                    liquidity_flag TEXT,
                    ts_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT,
                    market_id TEXT,
                    asset_id TEXT,
                    outcome TEXT,
                    quantity TEXT,
                    avg_price TEXT,
                    realized_pnl TEXT,
                    unrealized_pnl TEXT,
                    fees_paid TEXT,
                    updated_ts_ms INTEGER,
                    UNIQUE(venue, market_id, asset_id, outcome)
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    client_order_id TEXT,
                    event_type TEXT,
                    payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS reconciliation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    severity TEXT,
                    event_type TEXT,
                    payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_fills_coid ON fills(client_order_id);
                CREATE INDEX IF NOT EXISTS idx_order_events_coid ON order_events(client_order_id);
                CREATE TABLE IF NOT EXISTS replay_runs (
                    replay_run_id TEXT PRIMARY KEY,
                    episode_id TEXT,
                    config_json TEXT,
                    config_hash TEXT,
                    seed INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    venue TEXT,
                    market_ids_json TEXT,
                    asset_ids_json TEXT,
                    start_ts_ms INTEGER,
                    end_ts_ms INTEGER,
                    event_count INTEGER DEFAULT 0,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_events_processed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, ts_ms INTEGER, sequence INTEGER,
                    source_event_id INTEGER, venue TEXT, market_id TEXT, asset_id TEXT,
                    event_type TEXT, payload_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, ts_ms INTEGER, proposal_id TEXT, policy_name TEXT,
                    venue TEXT, market_id TEXT, asset_id TEXT, side TEXT, outcome TEXT,
                    fair_probability TEXT, confidence TEXT, limit_price TEXT, quantity TEXT,
                    notional TEXT, edge_after_costs TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, ts_ms INTEGER, proposal_id TEXT, client_order_id TEXT,
                    approved INTEGER, reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, client_order_id TEXT, ts_ms INTEGER, venue TEXT,
                    market_id TEXT, asset_id TEXT, side TEXT, order_type TEXT, limit_price TEXT,
                    quantity TEXT, notional TEXT, status TEXT, reject_reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, fill_id TEXT, client_order_id TEXT, ts_ms INTEGER,
                    venue TEXT, market_id TEXT, asset_id TEXT, side TEXT, price TEXT, quantity TEXT,
                    notional TEXT, fee TEXT, liquidity_flag TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, ts_ms INTEGER, venue TEXT, market_id TEXT, asset_id TEXT,
                    outcome TEXT, quantity TEXT, avg_price TEXT, realized_pnl TEXT,
                    unrealized_pnl TEXT, fees_paid TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, ts_ms INTEGER, cash TEXT, equity TEXT, realized_pnl TEXT,
                    unrealized_pnl TEXT, fees_paid TEXT, drawdown TEXT, exposure TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_metrics (
                    replay_run_id TEXT, metric_name TEXT, metric_value TEXT, metric_json TEXT,
                    PRIMARY KEY (replay_run_id, metric_name)
                );
                CREATE TABLE IF NOT EXISTS replay_calibration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_run_id TEXT, market_id TEXT, asset_id TEXT, outcome TEXT,
                    predicted_probability TEXT, confidence TEXT, realized_outcome INTEGER,
                    bucket TEXT, brier TEXT, log_loss TEXT, ts_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS market_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT, market_id TEXT, asset_id TEXT, outcome TEXT,
                    resolved_ts_ms INTEGER, realized_outcome INTEGER, payout_price TEXT,
                    source TEXT, payload_json TEXT,
                    UNIQUE(venue, market_id, asset_id, outcome)
                );
                CREATE INDEX IF NOT EXISTS idx_replay_events_run ON replay_events_processed(replay_run_id);
                CREATE INDEX IF NOT EXISTS idx_replay_orders_run ON replay_orders(replay_run_id);
                CREATE INDEX IF NOT EXISTS idx_replay_fills_run ON replay_fills(replay_run_id);
                CREATE INDEX IF NOT EXISTS idx_replay_equity_run ON replay_equity(replay_run_id);
                """
            )
            self._conn.commit()
        self._init_research_schema()
        self._init_venue_schema()
        self._init_shadow_schema()
        self._init_guarded_live_schema()
        self._init_micro_live_schema()
        self._init_post_canary_schema()
        self._init_production_review_schema()

    def _init_venue_schema(self) -> None:
        """Phase 6: venue-neutral metadata/resolution/lifecycle + Kalshi books.
        Idempotent; never wipes data; no secrets stored."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS venue_markets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT, market_id TEXT, market_ticker TEXT, asset_id TEXT,
                    event_ticker TEXT, series_ticker TEXT, question TEXT, title TEXT,
                    yes_title TEXT, no_title TEXT, outcomes_json TEXT, category TEXT,
                    tags_json TEXT, status TEXT, open_ts_ms INTEGER, close_ts_ms INTEGER,
                    latest_expiration_ts_ms INTEGER, settlement_timer_seconds INTEGER,
                    can_close_early INTEGER, fractional_trading_enabled INTEGER,
                    volume TEXT, volume_24h TEXT, open_interest TEXT, last_price TEXT,
                    yes_bid TEXT, yes_ask TEXT, no_bid TEXT, no_ask TEXT,
                    price_level_structure TEXT, min_tick_size TEXT, fee_metadata_json TEXT,
                    settlement_sources_json TEXT, contract_url TEXT, contract_terms_url TEXT,
                    raw_payload_hash TEXT, updated_ts_ms INTEGER,
                    UNIQUE(venue, market_ticker, asset_id)
                );
                CREATE TABLE IF NOT EXISTS venue_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT, series_ticker TEXT, title TEXT, category TEXT, tags_json TEXT,
                    frequency TEXT, settlement_sources_json TEXT, contract_url TEXT,
                    contract_terms_url TEXT, fee_multiplier TEXT, additional_prohibitions_json TEXT,
                    raw_payload_hash TEXT, updated_ts_ms INTEGER,
                    UNIQUE(venue, series_ticker)
                );
                CREATE TABLE IF NOT EXISTS resolution_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT, market_id TEXT, market_ticker TEXT, asset_id TEXT,
                    event_ticker TEXT, series_ticker TEXT, question TEXT, outcome TEXT,
                    rules_primary TEXT, rules_secondary TEXT, settlement_sources_json TEXT,
                    contract_url TEXT, contract_terms_url TEXT, close_ts_ms INTEGER,
                    latest_expiration_ts_ms INTEGER, can_close_early INTEGER,
                    ambiguity_categories_json TEXT, ambiguity_score TEXT, parsed_ts_ms INTEGER,
                    raw_text_hash TEXT,
                    UNIQUE(venue, market_ticker, asset_id, outcome)
                );
                CREATE TABLE IF NOT EXISTS venue_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER, venue TEXT, market_id TEXT, market_ticker TEXT,
                    asset_id TEXT, event_ticker TEXT, event_type TEXT, status TEXT,
                    payload_json TEXT, raw_payload_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS kalshi_orderbook_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER, market_ticker TEXT, market_id TEXT, seq INTEGER,
                    yes_bids_json TEXT, no_bids_json TEXT, yes_best_bid TEXT, yes_best_ask TEXT,
                    no_best_bid TEXT, no_best_ask TEXT, normalized_json TEXT
                );
                CREATE TABLE IF NOT EXISTS kalshi_orderbook_deltas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER, market_ticker TEXT, market_id TEXT, seq INTEGER,
                    side TEXT, price TEXT, delta TEXT, resulting_size TEXT,
                    gap_detected INTEGER DEFAULT 0, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS venue_market_data_health (
                    venue TEXT PRIMARY KEY, status TEXT, last_message_ts_ms INTEGER,
                    reconnect_count INTEGER DEFAULT 0, parse_errors INTEGER DEFAULT 0,
                    subscribed_count INTEGER DEFAULT 0, stale_count INTEGER DEFAULT 0,
                    seq_gap_count INTEGER DEFAULT 0, resnapshot_count INTEGER DEFAULT 0,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_venue_markets_venue ON venue_markets(venue);
                CREATE INDEX IF NOT EXISTS idx_resolution_rules_venue ON resolution_rules(venue);
                CREATE INDEX IF NOT EXISTS idx_venue_lifecycle_ticker
                    ON venue_lifecycle_events(market_ticker);
                """
            )
            self._conn.commit()

    # --- venues (Phase 6) ---------------------------------------------- #
    def upsert_venue_market(self, record: dict) -> None:
        cols = ("venue", "market_id", "market_ticker", "asset_id", "event_ticker",
                "series_ticker", "question", "title", "yes_title", "no_title", "outcomes_json",
                "category", "tags_json", "status", "open_ts_ms", "close_ts_ms",
                "latest_expiration_ts_ms", "settlement_timer_seconds", "can_close_early",
                "fractional_trading_enabled", "volume", "volume_24h", "open_interest",
                "last_price", "yes_bid", "yes_ask", "no_bid", "no_ask", "price_level_structure",
                "min_tick_size", "fee_metadata_json", "settlement_sources_json", "contract_url",
                "contract_terms_url", "raw_payload_hash", "updated_ts_ms")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in
                            ("venue", "market_ticker", "asset_id"))
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO venue_markets({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(venue, market_ticker, asset_id) DO UPDATE SET {updates}",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def upsert_venue_series(self, record: dict) -> None:
        cols = ("venue", "series_ticker", "title", "category", "tags_json", "frequency",
                "settlement_sources_json", "contract_url", "contract_terms_url",
                "fee_multiplier", "additional_prohibitions_json", "raw_payload_hash",
                "updated_ts_ms")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("venue", "series_ticker"))
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO venue_series({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(venue, series_ticker) DO UPDATE SET {updates}",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def upsert_resolution_rules(self, record: dict) -> None:
        cols = ("venue", "market_id", "market_ticker", "asset_id", "event_ticker",
                "series_ticker", "question", "outcome", "rules_primary", "rules_secondary",
                "settlement_sources_json", "contract_url", "contract_terms_url", "close_ts_ms",
                "latest_expiration_ts_ms", "can_close_early", "ambiguity_categories_json",
                "ambiguity_score", "parsed_ts_ms", "raw_text_hash")
        rec = {**record}
        rec.setdefault("outcome", "")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in
                            ("venue", "market_ticker", "asset_id", "outcome"))
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO resolution_rules({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(venue, market_ticker, asset_id, outcome) DO UPDATE SET {updates}",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_venue_lifecycle_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("venue_lifecycle_events",
                     ("ts_ms", "venue", "market_id", "market_ticker", "asset_id",
                      "event_ticker", "event_type", "status", "payload_json", "raw_payload_hash"),
                     self._json_field(record, "payload_json"))

    def append_kalshi_snapshot(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("kalshi_orderbook_snapshots",
                     ("ts_ms", "market_ticker", "market_id", "seq", "yes_bids_json",
                      "no_bids_json", "yes_best_bid", "yes_best_ask", "no_best_bid",
                      "no_best_ask", "normalized_json"), record)

    def append_kalshi_delta(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("kalshi_orderbook_deltas",
                     ("ts_ms", "market_ticker", "market_id", "seq", "side", "price",
                      "delta", "resulting_size", "gap_detected", "payload_json"),
                     self._json_field(record, "payload_json"))

    def upsert_venue_market_data_health(self, record: dict) -> None:
        cols = ("venue", "status", "last_message_ts_ms", "reconnect_count", "parse_errors",
                "subscribed_count", "stale_count", "seq_gap_count", "resnapshot_count", "updated_at")
        record.setdefault("updated_at", self._now_iso())
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "venue")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO venue_market_data_health({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(venue) DO UPDATE SET {updates}",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_venue_markets(self, *, venue: str | None = None, market_id: str | None = None,
                          market_ticker: str | None = None, limit: int = 200) -> list[dict]:
        try:
            q = "SELECT * FROM venue_markets"
            clauses, params = [], []
            if venue:
                clauses.append("venue=?"); params.append(venue)
            if market_id:
                clauses.append("market_id=?"); params.append(market_id)
            if market_ticker:
                clauses.append("market_ticker=?"); params.append(market_ticker)
            if clauses:
                q += " WHERE " + " AND ".join(clauses)
            q += " ORDER BY updated_ts_ms DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_venue_series_rows(self, venue: str | None = None) -> list[dict]:
        try:
            q = "SELECT * FROM venue_series"
            params: list = []
            if venue:
                q += " WHERE venue=?"; params.append(venue)
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_resolution_rules(self, *, venue: str, market_ticker: str | None = None,
                             market_id: str | None = None, asset_id: str | None = None,
                             outcome: str | None = None) -> Optional[dict]:
        try:
            q = "SELECT * FROM resolution_rules WHERE venue=?"
            params: list = [venue]
            if market_ticker:
                q += " AND market_ticker=?"; params.append(market_ticker)
            if market_id:
                q += " AND market_id=?"; params.append(market_id)
            if asset_id:
                q += " AND asset_id=?"; params.append(asset_id)
            if outcome is not None:
                q += " AND outcome=?"; params.append(outcome)
            q += " LIMIT 1"
            with _LOCK:
                row = self._conn.execute(q, params).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_venue_lifecycle_events(self, *, venue: str | None = None,
                                   limit: int = 100) -> list[dict]:
        try:
            q = "SELECT * FROM venue_lifecycle_events"
            params: list = []
            if venue:
                q += " WHERE venue=?"; params.append(venue)
            q += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def _init_shadow_schema(self) -> None:
        """Phase 7: shadow-mode tables. Idempotent; isolated from operational
        paper/replay tables; never wipes data; no secrets stored."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_sessions (
                    shadow_session_id TEXT PRIMARY KEY, status TEXT, started_ts_ms INTEGER,
                    stopped_ts_ms INTEGER, config_hash TEXT, config_json TEXT, venues_json TEXT,
                    mode TEXT, notes TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT UNIQUE,
                    shadow_session_id TEXT, ts_ms INTEGER, venue TEXT, market_id TEXT,
                    market_ticker TEXT, asset_id TEXT, outcome TEXT, question TEXT, category TEXT,
                    close_ts_ms INTEGER, liquidity_score TEXT, spread TEXT, volume TEXT,
                    open_interest TEXT, ambiguity_score TEXT, metadata_complete INTEGER,
                    data_fresh INTEGER, selected INTEGER, rejection_reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE,
                    shadow_session_id TEXT, ts_ms INTEGER, cycle_id TEXT, venue TEXT,
                    market_id TEXT, market_ticker TEXT, asset_id TEXT, outcome TEXT,
                    p_market TEXT, p_research TEXT, p_ensemble TEXT, confidence TEXT,
                    ambiguity_score TEXT, evidence_score TEXT, best_bid TEXT, best_ask TEXT,
                    spread TEXT, midpoint TEXT, intended_side TEXT, intended_limit_price TEXT,
                    intended_notional TEXT, edge_after_costs TEXT, decision TEXT, reason TEXT,
                    proposal_id TEXT, risk_decision_id TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shadow_order_id TEXT UNIQUE,
                    shadow_session_id TEXT, decision_id TEXT, proposal_id TEXT, client_order_id TEXT,
                    venue TEXT, market_id TEXT, market_ticker TEXT, asset_id TEXT, outcome TEXT,
                    side TEXT, order_type TEXT, limit_price TEXT, quantity TEXT, notional TEXT,
                    status TEXT, reject_reason TEXT, created_ts_ms INTEGER, updated_ts_ms INTEGER,
                    payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shadow_fill_id TEXT UNIQUE,
                    shadow_session_id TEXT, shadow_order_id TEXT, client_order_id TEXT, venue TEXT,
                    market_id TEXT, asset_id TEXT, side TEXT, price TEXT, quantity TEXT,
                    notional TEXT, fee TEXT, liquidity_flag TEXT, ts_ms INTEGER, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shadow_session_id TEXT, ts_ms INTEGER,
                    venue TEXT, market_id TEXT, market_ticker TEXT, asset_id TEXT, outcome TEXT,
                    quantity TEXT, avg_price TEXT, realized_pnl TEXT, unrealized_pnl TEXT,
                    fees_paid TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shadow_session_id TEXT, ts_ms INTEGER,
                    cash TEXT, equity TEXT, realized_pnl TEXT, unrealized_pnl TEXT, fees_paid TEXT,
                    drawdown TEXT, exposure TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT UNIQUE,
                    shadow_session_id TEXT, decision_id TEXT, shadow_order_id TEXT, venue TEXT,
                    market_id TEXT, market_ticker TEXT, asset_id TEXT, outcome TEXT,
                    horizon_ms INTEGER, observed_ts_ms INTEGER, best_bid TEXT, best_ask TEXT,
                    spread TEXT, midpoint TEXT, last_trade_price TEXT, depth_near_touch TEXT,
                    resolved_outcome TEXT, markout TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_metrics (
                    shadow_session_id TEXT, metric_name TEXT, metric_value TEXT, metric_json TEXT,
                    PRIMARY KEY (shadow_session_id, metric_name)
                );
                CREATE TABLE IF NOT EXISTS readiness_reports (
                    report_id TEXT PRIMARY KEY, shadow_session_id TEXT, generated_ts_ms INTEGER,
                    overall_status TEXT, summary_json TEXT, report_path TEXT
                );
                CREATE TABLE IF NOT EXISTS readiness_gate_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT, shadow_session_id TEXT,
                    gate_name TEXT, status TEXT, score TEXT, threshold TEXT, observed_value TEXT,
                    reason TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT UNIQUE,
                    shadow_session_id TEXT, ts_ms INTEGER, severity TEXT, alert_type TEXT,
                    message TEXT, payload_json TEXT, acknowledged INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shadow_heartbeats (
                    shadow_session_id TEXT PRIMARY KEY, ts_ms INTEGER, status TEXT,
                    cycle_count INTEGER, last_cycle_ts_ms INTEGER, last_error TEXT,
                    venue_status_json TEXT, updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_decisions_sess ON shadow_decisions(shadow_session_id);
                CREATE INDEX IF NOT EXISTS idx_shadow_orders_sess ON shadow_orders(shadow_session_id);
                CREATE INDEX IF NOT EXISTS idx_shadow_obs_sess ON shadow_observations(shadow_session_id);
                """
            )
            self._conn.commit()

    # --- shadow (Phase 7) ---------------------------------------------- #
    _SHADOW_TABLES = {
        "shadow_candidates", "shadow_decisions", "shadow_orders", "shadow_fills",
        "shadow_positions", "shadow_equity", "shadow_observations", "shadow_alerts",
    }

    def upsert_shadow_session(self, record: dict) -> None:
        cols = ("shadow_session_id", "status", "started_ts_ms", "stopped_ts_ms", "config_hash",
                "config_json", "venues_json", "mode", "notes")
        rec = self._json_field(self._json_field(record, "config_json"), "venues_json")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "shadow_session_id")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO shadow_sessions({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(shadow_session_id) DO UPDATE SET {updates}",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def update_shadow_session(self, shadow_session_id: str, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            with _LOCK:
                self._conn.execute(f"UPDATE shadow_sessions SET {sets} WHERE shadow_session_id=?",
                                   [*fields.values(), shadow_session_id])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_shadow_candidate(self, record: dict) -> None:
        self._insert("shadow_candidates",
                     ("candidate_id", "shadow_session_id", "ts_ms", "venue", "market_id",
                      "market_ticker", "asset_id", "outcome", "question", "category", "close_ts_ms",
                      "liquidity_score", "spread", "volume", "open_interest", "ambiguity_score",
                      "metadata_complete", "data_fresh", "selected", "rejection_reason",
                      "payload_json"), self._json_field(record, "payload_json"))

    def add_shadow_decision(self, record: dict) -> None:
        self._insert("shadow_decisions",
                     ("decision_id", "shadow_session_id", "ts_ms", "cycle_id", "venue", "market_id",
                      "market_ticker", "asset_id", "outcome", "p_market", "p_research", "p_ensemble",
                      "confidence", "ambiguity_score", "evidence_score", "best_bid", "best_ask",
                      "spread", "midpoint", "intended_side", "intended_limit_price",
                      "intended_notional", "edge_after_costs", "decision", "reason", "proposal_id",
                      "risk_decision_id", "payload_json"), self._json_field(record, "payload_json"))

    def add_shadow_order(self, record: dict) -> None:
        self._insert("shadow_orders",
                     ("shadow_order_id", "shadow_session_id", "decision_id", "proposal_id",
                      "client_order_id", "venue", "market_id", "market_ticker", "asset_id",
                      "outcome", "side", "order_type", "limit_price", "quantity", "notional",
                      "status", "reject_reason", "created_ts_ms", "updated_ts_ms", "payload_json"),
                     self._json_field(record, "payload_json"))

    def update_shadow_order(self, shadow_order_id: str, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            with _LOCK:
                self._conn.execute(f"UPDATE shadow_orders SET {sets} WHERE shadow_order_id=?",
                                   [*fields.values(), shadow_order_id])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_shadow_fill(self, record: dict) -> None:
        self._insert("shadow_fills",
                     ("shadow_fill_id", "shadow_session_id", "shadow_order_id", "client_order_id",
                      "venue", "market_id", "asset_id", "side", "price", "quantity", "notional",
                      "fee", "liquidity_flag", "ts_ms", "payload_json"),
                     self._json_field(record, "payload_json"))

    def add_shadow_observation(self, record: dict) -> None:
        self._insert("shadow_observations",
                     ("observation_id", "shadow_session_id", "decision_id", "shadow_order_id",
                      "venue", "market_id", "market_ticker", "asset_id", "outcome", "horizon_ms",
                      "observed_ts_ms", "best_bid", "best_ask", "spread", "midpoint",
                      "last_trade_price", "depth_near_touch", "resolved_outcome", "markout",
                      "payload_json"), self._json_field(record, "payload_json"))

    def add_shadow_alert(self, record: dict) -> None:
        self._insert("shadow_alerts",
                     ("alert_id", "shadow_session_id", "ts_ms", "severity", "alert_type",
                      "message", "payload_json", "acknowledged"),
                     self._json_field(record, "payload_json"))

    def upsert_shadow_heartbeat(self, record: dict) -> None:
        cols = ("shadow_session_id", "ts_ms", "status", "cycle_count", "last_cycle_ts_ms",
                "last_error", "venue_status_json", "updated_at")
        record.setdefault("updated_at", self._now_iso())
        rec = self._json_field(record, "venue_status_json")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "shadow_session_id")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO shadow_heartbeats({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(shadow_session_id) DO UPDATE SET {updates}",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def set_shadow_metric(self, session_id: str, name: str, value, json_value=None) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO shadow_metrics(shadow_session_id,metric_name,metric_value,metric_json)"
                    " VALUES(?,?,?,?) ON CONFLICT(shadow_session_id,metric_name) DO UPDATE SET "
                    "metric_value=excluded.metric_value, metric_json=excluded.metric_json",
                    (session_id, name, None if value is None else str(value),
                     None if json_value is None else json.dumps(json_value, default=str)))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_readiness_report(self, record: dict) -> None:
        cols = ("report_id", "shadow_session_id", "generated_ts_ms", "overall_status",
                "summary_json", "report_path")
        rec = self._json_field(record, "summary_json")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO readiness_reports({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(report_id) DO UPDATE SET overall_status=excluded.overall_status, "
                    "summary_json=excluded.summary_json, report_path=excluded.report_path",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_readiness_gate_result(self, record: dict) -> None:
        self._insert("readiness_gate_results",
                     ("report_id", "shadow_session_id", "gate_name", "status", "score",
                      "threshold", "observed_value", "reason", "details_json"),
                     self._json_field(record, "details_json"))

    def get_shadow_rows(self, table: str, session_id: str, limit: int = 100000) -> list[dict]:
        if table not in self._SHADOW_TABLES:
            return []
        try:
            with _LOCK:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE shadow_session_id=? ORDER BY id ASC LIMIT ?",
                    (session_id, int(limit))).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_shadow_sessions(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM shadow_sessions ORDER BY started_ts_ms DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_shadow_session(self, shadow_session_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM shadow_sessions WHERE shadow_session_id=?",
                    (shadow_session_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_shadow_heartbeat(self, shadow_session_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM shadow_heartbeats WHERE shadow_session_id=?",
                    (shadow_session_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_readiness_reports(self, shadow_session_id: str | None = None,
                              limit: int = 50) -> list[dict]:
        try:
            q = "SELECT * FROM readiness_reports"
            params: list = []
            if shadow_session_id:
                q += " WHERE shadow_session_id=?"
                params.append(shadow_session_id)
            q += " ORDER BY generated_ts_ms DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_readiness_report(self, report_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM readiness_reports WHERE report_id=?", (report_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_shadow_alerts(self, shadow_session_id: str | None = None,
                          limit: int = 100) -> list[dict]:
        try:
            q = "SELECT * FROM shadow_alerts"
            params: list = []
            if shadow_session_id:
                q += " WHERE shadow_session_id=?"
                params.append(shadow_session_id)
            q += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def _init_guarded_live_schema(self) -> None:
        """Phase 8: guarded-live design/dry-run tables. Idempotent; never wipes
        data; stores token HASHES only and redacted secret hints only."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guarded_live_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, state TEXT,
                    previous_state TEXT, reason TEXT, config_hash TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS guarded_live_prechecks (
                    precheck_id TEXT PRIMARY KEY, ts_ms INTEGER, config_hash TEXT,
                    readiness_report_id TEXT, status TEXT, hard_fail_count INTEGER,
                    warning_count INTEGER, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS guarded_live_precheck_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, precheck_id TEXT, check_name TEXT,
                    status TEXT, reason TEXT, observed_value TEXT, threshold TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS manual_approvals (
                    approval_id TEXT PRIMARY KEY, approval_batch_id TEXT, ts_ms INTEGER,
                    approver_id TEXT, role TEXT, readiness_report_id TEXT, config_hash TEXT,
                    risk_limits_hash TEXT, approval_reason TEXT, confirmation_text TEXT,
                    expires_ts_ms INTEGER, revoked_ts_ms INTEGER, status TEXT
                );
                CREATE TABLE IF NOT EXISTS approval_batches (
                    approval_batch_id TEXT PRIMARY KEY, readiness_report_id TEXT, config_hash TEXT,
                    required_approvals INTEGER, valid_approvals INTEGER, status TEXT,
                    created_ts_ms INTEGER, expires_ts_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS arming_tokens (
                    arming_token_id TEXT PRIMARY KEY, token_hash TEXT, approval_batch_id TEXT,
                    readiness_report_id TEXT, config_hash TEXT, mode TEXT, created_ts_ms INTEGER,
                    expires_ts_ms INTEGER, used_ts_ms INTEGER, revoked_ts_ms INTEGER, status TEXT
                );
                CREATE TABLE IF NOT EXISTS dry_run_order_intents (
                    dry_run_intent_id TEXT PRIMARY KEY, ts_ms INTEGER, venue TEXT, market_id TEXT,
                    market_ticker TEXT, asset_id TEXT, outcome TEXT, side TEXT, order_type TEXT,
                    limit_price TEXT, quantity TEXT, notional TEXT, internal_order_request_json TEXT,
                    venue_payload_json TEXT, unsigned INTEGER, unsent INTEGER, signer_used INTEGER,
                    network_called INTEGER, risk_decision_id TEXT, safety_envelope_decision_id TEXT,
                    oms_order_id TEXT, status TEXT, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS safety_envelope_decisions (
                    decision_id TEXT PRIMARY KEY, ts_ms INTEGER, allowed INTEGER, mode TEXT,
                    state TEXT, reason TEXT, checks_json TEXT, config_hash TEXT, proposal_id TEXT,
                    client_order_id TEXT
                );
                CREATE TABLE IF NOT EXISTS conformance_runs (
                    conformance_run_id TEXT PRIMARY KEY, started_ts_ms INTEGER, finished_ts_ms INTEGER,
                    status TEXT, config_hash TEXT, test_count INTEGER, pass_count INTEGER,
                    fail_count INTEGER, warning_count INTEGER, report_path TEXT
                );
                CREATE TABLE IF NOT EXISTS conformance_checks (
                    check_id TEXT PRIMARY KEY, conformance_run_id TEXT, check_name TEXT, status TEXT,
                    reason TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS secret_policy_violations (
                    violation_id TEXT PRIMARY KEY, ts_ms INTEGER, severity TEXT, location TEXT,
                    violation_type TEXT, redacted_value TEXT, reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS guarded_live_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, event_type TEXT,
                    severity TEXT, actor TEXT, state TEXT, config_hash TEXT, payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_gl_state_ts ON guarded_live_state(ts_ms);
                CREATE INDEX IF NOT EXISTS idx_gl_appr_batch ON manual_approvals(approval_batch_id);
                """
            )
            self._conn.commit()

    # --- guarded live (Phase 8) ---------------------------------------- #
    _GUARDED_ROW_TABLES = {"dry_run_order_intents", "safety_envelope_decisions",
                           "secret_policy_violations", "conformance_checks",
                           "guarded_live_precheck_results"}

    def add_guarded_live_state(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("guarded_live_state",
                     ("ts_ms", "state", "previous_state", "reason", "config_hash", "payload_json"),
                     self._json_field(record, "payload_json"))

    def get_guarded_live_state(self, limit: int = 1) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM guarded_live_state ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_guarded_live_precheck(self, record: dict) -> None:
        self._insert("guarded_live_prechecks",
                     ("precheck_id", "ts_ms", "config_hash", "readiness_report_id", "status",
                      "hard_fail_count", "warning_count", "payload_json"),
                     self._json_field(record, "payload_json"))

    def add_guarded_live_precheck_result(self, record: dict) -> None:
        self._insert("guarded_live_precheck_results",
                     ("precheck_id", "check_name", "status", "reason", "observed_value",
                      "threshold", "details_json"), self._json_field(record, "details_json"))

    def get_guarded_live_prechecks(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM guarded_live_prechecks ORDER BY ts_ms DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_guarded_live_precheck(self, precheck_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM guarded_live_prechecks WHERE precheck_id=?",
                    (precheck_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def upsert_approval_batch(self, record: dict) -> None:
        cols = ("approval_batch_id", "readiness_report_id", "config_hash", "required_approvals",
                "valid_approvals", "status", "created_ts_ms", "expires_ts_ms")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "approval_batch_id")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO approval_batches({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(approval_batch_id) DO UPDATE SET {updates}",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_approval_batches(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM approval_batches ORDER BY created_ts_ms DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_approval_batch(self, approval_batch_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM approval_batches WHERE approval_batch_id=?",
                    (approval_batch_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def add_manual_approval(self, record: dict) -> None:
        self._insert("manual_approvals",
                     ("approval_id", "approval_batch_id", "ts_ms", "approver_id", "role",
                      "readiness_report_id", "config_hash", "risk_limits_hash", "approval_reason",
                      "confirmation_text", "expires_ts_ms", "revoked_ts_ms", "status"), record)

    def get_manual_approvals(self, approval_batch_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM manual_approvals WHERE approval_batch_id=?",
                    (approval_batch_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_arming_token(self, record: dict) -> None:
        self._insert("arming_tokens",
                     ("arming_token_id", "token_hash", "approval_batch_id", "readiness_report_id",
                      "config_hash", "mode", "created_ts_ms", "expires_ts_ms", "used_ts_ms",
                      "revoked_ts_ms", "status"), record)

    def get_arming_token_by_hash(self, token_hash: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM arming_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def update_arming_token(self, arming_token_id: str, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            with _LOCK:
                self._conn.execute(f"UPDATE arming_tokens SET {sets} WHERE arming_token_id=?",
                                   [*fields.values(), arming_token_id])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_dry_run_order_intent(self, record: dict) -> None:
        self._insert("dry_run_order_intents",
                     ("dry_run_intent_id", "ts_ms", "venue", "market_id", "market_ticker",
                      "asset_id", "outcome", "side", "order_type", "limit_price", "quantity",
                      "notional", "internal_order_request_json", "venue_payload_json", "unsigned",
                      "unsent", "signer_used", "network_called", "risk_decision_id",
                      "safety_envelope_decision_id", "oms_order_id", "status", "reason"), record)

    def get_dry_run_order_intent(self, dry_run_intent_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM dry_run_order_intents WHERE dry_run_intent_id=?",
                    (dry_run_intent_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def add_safety_envelope_decision(self, record: dict) -> None:
        self._insert("safety_envelope_decisions",
                     ("decision_id", "ts_ms", "allowed", "mode", "state", "reason", "checks_json",
                      "config_hash", "proposal_id", "client_order_id"),
                     self._json_field(record, "checks_json"))

    def add_conformance_run(self, record: dict) -> None:
        cols = ("conformance_run_id", "started_ts_ms", "finished_ts_ms", "status", "config_hash",
                "test_count", "pass_count", "fail_count", "warning_count", "report_path")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO conformance_runs({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(conformance_run_id) DO UPDATE SET status=excluded.status, "
                    "finished_ts_ms=excluded.finished_ts_ms, report_path=excluded.report_path",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_conformance_check(self, record: dict) -> None:
        self._insert("conformance_checks",
                     ("check_id", "conformance_run_id", "check_name", "status", "reason",
                      "details_json"), self._json_field(record, "details_json"))

    def get_conformance_runs(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM conformance_runs ORDER BY started_ts_ms DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_conformance_run(self, conformance_run_id: str) -> dict | None:
        try:
            with _LOCK:
                run = self._conn.execute("SELECT * FROM conformance_runs WHERE conformance_run_id=?",
                                         (conformance_run_id,)).fetchone()
                checks = self._conn.execute(
                    "SELECT * FROM conformance_checks WHERE conformance_run_id=?",
                    (conformance_run_id,)).fetchall()
            if not run:
                return None
            d = dict(run)
            d["checks"] = [dict(c) for c in checks]
            return d
        except Exception:  # noqa: BLE001
            return None

    def add_secret_policy_violation(self, record: dict) -> None:
        self._insert("secret_policy_violations",
                     ("violation_id", "ts_ms", "severity", "location", "violation_type",
                      "redacted_value", "reason", "payload_json"),
                     self._json_field(record, "payload_json"))

    def add_guarded_live_audit_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("guarded_live_audit_events",
                     ("ts_ms", "event_type", "severity", "actor", "state", "config_hash",
                      "payload_json"), self._json_field(record, "payload_json"))

    def get_guarded_live_audit_events(self, limit: int = 200) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM guarded_live_audit_events ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    # --- micro live (Phase 9) ------------------------------------------ #
    def _init_micro_live_schema(self) -> None:
        """Phase 9: micro-live canary execution tables. Idempotent; never wipes
        data; stores only hashes / redacted metadata for anything secret-bearing."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS micro_live_lock_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, lock_name TEXT,
                    passed INTEGER, reason TEXT, required_value TEXT,
                    observed_value_redacted TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_preflights (
                    preflight_id TEXT PRIMARY KEY, ts_ms INTEGER, canary_plan_id TEXT, status TEXT,
                    risk_status TEXT, safety_status TEXT, venue_status TEXT, account_status TEXT,
                    readiness_status TEXT, approval_status TEXT, arming_status TEXT,
                    hard_fail_count INTEGER, warning_count INTEGER, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_canary_plans (
                    canary_plan_id TEXT PRIMARY KEY, created_ts_ms INTEGER, expires_ts_ms INTEGER,
                    venue TEXT, environment TEXT, market_id TEXT, market_ticker TEXT, asset_id TEXT,
                    outcome TEXT, side TEXT, order_type TEXT, time_in_force TEXT, limit_price TEXT,
                    quantity TEXT, notional TEXT, max_slippage TEXT, max_staleness_ms INTEGER,
                    source_shadow_session_id TEXT, source_shadow_decision_id TEXT,
                    source_dry_run_intent_id TEXT, readiness_report_id TEXT, approval_batch_id TEXT,
                    arming_token_id TEXT, risk_decision_id TEXT, safety_envelope_decision_id TEXT,
                    expected_payload_hash TEXT, status TEXT, reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_order_attempts (
                    live_order_attempt_id TEXT PRIMARY KEY, canary_plan_id TEXT, ts_ms INTEGER,
                    venue TEXT, environment TEXT, client_order_id TEXT, exchange_order_id TEXT,
                    status TEXT, submit_allowed INTEGER, submitted INTEGER, acknowledged INTEGER,
                    filled_quantity TEXT, avg_fill_price TEXT, notional_submitted TEXT,
                    notional_filled TEXT, fee TEXT, reject_reason TEXT, error_type TEXT,
                    error_message_redacted TEXT, request_payload_hash TEXT, response_payload_hash TEXT,
                    network_call_count INTEGER DEFAULT 0, signer_used INTEGER DEFAULT 0,
                    risk_decision_id TEXT, safety_envelope_decision_id TEXT, audit_chain_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_account_snapshots (
                    snapshot_id TEXT PRIMARY KEY, ts_ms INTEGER, venue TEXT, environment TEXT,
                    cash_available TEXT, collateral_available TEXT, positions_value TEXT,
                    open_order_notional TEXT, raw_payload_hash TEXT, payload_json_redacted TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_reconciliations (
                    reconciliation_id TEXT PRIMARY KEY, ts_ms INTEGER, live_order_attempt_id TEXT,
                    status TEXT, exchange_order_status TEXT, local_order_status TEXT,
                    filled_quantity TEXT, local_filled_quantity TEXT, fee TEXT, position_delta TEXT,
                    discrepancies_json TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_emergency_cancels (
                    cancel_id TEXT PRIMARY KEY, ts_ms INTEGER, venue TEXT, environment TEXT,
                    requested_by TEXT, reason TEXT, client_order_id TEXT, exchange_order_id TEXT,
                    cancel_all INTEGER, sent INTEGER, success INTEGER, response_hash TEXT,
                    error_message_redacted TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, event_type TEXT,
                    severity TEXT, actor TEXT, canary_plan_id TEXT, live_order_attempt_id TEXT,
                    state TEXT, message TEXT, payload_json TEXT, audit_chain_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS micro_live_reports (
                    report_id TEXT PRIMARY KEY, ts_ms INTEGER, canary_plan_id TEXT,
                    live_order_attempt_id TEXT, status TEXT, report_path TEXT, summary_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_mla_plan ON micro_live_order_attempts(canary_plan_id);
                CREATE INDEX IF NOT EXISTS idx_mla_ts ON micro_live_order_attempts(ts_ms);
                """
            )
            self._conn.commit()

    def add_micro_live_lock_check(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("micro_live_lock_checks",
                     ("ts_ms", "lock_name", "passed", "reason", "required_value",
                      "observed_value_redacted", "payload_json"),
                     self._json_field(record, "payload_json"))

    def add_micro_live_preflight(self, record: dict) -> None:
        self._insert("micro_live_preflights",
                     ("preflight_id", "ts_ms", "canary_plan_id", "status", "risk_status",
                      "safety_status", "venue_status", "account_status", "readiness_status",
                      "approval_status", "arming_status", "hard_fail_count", "warning_count",
                      "payload_json"), record)

    def get_micro_live_preflights(self, limit: int = 50) -> list[dict]:
        return self._select_all("micro_live_preflights", "ts_ms", limit)

    def add_micro_live_canary_plan(self, record: dict) -> None:
        self._insert("micro_live_canary_plans",
                     ("canary_plan_id", "created_ts_ms", "expires_ts_ms", "venue", "environment",
                      "market_id", "market_ticker", "asset_id", "outcome", "side", "order_type",
                      "time_in_force", "limit_price", "quantity", "notional", "max_slippage",
                      "max_staleness_ms", "source_shadow_session_id", "source_shadow_decision_id",
                      "source_dry_run_intent_id", "readiness_report_id", "approval_batch_id",
                      "arming_token_id", "risk_decision_id", "safety_envelope_decision_id",
                      "expected_payload_hash", "status", "reason", "payload_json"), record)

    def get_micro_live_canary_plan(self, canary_plan_id: str) -> dict | None:
        return self._select_one("micro_live_canary_plans", "canary_plan_id", canary_plan_id)

    def get_micro_live_canary_plans(self, limit: int = 50) -> list[dict]:
        return self._select_all("micro_live_canary_plans", "created_ts_ms", limit)

    def add_micro_live_order_attempt(self, record: dict) -> None:
        cols = ("live_order_attempt_id", "canary_plan_id", "ts_ms", "venue", "environment",
                "client_order_id", "exchange_order_id", "status", "submit_allowed", "submitted",
                "acknowledged", "filled_quantity", "avg_fill_price", "notional_submitted",
                "notional_filled", "fee", "reject_reason", "error_type", "error_message_redacted",
                "request_payload_hash", "response_payload_hash", "network_call_count",
                "signer_used", "risk_decision_id", "safety_envelope_decision_id", "audit_chain_hash")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO micro_live_order_attempts({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(live_order_attempt_id) DO UPDATE SET status=excluded.status, "
                    "exchange_order_id=excluded.exchange_order_id, submitted=excluded.submitted, "
                    "acknowledged=excluded.acknowledged, filled_quantity=excluded.filled_quantity, "
                    "avg_fill_price=excluded.avg_fill_price, notional_filled=excluded.notional_filled, "
                    "fee=excluded.fee, reject_reason=excluded.reject_reason, "
                    "error_type=excluded.error_type, "
                    "error_message_redacted=excluded.error_message_redacted, "
                    "response_payload_hash=excluded.response_payload_hash, "
                    "network_call_count=excluded.network_call_count, "
                    "signer_used=excluded.signer_used, audit_chain_hash=excluded.audit_chain_hash",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_micro_live_attempts(self, limit: int = 100) -> list[dict]:
        return self._select_all("micro_live_order_attempts", "ts_ms", limit)

    def get_micro_live_attempt(self, live_order_attempt_id: str) -> dict | None:
        return self._select_one("micro_live_order_attempts", "live_order_attempt_id",
                                live_order_attempt_id)

    def get_micro_live_attempts_for_plan(self, canary_plan_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM micro_live_order_attempts WHERE canary_plan_id=?",
                    (canary_plan_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_micro_live_account_snapshot(self, record: dict) -> None:
        self._insert("micro_live_account_snapshots",
                     ("snapshot_id", "ts_ms", "venue", "environment", "cash_available",
                      "collateral_available", "positions_value", "open_order_notional",
                      "raw_payload_hash", "payload_json_redacted"), record)

    def add_micro_live_reconciliation(self, record: dict) -> None:
        self._insert("micro_live_reconciliations",
                     ("reconciliation_id", "ts_ms", "live_order_attempt_id", "status",
                      "exchange_order_status", "local_order_status", "filled_quantity",
                      "local_filled_quantity", "fee", "position_delta", "discrepancies_json"),
                     record)

    def get_micro_live_reconciliations(self, limit: int = 50) -> list[dict]:
        return self._select_all("micro_live_reconciliations", "ts_ms", limit)

    def add_micro_live_emergency_cancel(self, record: dict) -> None:
        self._insert("micro_live_emergency_cancels",
                     ("cancel_id", "ts_ms", "venue", "environment", "requested_by", "reason",
                      "client_order_id", "exchange_order_id", "cancel_all", "sent", "success",
                      "response_hash", "error_message_redacted", "payload_json"), record)

    def add_micro_live_audit_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("micro_live_audit_events",
                     ("ts_ms", "event_type", "severity", "actor", "canary_plan_id",
                      "live_order_attempt_id", "state", "message", "payload_json",
                      "audit_chain_hash"), self._json_field(record, "payload_json"))

    def get_micro_live_audit_events(self, limit: int = 200) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM micro_live_audit_events ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_last_micro_live_audit_hash(self) -> str | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT audit_chain_hash FROM micro_live_audit_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return row["audit_chain_hash"] if row else None
        except Exception:  # noqa: BLE001
            return None

    def add_micro_live_report(self, record: dict) -> None:
        self._insert("micro_live_reports",
                     ("report_id", "ts_ms", "canary_plan_id", "live_order_attempt_id", "status",
                      "report_path", "summary_json"), self._json_field(record, "summary_json"))

    def get_micro_live_reports(self, limit: int = 50) -> list[dict]:
        return self._select_all("micro_live_reports", "ts_ms", limit)

    def _select_all(self, table: str, order_col: str, limit: int) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def _select_one(self, table: str, key_col: str, key: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    f"SELECT * FROM {table} WHERE {key_col}=?", (key,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    # --- extra micro-live getters used by post-canary (Phase 10) ------- #
    def get_micro_live_emergency_cancels(self, limit: int = 200) -> list[dict]:
        return self._select_all("micro_live_emergency_cancels", "ts_ms", limit)

    def get_micro_live_account_snapshots(self, limit: int = 200) -> list[dict]:
        return self._select_all("micro_live_account_snapshots", "ts_ms", limit)

    def get_safety_envelope_decision(self, decision_id: str) -> dict | None:
        return self._select_one("safety_envelope_decisions", "decision_id", decision_id)

    # --- post-canary (Phase 10) ---------------------------------------- #
    def _init_post_canary_schema(self) -> None:
        """Phase 10: post-canary analysis + veto tables. Idempotent; never wipes
        data; stores only redacted payloads / hashes."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS post_canary_analyses (
                    analysis_id TEXT PRIMARY KEY, live_order_attempt_id TEXT, canary_plan_id TEXT,
                    ts_ms INTEGER, status TEXT, recommendation TEXT, hard_fail_count INTEGER,
                    warning_count INTEGER, unknown_blocking_count INTEGER,
                    clean_for_repeat_demo_same_size INTEGER,
                    eligible_for_production_design_review INTEGER,
                    eligible_for_size_increase INTEGER DEFAULT 0,
                    eligible_for_autonomous_live INTEGER DEFAULT 0, summary_json TEXT,
                    blocking_reasons_json TEXT, next_required_actions_json TEXT, report_path TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_audit_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id TEXT, category TEXT,
                    check_name TEXT, status TEXT, severity TEXT, reason TEXT, observed_value TEXT,
                    expected_value TEXT, threshold TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_reconciliation_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, exchange_status TEXT,
                    local_status TEXT, filled_quantity TEXT, local_filled_quantity TEXT, fee TEXT,
                    local_fee TEXT, position_delta TEXT, local_position_delta TEXT,
                    discrepancies_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_execution_quality (
                    analysis_id TEXT PRIMARY KEY, status TEXT, intended_price TEXT,
                    submitted_price TEXT, avg_fill_price TEXT, intended_quantity TEXT,
                    filled_quantity TEXT, intended_notional TEXT, filled_notional TEXT,
                    slippage_bps TEXT, payload_drift_detected INTEGER, unexpected_partial_fill INTEGER,
                    unexpected_resting_order INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_markout (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id TEXT, horizon_ms INTEGER,
                    observed_ts_ms INTEGER, best_bid TEXT, best_ask TEXT, midpoint TEXT, spread TEXT,
                    last_trade_price TEXT, markout_vs_mid TEXT, markout_vs_touch TEXT,
                    adverse_selection TEXT, data_missing INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_market_data_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, bbo_age_ms INTEGER,
                    orderbook_age_ms INTEGER, spread TEXT, depth_at_limit TEXT,
                    sequence_gap_detected INTEGER, tick_dirty INTEGER, venue_status TEXT,
                    market_status TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_research_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, estimate_id TEXT, p_ensemble TEXT,
                    confidence TEXT, evidence_score TEXT, source_count INTEGER, ambiguity_score TEXT,
                    stale INTEGER, no_trade_reason TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_risk_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, risk_decision_id TEXT,
                    safety_envelope_decision_id TEXT, risk_approved INTEGER, safety_allowed INTEGER,
                    bypass_detected INTEGER, limit_breach_detected INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_chain_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, missing_links_json TEXT,
                    audit_chain_hash_valid INTEGER, trace_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_secret_audits (
                    analysis_id TEXT PRIMARY KEY, status TEXT, secret_leak_count INTEGER,
                    redaction_count INTEGER, violations_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_eligibility (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT, environment TEXT, ts_ms INTEGER,
                    total_canaries INTEGER, clean_canaries INTEGER, failed_canaries INTEGER,
                    unresolved_canaries INTEGER, emergency_cancel_count INTEGER,
                    clean_demo_canary_streak INTEGER, last_clean_canary_ts_ms INTEGER,
                    renewed_shadow_hours_after_last_canary TEXT,
                    renewed_shadow_decisions_after_last_canary INTEGER,
                    eligible_repeat_demo_same_size INTEGER, eligible_production_design_review INTEGER,
                    eligible_size_increase INTEGER DEFAULT 0, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_reports (
                    report_id TEXT PRIMARY KEY, analysis_id TEXT, ts_ms INTEGER, status TEXT,
                    recommendation TEXT, report_path TEXT, summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS post_canary_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, analysis_id TEXT,
                    event_type TEXT, severity TEXT, actor TEXT, message TEXT, payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pca_attempt ON post_canary_analyses(live_order_attempt_id);
                CREATE INDEX IF NOT EXISTS idx_pca_checks ON post_canary_audit_checks(analysis_id);
                """
            )
            self._conn.commit()

    def add_post_canary_analysis(self, record: dict) -> None:
        cols = ("analysis_id", "live_order_attempt_id", "canary_plan_id", "ts_ms", "status",
                "recommendation", "hard_fail_count", "warning_count", "unknown_blocking_count",
                "clean_for_repeat_demo_same_size", "eligible_for_production_design_review",
                "eligible_for_size_increase", "eligible_for_autonomous_live", "summary_json",
                "blocking_reasons_json", "next_required_actions_json", "report_path")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO post_canary_analyses({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(analysis_id) DO UPDATE SET status=excluded.status, "
                    "recommendation=excluded.recommendation, report_path=excluded.report_path",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_post_canary_analyses(self, limit: int = 100) -> list[dict]:
        return self._select_all("post_canary_analyses", "ts_ms", limit)

    def get_post_canary_analysis(self, analysis_id: str) -> dict | None:
        return self._select_one("post_canary_analyses", "analysis_id", analysis_id)

    def add_post_canary_audit_check(self, record: dict) -> None:
        self._insert("post_canary_audit_checks",
                     ("analysis_id", "category", "check_name", "status", "severity", "reason",
                      "observed_value", "expected_value", "threshold", "details_json"),
                     self._json_field(record, "details_json"))

    def get_post_canary_audit_checks(self, analysis_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM post_canary_audit_checks WHERE analysis_id=? ORDER BY id ASC",
                    (analysis_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_post_canary_reconciliation_audit(self, record: dict) -> None:
        self._insert("post_canary_reconciliation_audits",
                     ("analysis_id", "status", "exchange_status", "local_status", "filled_quantity",
                      "local_filled_quantity", "fee", "local_fee", "position_delta",
                      "local_position_delta", "discrepancies_json"), record)

    def add_post_canary_execution_quality(self, record: dict) -> None:
        self._insert("post_canary_execution_quality",
                     ("analysis_id", "status", "intended_price", "submitted_price",
                      "avg_fill_price", "intended_quantity", "filled_quantity", "intended_notional",
                      "filled_notional", "slippage_bps", "payload_drift_detected",
                      "unexpected_partial_fill", "unexpected_resting_order", "details_json"), record)

    def add_post_canary_markout(self, record: dict) -> None:
        self._insert("post_canary_markout",
                     ("analysis_id", "horizon_ms", "observed_ts_ms", "best_bid", "best_ask",
                      "midpoint", "spread", "last_trade_price", "markout_vs_mid", "markout_vs_touch",
                      "adverse_selection", "data_missing", "details_json"), record)

    def get_post_canary_markout(self, analysis_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM post_canary_markout WHERE analysis_id=? ORDER BY horizon_ms ASC",
                    (analysis_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_post_canary_market_data_audit(self, record: dict) -> None:
        self._insert("post_canary_market_data_audits",
                     ("analysis_id", "status", "bbo_age_ms", "orderbook_age_ms", "spread",
                      "depth_at_limit", "sequence_gap_detected", "tick_dirty", "venue_status",
                      "market_status", "details_json"), record)

    def add_post_canary_research_audit(self, record: dict) -> None:
        self._insert("post_canary_research_audits",
                     ("analysis_id", "status", "estimate_id", "p_ensemble", "confidence",
                      "evidence_score", "source_count", "ambiguity_score", "stale",
                      "no_trade_reason", "details_json"), record)

    def add_post_canary_risk_audit(self, record: dict) -> None:
        self._insert("post_canary_risk_audits",
                     ("analysis_id", "status", "risk_decision_id", "safety_envelope_decision_id",
                      "risk_approved", "safety_allowed", "bypass_detected", "limit_breach_detected",
                      "details_json"), record)

    def add_post_canary_chain_audit(self, record: dict) -> None:
        self._insert("post_canary_chain_audits",
                     ("analysis_id", "status", "missing_links_json", "audit_chain_hash_valid",
                      "trace_json"), record)

    def add_post_canary_secret_audit(self, record: dict) -> None:
        self._insert("post_canary_secret_audits",
                     ("analysis_id", "status", "secret_leak_count", "redaction_count",
                      "violations_json"), record)

    def add_post_canary_eligibility(self, record: dict) -> None:
        self._insert("post_canary_eligibility",
                     ("venue", "environment", "ts_ms", "total_canaries", "clean_canaries",
                      "failed_canaries", "unresolved_canaries", "emergency_cancel_count",
                      "clean_demo_canary_streak", "last_clean_canary_ts_ms",
                      "renewed_shadow_hours_after_last_canary",
                      "renewed_shadow_decisions_after_last_canary", "eligible_repeat_demo_same_size",
                      "eligible_production_design_review", "eligible_size_increase", "reason"),
                     record)

    def get_post_canary_eligibility(self, limit: int = 50) -> list[dict]:
        return self._select_all("post_canary_eligibility", "ts_ms", limit)

    def add_post_canary_report(self, record: dict) -> None:
        self._insert("post_canary_reports",
                     ("report_id", "analysis_id", "ts_ms", "status", "recommendation",
                      "report_path", "summary_json"), self._json_field(record, "summary_json"))

    def get_post_canary_reports(self, limit: int = 50) -> list[dict]:
        return self._select_all("post_canary_reports", "ts_ms", limit)

    def add_post_canary_audit_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("post_canary_audit_events",
                     ("ts_ms", "analysis_id", "event_type", "severity", "actor", "message",
                      "payload_json"), self._json_field(record, "payload_json"))

    def get_post_canary_audit_events(self, limit: int = 200) -> list[dict]:
        return self._select_all("post_canary_audit_events", "id", limit)

    def get_post_canary_rows(self, table: str, limit: int = 100000) -> list[dict]:
        if not table.startswith("post_canary_"):
            return []
        return self._select_all(table, "rowid", limit)

    # --- production review (Phase 11) ---------------------------------- #
    def _init_production_review_schema(self) -> None:
        """Phase 11: production-canary DESIGN REVIEW tables. Idempotent; never
        wipes data; stores only redacted account identifiers + manual attestations;
        never raw secrets."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS production_review_runs (
                    review_id TEXT PRIMARY KEY, ts_ms INTEGER, status TEXT, recommendation TEXT,
                    generated_by TEXT, hard_fail_count INTEGER, warning_count INTEGER,
                    blocked_count INTEGER, eligible_to_draft_phase12_plan INTEGER,
                    eligible_for_production_execution INTEGER DEFAULT 0,
                    eligible_for_size_increase INTEGER DEFAULT 0,
                    eligible_for_autonomous_live INTEGER DEFAULT 0, blocking_reasons_json TEXT,
                    next_required_actions_json TEXT, summary_json TEXT, report_path TEXT
                );
                CREATE TABLE IF NOT EXISTS production_review_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT, category TEXT,
                    check_name TEXT, status TEXT, severity TEXT, reason TEXT, observed_value TEXT,
                    expected_value TEXT, evidence_ref TEXT, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_evidence_summaries (
                    evidence_id TEXT PRIMARY KEY, review_id TEXT, ts_ms INTEGER,
                    latest_shadow_report_id TEXT, latest_post_canary_analysis_id TEXT,
                    clean_demo_canary_count INTEGER, unresolved_canary_count INTEGER,
                    failed_canary_count INTEGER, renewed_shadow_hours TEXT,
                    renewed_shadow_decisions INTEGER, guarded_live_conformance_status TEXT,
                    micro_live_conformance_status TEXT, post_canary_eligibility_status TEXT,
                    missing_evidence_json TEXT, stale_evidence_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_account_readiness (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT, status TEXT,
                    venue_accounts_reviewed_json TEXT, production_account_attested INTEGER,
                    read_only_snapshot_used INTEGER, funding_or_collateral_attested INTEGER,
                    restrictions_attested_clear INTEGER, no_funds_moved INTEGER
                );
                CREATE TABLE IF NOT EXISTS production_venue_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT, venue TEXT, status TEXT,
                    environment_separation_passed INTEGER, read_only_key_separated INTEGER,
                    trading_key_custody_plan_present INTEGER, private_user_channels_disabled INTEGER,
                    order_endpoints_blocked INTEGER, forbidden_flows_blocked INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_jurisdiction_attestations (
                    attestation_id TEXT PRIMARY KEY, ts_ms INTEGER, reviewer_id TEXT, venue TEXT,
                    account_identifier_redacted TEXT, jurisdiction_reviewed INTEGER,
                    eligibility_confirmed_by_operator INTEGER, venue_terms_reviewed INTEGER,
                    prohibited_market_categories_reviewed INTEGER,
                    tax_reporting_out_of_scope_acknowledged INTEGER,
                    legal_advice_not_provided_acknowledged INTEGER, confirmation_text TEXT,
                    expires_ts_ms INTEGER, revoked_ts_ms INTEGER, status TEXT
                );
                CREATE TABLE IF NOT EXISTS production_endpoint_separation (
                    review_id TEXT PRIMARY KEY, status TEXT, api_submit_routes_found INTEGER,
                    dashboard_submit_controls_found INTEGER, strategy_production_paths_found INTEGER,
                    grok_production_paths_found INTEGER, production_order_endpoint_reachable INTEGER,
                    read_only_endpoints_isolated INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_credential_custody (
                    review_id TEXT PRIMARY KEY, status TEXT, raw_secret_findings INTEGER,
                    redaction_findings INTEGER, production_signer_loaded INTEGER,
                    wallet_private_key_loaded INTEGER, db_secret_findings INTEGER,
                    artifact_secret_findings INTEGER, custody_plan_present INTEGER,
                    rotation_plan_present INTEGER, revocation_plan_present INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_conformance_runs (
                    conformance_run_id TEXT PRIMARY KEY, review_id TEXT, ts_ms INTEGER, status TEXT,
                    mock_only INTEGER, real_network_calls INTEGER, production_order_calls INTEGER,
                    production_cancel_calls INTEGER, production_signer_calls INTEGER,
                    report_path TEXT, summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_operational_readiness (
                    review_id TEXT PRIMARY KEY, status TEXT, runbook_present INTEGER,
                    monitoring_plan_present INTEGER, incident_response_present INTEGER,
                    rollback_plan_present INTEGER, emergency_contact_placeholder_present INTEGER,
                    manual_exchange_ui_checklist_present INTEGER, details_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_change_control (
                    change_id TEXT PRIMARY KEY, ts_ms INTEGER, requester_id TEXT, reviewers_json TEXT,
                    review_id TEXT, intended_scope TEXT, risk_summary TEXT, evidence_refs_json TEXT,
                    rollback_plan_ref TEXT, no_execution_statement TEXT, approval_status TEXT,
                    expires_ts_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS production_human_checklists (
                    checklist_id TEXT PRIMARY KEY, ts_ms INTEGER, reviewer_id TEXT, review_id TEXT,
                    all_required_items_passed INTEGER, confirmation_text TEXT, status TEXT,
                    items_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_review_reports (
                    report_id TEXT PRIMARY KEY, review_id TEXT, ts_ms INTEGER, status TEXT,
                    recommendation TEXT, report_path TEXT, summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS production_review_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, review_id TEXT,
                    event_type TEXT, severity TEXT, actor TEXT, message TEXT, payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_prr_ts ON production_review_runs(ts_ms);
                CREATE INDEX IF NOT EXISTS idx_prc_review ON production_review_checks(review_id);
                """
            )
            self._conn.commit()

    def add_production_review_run(self, record: dict) -> None:
        cols = ("review_id", "ts_ms", "status", "recommendation", "generated_by",
                "hard_fail_count", "warning_count", "blocked_count",
                "eligible_to_draft_phase12_plan", "eligible_for_production_execution",
                "eligible_for_size_increase", "eligible_for_autonomous_live",
                "blocking_reasons_json", "next_required_actions_json", "summary_json", "report_path")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO production_review_runs({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(review_id) DO UPDATE SET status=excluded.status, "
                    "recommendation=excluded.recommendation, report_path=excluded.report_path",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_production_review_runs(self, limit: int = 50) -> list[dict]:
        return self._select_all("production_review_runs", "ts_ms", limit)

    def get_production_review_run(self, review_id: str) -> dict | None:
        return self._select_one("production_review_runs", "review_id", review_id)

    def add_production_review_check(self, record: dict) -> None:
        self._insert("production_review_checks",
                     ("review_id", "category", "check_name", "status", "severity", "reason",
                      "observed_value", "expected_value", "evidence_ref", "details_json"),
                     self._json_field(record, "details_json"))

    def get_production_review_checks(self, review_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM production_review_checks WHERE review_id=? ORDER BY id ASC",
                    (review_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_production_evidence_summary(self, record: dict) -> None:
        self._insert("production_evidence_summaries",
                     ("evidence_id", "review_id", "ts_ms", "latest_shadow_report_id",
                      "latest_post_canary_analysis_id", "clean_demo_canary_count",
                      "unresolved_canary_count", "failed_canary_count", "renewed_shadow_hours",
                      "renewed_shadow_decisions", "guarded_live_conformance_status",
                      "micro_live_conformance_status", "post_canary_eligibility_status",
                      "missing_evidence_json", "stale_evidence_json"), record)

    def add_production_account_readiness(self, record: dict) -> None:
        self._insert("production_account_readiness",
                     ("review_id", "status", "venue_accounts_reviewed_json",
                      "production_account_attested", "read_only_snapshot_used",
                      "funding_or_collateral_attested", "restrictions_attested_clear",
                      "no_funds_moved"), record)

    def add_production_venue_permission(self, record: dict) -> None:
        self._insert("production_venue_permissions",
                     ("review_id", "venue", "status", "environment_separation_passed",
                      "read_only_key_separated", "trading_key_custody_plan_present",
                      "private_user_channels_disabled", "order_endpoints_blocked",
                      "forbidden_flows_blocked", "details_json"), record)

    def add_production_jurisdiction_attestation(self, record: dict) -> None:
        self._insert("production_jurisdiction_attestations",
                     ("attestation_id", "ts_ms", "reviewer_id", "venue",
                      "account_identifier_redacted", "jurisdiction_reviewed",
                      "eligibility_confirmed_by_operator", "venue_terms_reviewed",
                      "prohibited_market_categories_reviewed",
                      "tax_reporting_out_of_scope_acknowledged",
                      "legal_advice_not_provided_acknowledged", "confirmation_text",
                      "expires_ts_ms", "revoked_ts_ms", "status"), record)

    def get_production_jurisdiction_attestations(self, limit: int = 200) -> list[dict]:
        return self._select_all("production_jurisdiction_attestations", "ts_ms", limit)

    def add_production_endpoint_separation(self, record: dict) -> None:
        cols = ("review_id", "status", "api_submit_routes_found", "dashboard_submit_controls_found",
                "strategy_production_paths_found", "grok_production_paths_found",
                "production_order_endpoint_reachable", "read_only_endpoints_isolated",
                "details_json")
        self._upsert("production_endpoint_separation", "review_id", cols,
                     self._json_field(record, "details_json"))

    def add_production_credential_custody(self, record: dict) -> None:
        cols = ("review_id", "status", "raw_secret_findings", "redaction_findings",
                "production_signer_loaded", "wallet_private_key_loaded", "db_secret_findings",
                "artifact_secret_findings", "custody_plan_present", "rotation_plan_present",
                "revocation_plan_present", "details_json")
        self._upsert("production_credential_custody", "review_id", cols,
                     self._json_field(record, "details_json"))

    def add_production_conformance_run(self, record: dict) -> None:
        self._insert("production_conformance_runs",
                     ("conformance_run_id", "review_id", "ts_ms", "status", "mock_only",
                      "real_network_calls", "production_order_calls", "production_cancel_calls",
                      "production_signer_calls", "report_path", "summary_json"),
                     self._json_field(record, "summary_json"))

    def get_production_conformance_runs(self, limit: int = 50) -> list[dict]:
        return self._select_all("production_conformance_runs", "ts_ms", limit)

    def add_production_operational_readiness(self, record: dict) -> None:
        cols = ("review_id", "status", "runbook_present", "monitoring_plan_present",
                "incident_response_present", "rollback_plan_present",
                "emergency_contact_placeholder_present", "manual_exchange_ui_checklist_present",
                "details_json")
        self._upsert("production_operational_readiness", "review_id", cols,
                     self._json_field(record, "details_json"))

    def add_production_change_control(self, record: dict) -> None:
        self._insert("production_change_control",
                     ("change_id", "ts_ms", "requester_id", "reviewers_json", "review_id",
                      "intended_scope", "risk_summary", "evidence_refs_json", "rollback_plan_ref",
                      "no_execution_statement", "approval_status", "expires_ts_ms"), record)

    def get_production_change_control(self, limit: int = 50) -> list[dict]:
        return self._select_all("production_change_control", "ts_ms", limit)

    def add_production_human_checklist(self, record: dict) -> None:
        self._insert("production_human_checklists",
                     ("checklist_id", "ts_ms", "reviewer_id", "review_id",
                      "all_required_items_passed", "confirmation_text", "status", "items_json"),
                     self._json_field(record, "items_json"))

    def get_production_human_checklists(self, limit: int = 50) -> list[dict]:
        return self._select_all("production_human_checklists", "ts_ms", limit)

    def add_production_review_report(self, record: dict) -> None:
        self._insert("production_review_reports",
                     ("report_id", "review_id", "ts_ms", "status", "recommendation", "report_path",
                      "summary_json"), self._json_field(record, "summary_json"))

    def get_production_review_reports(self, limit: int = 50) -> list[dict]:
        return self._select_all("production_review_reports", "ts_ms", limit)

    def add_production_review_audit_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("production_review_audit_events",
                     ("ts_ms", "review_id", "event_type", "severity", "actor", "message",
                      "payload_json"), self._json_field(record, "payload_json"))

    def get_production_review_audit_events(self, limit: int = 200) -> list[dict]:
        return self._select_all("production_review_audit_events", "id", limit)

    def _upsert(self, table: str, key_col: str, cols: tuple, record: dict) -> None:
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key_col)
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                    f"ON CONFLICT({key_col}) DO UPDATE SET {updates}",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_guarded_rows(self, table: str, limit: int = 100000) -> list[dict]:
        if table not in self._GUARDED_ROW_TABLES:
            return []
        try:
            with _LOCK:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def _init_research_schema(self) -> None:
        """Phase 5: research/probability tables. Idempotent; never wipes data.
        API keys and full prompts are NEVER stored here (only prompt hashes)."""
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_runs (
                    research_run_id TEXT PRIMARY KEY,
                    ts_ms INTEGER, status TEXT, mode TEXT, model TEXT, venue TEXT,
                    market_id TEXT, asset_id TEXT, outcome TEXT, prompt_hash TEXT,
                    config_hash TEXT, request_tokens INTEGER, response_tokens INTEGER,
                    reasoning_tokens INTEGER, cached_tokens INTEGER, estimated_cost_usd TEXT,
                    latency_ms INTEGER, error_type TEXT, error_message TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS research_sources (
                    source_id TEXT PRIMARY KEY, source_type TEXT, normalized_url TEXT,
                    title TEXT, publisher TEXT, author TEXT, published_ts_ms INTEGER,
                    retrieved_ts_ms INTEGER, credibility TEXT, content_hash TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS research_evidence (
                    evidence_id TEXT PRIMARY KEY, research_run_id TEXT, estimate_id TEXT,
                    source_id TEXT, venue TEXT, market_id TEXT, asset_id TEXT, claim TEXT,
                    short_excerpt TEXT, direction TEXT, weight TEXT, credibility TEXT,
                    freshness TEXT, relevance TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS market_rule_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT, market_id TEXT, asset_id TEXT, question TEXT, outcome TEXT,
                    resolution_source TEXT, close_ts_ms INTEGER, resolution_deadline_ts_ms INTEGER,
                    criteria_json TEXT, edge_cases_json TEXT, ambiguous_terms_json TEXT,
                    ambiguity_categories_json TEXT, ambiguity_score TEXT, parsed_ts_ms INTEGER,
                    UNIQUE(venue, market_id, asset_id, outcome)
                );
                CREATE TABLE IF NOT EXISTS probability_estimates (
                    estimate_id TEXT PRIMARY KEY, research_run_id TEXT, venue TEXT,
                    market_id TEXT, asset_id TEXT, outcome TEXT, ts_ms INTEGER,
                    p_market_mid TEXT, p_llm_raw TEXT, p_model TEXT, p_calibrated TEXT,
                    p_ensemble TEXT, confidence TEXT, ambiguity_score TEXT, evidence_score TEXT,
                    source_count INTEGER, calibration_version TEXT, ensemble_version TEXT,
                    stale_after_ts_ms INTEGER, no_trade_reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS research_budget_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER, event_type TEXT, model TEXT, market_id TEXT,
                    estimated_cost_usd TEXT, reason TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS research_validation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER, research_run_id TEXT, severity TEXT, event_type TEXT,
                    reason TEXT, payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_prob_est_market
                    ON probability_estimates(venue, market_id, asset_id, ts_ms);
                CREATE INDEX IF NOT EXISTS idx_research_evidence_run
                    ON research_evidence(research_run_id);
                CREATE INDEX IF NOT EXISTS idx_research_runs_ts ON research_runs(ts_ms);
                """
            )
            self._conn.commit()

    # --- research (Phase 5) --------------------------------------------- #
    def add_research_run(self, record: dict) -> None:
        cols = ("research_run_id", "ts_ms", "status", "mode", "model", "venue",
                "market_id", "asset_id", "outcome", "prompt_hash", "config_hash",
                "request_tokens", "response_tokens", "reasoning_tokens", "cached_tokens",
                "estimated_cost_usd", "latency_ms", "error_type", "error_message", "payload_json")
        rec = self._json_field(record, "payload_json")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO research_runs({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(research_run_id) DO UPDATE SET "
                    "status=excluded.status, error_type=excluded.error_type, "
                    "error_message=excluded.error_message, payload_json=excluded.payload_json",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def upsert_research_source(self, record: dict) -> None:
        cols = ("source_id", "source_type", "normalized_url", "title", "publisher",
                "author", "published_ts_ms", "retrieved_ts_ms", "credibility",
                "content_hash", "payload_json")
        rec = self._json_field(record, "payload_json")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO research_sources({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(source_id) DO UPDATE SET title=excluded.title, "
                    "credibility=excluded.credibility, retrieved_ts_ms=excluded.retrieved_ts_ms",
                    [rec.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_research_evidence(self, record: dict) -> None:
        self._insert("research_evidence",
                     ("evidence_id", "research_run_id", "estimate_id", "source_id", "venue",
                      "market_id", "asset_id", "claim", "short_excerpt", "direction", "weight",
                      "credibility", "freshness", "relevance", "payload_json"),
                     self._json_field(record, "payload_json"))

    def upsert_market_rule_summary(self, record: dict) -> None:
        cols = ("venue", "market_id", "asset_id", "question", "outcome", "resolution_source",
                "close_ts_ms", "resolution_deadline_ts_ms", "criteria_json", "edge_cases_json",
                "ambiguous_terms_json", "ambiguity_categories_json", "ambiguity_score", "parsed_ts_ms")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO market_rule_summaries({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(venue, market_id, asset_id, outcome) DO UPDATE SET "
                    "question=excluded.question, resolution_source=excluded.resolution_source, "
                    "ambiguity_score=excluded.ambiguity_score, "
                    "ambiguity_categories_json=excluded.ambiguity_categories_json, "
                    "parsed_ts_ms=excluded.parsed_ts_ms",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_probability_estimate(self, record: dict) -> None:
        self._insert("probability_estimates",
                     ("estimate_id", "research_run_id", "venue", "market_id", "asset_id",
                      "outcome", "ts_ms", "p_market_mid", "p_llm_raw", "p_model", "p_calibrated",
                      "p_ensemble", "confidence", "ambiguity_score", "evidence_score",
                      "source_count", "calibration_version", "ensemble_version",
                      "stale_after_ts_ms", "no_trade_reason", "payload_json"),
                     self._json_field(record, "payload_json"))

    def add_research_budget_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("research_budget_events",
                     ("ts_ms", "event_type", "model", "market_id", "estimated_cost_usd",
                      "reason", "payload_json"), self._json_field(record, "payload_json"))

    def add_research_validation_event(self, record: dict) -> None:
        record.setdefault("ts_ms", int(time.time() * 1000))
        self._insert("research_validation_events",
                     ("ts_ms", "research_run_id", "severity", "event_type", "reason",
                      "payload_json"), self._json_field(record, "payload_json"))

    def _json_field(self, record: dict, field: str) -> dict:
        v = record.get(field)
        if v is not None and not isinstance(v, str):
            return {**record, field: json.dumps(v, default=str)}
        return record

    def get_research_runs(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM research_runs ORDER BY ts_ms DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_research_run(self, research_run_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM research_runs WHERE research_run_id=?",
                    (research_run_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_probability_estimates(self, *, venue: str | None = None,
                                  market_id: str | None = None, asset_id: str | None = None,
                                  limit: int = 100) -> list[dict]:
        try:
            q = "SELECT * FROM probability_estimates"
            clauses, params = [], []
            if venue:
                clauses.append("venue=?"); params.append(venue)
            if market_id:
                clauses.append("market_id=?"); params.append(market_id)
            if asset_id:
                clauses.append("asset_id=?"); params.append(asset_id)
            if clauses:
                q += " WHERE " + " AND ".join(clauses)
            q += " ORDER BY ts_ms DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_probability_estimate(self, estimate_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM probability_estimates WHERE estimate_id=?",
                    (estimate_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_latest_estimate_before(self, venue: str, market_id: str,
                                   asset_id: str | None, at_ts_ms: int) -> dict | None:
        """Deterministic replay lookup: latest estimate at or before a timestamp."""
        try:
            q = ("SELECT * FROM probability_estimates WHERE venue=? AND market_id=? "
                 "AND ts_ms<=?")
            params: list = [venue, market_id, int(at_ts_ms)]
            if asset_id is not None:
                q += " AND asset_id=?"
                params.append(asset_id)
            q += " ORDER BY ts_ms DESC LIMIT 1"
            with _LOCK:
                row = self._conn.execute(q, params).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_research_evidence(self, *, research_run_id: str | None = None,
                              estimate_id: str | None = None, limit: int = 200) -> list[dict]:
        try:
            q = "SELECT * FROM research_evidence"
            clauses, params = [], []
            if research_run_id:
                clauses.append("research_run_id=?"); params.append(research_run_id)
            if estimate_id:
                clauses.append("estimate_id=?"); params.append(estimate_id)
            if clauses:
                q += " WHERE " + " AND ".join(clauses)
            q += " LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_market_rule_summary(self, venue: str, market_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM market_rule_summaries WHERE venue=? AND market_id=?",
                    (venue, market_id)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    # --- meta -----------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        with _LOCK:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    # --- trades ---------------------------------------------------------
    def add_trade(self, **kw) -> int:
        kw.setdefault("ts", time.time())
        kw.setdefault("status", "open")
        kw.setdefault("pnl", 0.0)
        kw["meta"] = json.dumps(kw.get("meta") or {})
        cols = ["ts", "market", "symbol", "side", "qty", "price", "stake", "status", "pnl", "rationale", "meta"]
        vals = [kw.get(c) for c in cols]
        with _LOCK:
            cur = self._conn.execute(
                f"INSERT INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **kw) -> None:
        if not kw:
            return
        if "meta" in kw and not isinstance(kw["meta"], str):
            kw["meta"] = json.dumps(kw["meta"] or {})
        sets = ", ".join(f"{k}=?" for k in kw)
        with _LOCK:
            self._conn.execute(f"UPDATE trades SET {sets} WHERE id=?", [*kw.values(), trade_id])
            self._conn.commit()

    def open_trades(self, market: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM trades WHERE status='open'"
        params: list = []
        if market:
            q += " AND market=?"
            params.append(market)
        with _LOCK:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit: int = 50) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl(self) -> float:
        with _LOCK:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl),0) AS s FROM trades WHERE status IN ('won','lost','closed')"
            ).fetchone()
        return float(row["s"] or 0.0)

    def stats(self) -> dict:
        with _LOCK:
            row = self._conn.execute(
                "SELECT "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses, "
                "COUNT(*) AS total "
                "FROM trades WHERE status IN ('won','lost','closed')"
            ).fetchone()
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        total = int(row["total"] or 0)
        win_rate = (wins / total) if total else 0.0
        return {"wins": wins, "losses": losses, "total": total, "win_rate": round(win_rate, 4)}

    # --- equity curve ---------------------------------------------------
    def snapshot_equity(self, equity: float, realized: float, unrealized: float) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO equity(ts,equity,realized,unrealized) VALUES(?,?,?,?) "
                "ON CONFLICT(ts) DO NOTHING",
                (round(time.time(), 1), equity, realized, unrealized),
            )
            self._conn.commit()

    def equity_curve(self, limit: int = 500) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- predictions (for calibration) ---------------------------------
    def add_prediction(self, p_raw: float, outcome: int) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO predictions(ts,p_raw,outcome) VALUES(?,?,?)",
                (time.time(), p_raw, outcome),
            )
            self._conn.commit()

    def get_predictions(self, limit: int = 3000) -> list[tuple]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT p_raw, outcome FROM (SELECT * FROM predictions ORDER BY id DESC LIMIT ?) ",
                (limit,),
            ).fetchall()
        return [(float(r["p_raw"]), int(r["outcome"])) for r in rows]

    # --- Phase 2: market-data persistence (best-effort, never fatal) -----
    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def append_raw_market_event(self, *, ts_ms: int, source: str, venue: str,
                                event_type: str, market_id, asset_id, payload) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO raw_market_events"
                    "(ts_ms,source,event_type,venue,market_id,asset_id,payload_json,inserted_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (int(ts_ms), source, event_type, venue, market_id, asset_id,
                     json.dumps(payload, default=str), self._now_iso()),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001 — market-data writes are best-effort
            pass

    def append_orderbook_snapshot(self, *, ts_ms: int, venue: str, market_id: str,
                                  asset_id: str, bids, asks, best_bid=None,
                                  best_ask=None, spread=None, midpoint=None,
                                  tick_size=None) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO orderbook_snapshots"
                    "(ts_ms,venue,market_id,asset_id,bids_json,asks_json,best_bid,best_ask,"
                    "spread,midpoint,tick_size) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (int(ts_ms), venue, market_id, asset_id,
                     json.dumps(bids, default=str), json.dumps(asks, default=str),
                     best_bid, best_ask, spread, midpoint, tick_size),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def append_orderbook_delta(self, *, ts_ms: int, venue: str, market_id: str,
                               asset_id: str, side: str, price: str, size: str,
                               action: str, best_bid=None, best_ask=None) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO orderbook_deltas"
                    "(ts_ms,venue,market_id,asset_id,side,price,size,action,best_bid,best_ask)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (int(ts_ms), venue, market_id, asset_id, side, price, size,
                     action, best_bid, best_ask),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def append_market_event(self, *, ts_ms: int, venue: str, market_id: str,
                            asset_id, event_type: str, payload) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO market_events(ts_ms,venue,market_id,asset_id,event_type,payload_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (int(ts_ms), venue, market_id, asset_id, event_type,
                     json.dumps(payload, default=str)),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def upsert_market_data_health(self, *, source: str, status: str,
                                  last_message_ts_ms: int, reconnect_count: int,
                                  parse_errors: int, subscribed_asset_count: int,
                                  stale_asset_count: int) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO market_data_health"
                    "(source,status,last_message_ts_ms,reconnect_count,parse_errors,"
                    "subscribed_asset_count,stale_asset_count,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(source) DO UPDATE SET status=excluded.status,"
                    " last_message_ts_ms=excluded.last_message_ts_ms,"
                    " reconnect_count=excluded.reconnect_count, parse_errors=excluded.parse_errors,"
                    " subscribed_asset_count=excluded.subscribed_asset_count,"
                    " stale_asset_count=excluded.stale_asset_count, updated_at=excluded.updated_at",
                    (source, status, int(last_message_ts_ms or 0), int(reconnect_count or 0),
                     int(parse_errors or 0), int(subscribed_asset_count or 0),
                     int(stale_asset_count or 0), self._now_iso()),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_recent_raw_market_events(self, limit: int = 100) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT ts_ms,source,event_type,venue,market_id,asset_id,payload_json"
                    " FROM raw_market_events ORDER BY id DESC LIMIT ?", (int(limit),),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["payload"] = json.loads(d.pop("payload_json") or "{}")
                except (ValueError, TypeError):
                    d["payload"] = {}
                out.append(d)
            return out
        except Exception:  # noqa: BLE001
            return []

    def get_recent_market_events(self, limit: int = 100) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT ts_ms,venue,market_id,asset_id,event_type,payload_json"
                    " FROM market_events ORDER BY id DESC LIMIT ?", (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_market_event_count(self, market_id=None, event_type=None) -> int:
        try:
            q = "SELECT COUNT(*) AS c FROM market_events WHERE 1=1"
            params: list = []
            if market_id is not None:
                q += " AND market_id=?"
                params.append(market_id)
            if event_type is not None:
                q += " AND event_type=?"
                params.append(event_type)
            with _LOCK:
                row = self._conn.execute(q, params).fetchone()
            return int(row["c"] or 0)
        except Exception:  # noqa: BLE001
            return 0

    def get_market_data_health(self, source: str = "polymarket_clob") -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM market_data_health WHERE source=?", (source,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def prune_market_events(self, keep: int = 50000) -> None:
        try:
            with _LOCK:
                for tbl in ("raw_market_events", "market_events", "orderbook_deltas",
                            "orderbook_snapshots"):
                    self._conn.execute(
                        f"DELETE FROM {tbl} WHERE id NOT IN "
                        f"(SELECT id FROM {tbl} ORDER BY id DESC LIMIT ?)", (int(keep),))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    # --- Phase 3: OMS persistence -------------------------------------- #
    _ORDER_COLS = ("client_order_id", "broker_order_id", "venue", "market_id",
                   "asset_id", "outcome", "side", "order_type", "limit_price",
                   "quantity", "notional", "time_in_force", "status", "source",
                   "proposal_id", "venue_kind", "parent_client_order_id",
                   "risk_decision_json", "reject_reason", "created_ts_ms", "updated_ts_ms")

    def add_order(self, record: dict) -> bool:
        """Persist a new order intent. Returns False on failure (fail closed)."""
        try:
            vals = [record.get(c) for c in self._ORDER_COLS]
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO orders({','.join(self._ORDER_COLS)}) "
                    f"VALUES({','.join('?' * len(self._ORDER_COLS))})", vals)
                self._conn.commit()
            return True
        except Exception:  # noqa: BLE001 — duplicate id or write failure -> fail closed
            return False

    def update_order(self, client_order_id: str, **fields) -> None:
        if not fields:
            return
        try:
            sets = ", ".join(f"{k}=?" for k in fields)
            with _LOCK:
                self._conn.execute(
                    f"UPDATE orders SET {sets} WHERE client_order_id=?",
                    [*fields.values(), client_order_id])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_order(self, client_order_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_orders(self, status: str | None = None, limit: int = 200) -> list[dict]:
        try:
            q = "SELECT * FROM orders"
            params: list = []
            if status:
                q += " WHERE status=?"
                params.append(status)
            q += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_open_orders(self) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED','ACCEPTED') "
                    "ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_recent_orders(self, limit: int = 50) -> list[dict]:
        return self.get_orders(limit=limit)

    def add_fill(self, record: dict) -> bool:
        cols = ("fill_id", "client_order_id", "broker_order_id", "venue", "market_id",
                "asset_id", "side", "price", "quantity", "notional", "fee",
                "liquidity_flag", "ts_ms")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO fills({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [record.get(c) for c in cols])
                self._conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def get_fills(self, limit: int = 200) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_fills_for_order(self, client_order_id: str) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM fills WHERE client_order_id=? ORDER BY id ASC",
                    (client_order_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def upsert_position(self, record: dict) -> None:
        cols = ("venue", "market_id", "asset_id", "outcome", "quantity", "avg_price",
                "realized_pnl", "unrealized_pnl", "fees_paid", "updated_ts_ms")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO positions({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(venue, market_id, asset_id, outcome) DO UPDATE SET "
                    "quantity=excluded.quantity, avg_price=excluded.avg_price, "
                    "realized_pnl=excluded.realized_pnl, unrealized_pnl=excluded.unrealized_pnl, "
                    "fees_paid=excluded.fees_paid, updated_ts_ms=excluded.updated_ts_ms",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_positions(self) -> list[dict]:
        """Return ONE aggregated row per logical position
        (venue, market_id, asset_id, outcome), keeping the newest snapshot.

        SQLite treats NULL `asset_id`/`outcome` as DISTINCT, so the table's
        UNIQUE constraint does not collapse NULL-keyed rows (e.g. BTC-pulse
        positions) and duplicate snapshots accumulate. This dedupes them so the
        dashboard shows real, aggregated positions instead of repeated snapshots.
        """
        try:
            with _LOCK:
                rows = self._conn.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()
            seen = set()
            out = []
            for r in rows:  # id DESC => newest snapshot per key wins
                d = dict(r)
                key = (d.get("venue"), d.get("market_id"), d.get("asset_id") or "",
                       d.get("outcome") or "")
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
            return out
        except Exception:  # noqa: BLE001
            return []

    def position_snapshot_count(self) -> int:
        """Raw row count in the positions table (includes duplicate snapshots)."""
        try:
            with _LOCK:
                row = self._conn.execute("SELECT COUNT(*) FROM positions").fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def add_order_event(self, ts_ms: int, client_order_id: str, event_type: str, payload) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO order_events(ts_ms,client_order_id,event_type,payload_json) "
                    "VALUES(?,?,?,?)",
                    (int(ts_ms), client_order_id, event_type, json.dumps(payload, default=str)))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_order_events(self, client_order_id: str | None = None, limit: int = 200) -> list[dict]:
        try:
            q = "SELECT * FROM order_events"
            params: list = []
            if client_order_id:
                q += " WHERE client_order_id=?"
                params.append(client_order_id)
            q += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def add_reconciliation_event(self, ts_ms: int, severity: str, event_type: str, payload) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO reconciliation_events(ts_ms,severity,event_type,payload_json) "
                    "VALUES(?,?,?,?)",
                    (int(ts_ms), severity, event_type, json.dumps(payload, default=str)))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_reconciliation_events(self, limit: int = 100) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM reconciliation_events ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    # --- Phase 4: replay persistence (isolated by replay_run_id) ------- #
    def _insert(self, table: str, cols: tuple, record: dict) -> None:
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001 — replay writes are best-effort
            pass

    def upsert_replay_run(self, record: dict) -> None:
        cols = ("replay_run_id", "episode_id", "config_json", "config_hash", "seed",
                "started_at", "finished_at", "status", "venue", "market_ids_json",
                "asset_ids_json", "start_ts_ms", "end_ts_ms", "event_count", "notes")
        try:
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO replay_runs({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(replay_run_id) DO UPDATE SET "
                    "finished_at=excluded.finished_at, status=excluded.status, "
                    "event_count=excluded.event_count",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_replay_event_processed(self, record: dict) -> None:
        self._insert("replay_events_processed",
                     ("replay_run_id", "ts_ms", "sequence", "source_event_id", "venue",
                      "market_id", "asset_id", "event_type", "payload_hash"), record)

    def add_replay_proposal(self, record: dict) -> None:
        self._insert("replay_proposals",
                     ("replay_run_id", "ts_ms", "proposal_id", "policy_name", "venue",
                      "market_id", "asset_id", "side", "outcome", "fair_probability",
                      "confidence", "limit_price", "quantity", "notional",
                      "edge_after_costs", "payload_json"), record)

    def add_replay_risk_decision(self, record: dict) -> None:
        self._insert("replay_risk_decisions",
                     ("replay_run_id", "ts_ms", "proposal_id", "client_order_id",
                      "approved", "reason", "payload_json"), record)

    def add_replay_order(self, record: dict) -> None:
        self._insert("replay_orders",
                     ("replay_run_id", "client_order_id", "ts_ms", "venue", "market_id",
                      "asset_id", "side", "order_type", "limit_price", "quantity",
                      "notional", "status", "reject_reason", "payload_json"), record)

    def add_replay_fill(self, record: dict) -> None:
        self._insert("replay_fills",
                     ("replay_run_id", "fill_id", "client_order_id", "ts_ms", "venue",
                      "market_id", "asset_id", "side", "price", "quantity", "notional",
                      "fee", "liquidity_flag", "payload_json"), record)

    def add_replay_position(self, record: dict) -> None:
        self._insert("replay_positions",
                     ("replay_run_id", "ts_ms", "venue", "market_id", "asset_id", "outcome",
                      "quantity", "avg_price", "realized_pnl", "unrealized_pnl",
                      "fees_paid", "payload_json"), record)

    def add_replay_equity(self, record: dict) -> None:
        self._insert("replay_equity",
                     ("replay_run_id", "ts_ms", "cash", "equity", "realized_pnl",
                      "unrealized_pnl", "fees_paid", "drawdown", "exposure"), record)

    def add_replay_calibration(self, record: dict) -> None:
        self._insert("replay_calibration",
                     ("replay_run_id", "market_id", "asset_id", "outcome",
                      "predicted_probability", "confidence", "realized_outcome",
                      "bucket", "brier", "log_loss", "ts_ms"), record)

    def set_replay_metric(self, replay_run_id: str, name: str, value, json_value) -> None:
        import json as _json
        try:
            with _LOCK:
                self._conn.execute(
                    "INSERT INTO replay_metrics(replay_run_id,metric_name,metric_value,metric_json) "
                    "VALUES(?,?,?,?) ON CONFLICT(replay_run_id,metric_name) DO UPDATE SET "
                    "metric_value=excluded.metric_value, metric_json=excluded.metric_json",
                    (replay_run_id, name, None if value is None else str(value),
                     None if json_value is None else _json.dumps(json_value, default=str)))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def _replay_rows(self, table: str, replay_run_id: str, limit: int = 100000,
                     order: str = "id ASC") -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE replay_run_id=? ORDER BY {order} LIMIT ?",
                    (replay_run_id, int(limit))).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_replay_runs(self, limit: int = 50) -> list[dict]:
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_replay_run(self, replay_run_id: str) -> dict | None:
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT * FROM replay_runs WHERE replay_run_id=?", (replay_run_id,)).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_replay_metrics(self, replay_run_id: str) -> dict:
        import json as _json
        out = {}
        try:
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT metric_name, metric_value, metric_json FROM replay_metrics "
                    "WHERE replay_run_id=?", (replay_run_id,)).fetchall()
            for r in rows:
                if r["metric_json"] is not None:
                    try:
                        out[r["metric_name"]] = _json.loads(r["metric_json"])
                    except (ValueError, TypeError):
                        out[r["metric_name"]] = None
                else:
                    out[r["metric_name"]] = r["metric_value"]
        except Exception:  # noqa: BLE001
            pass
        return out

    def get_replay_equity(self, replay_run_id: str) -> list[dict]:
        return self._replay_rows("replay_equity", replay_run_id, order="id ASC")

    def get_replay_orders(self, replay_run_id: str) -> list[dict]:
        return self._replay_rows("replay_orders", replay_run_id)

    def get_replay_fills(self, replay_run_id: str) -> list[dict]:
        return self._replay_rows("replay_fills", replay_run_id)

    def get_replay_calibration(self, replay_run_id: str) -> list[dict]:
        return self._replay_rows("replay_calibration", replay_run_id)

    # --- market outcomes (for realized calibration) -------------------- #
    def upsert_market_outcome(self, record: dict) -> None:
        import json as _json
        cols = ("venue", "market_id", "asset_id", "outcome", "resolved_ts_ms",
                "realized_outcome", "payout_price", "source", "payload_json")
        try:
            payload = record.get("payload_json")
            if payload is not None and not isinstance(payload, str):
                record = {**record, "payload_json": _json.dumps(payload, default=str)}
            with _LOCK:
                self._conn.execute(
                    f"INSERT INTO market_outcomes({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))}) "
                    "ON CONFLICT(venue, market_id, asset_id, outcome) DO UPDATE SET "
                    "resolved_ts_ms=excluded.resolved_ts_ms, realized_outcome=excluded.realized_outcome, "
                    "payout_price=excluded.payout_price, source=excluded.source",
                    [record.get(c) for c in cols])
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_market_outcomes(self, venue: str | None = None) -> list[dict]:
        try:
            q = "SELECT * FROM market_outcomes"
            params: list = []
            if venue:
                q += " WHERE venue=?"
                params.append(venue)
            with _LOCK:
                rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []
