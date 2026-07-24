"""The daily-loss halt must be fed by real (paper) settlements — not dead code.

Ground truth: Monte-Carlo-Sim settles paper trades into trade_log.jsonl;
the bridge ingests each settled TRADE exactly once, converts its
realized_pnl_pct into dollars on the bridge's own planned notional, and
feeds RobinhoodSafetyGates.record_realized_pnl. When accumulated losses
cross RH_DAILY_LOSS_LIMIT_USD, the gate blocks new orders — in the very
same bridge pass, and after a restart.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.mc_bridge import (
    LEDGER_FILENAME,
    ingest_settlements,
    process_once,
)
from engine.robinhood.mc_bridge import BridgeState
from engine.robinhood.safety_gates import RobinhoodSafetyGates


def make_config(tmp_path: Path, **overrides) -> RobinhoodConfig:
    cfg = RobinhoodConfig.from_env()
    return replace(
        cfg,
        data_dir=str(tmp_path / "data"),
        live_trading_enabled=False,
        max_order_notional_usd=overrides.pop("max_order_notional_usd", 1000.0),
        daily_loss_limit_usd=overrides.pop("daily_loss_limit_usd", 200.0),
        **overrides,
    )


def settled_entry(*, ticker="AAPL", s0=100.0, shares=5,
                  pnl_pct=-0.5, ts=None) -> dict:
    ts = ts or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "timestamp_utc": ts,
        "ticker": ticker,
        "verdict": "TRADE",
        "side": "long",
        "horizon_days": 5,
        "s0": s0,
        "sizing": {"shares": shares},
        "settled": True,
        "settlement": {
            "realized_pnl_pct": pnl_pct,
            "exit_reason": "stop_loss",
        },
    }


def write_trade_log(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def fresh_verdict(ticker="MSFT") -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "timestamp_utc": now.isoformat(),
        "engine": "meta_label_v2",
        "ticker": ticker,
        "verdict": "TRADE",
        "side": "long",
        "horizon_days": 5,
        "s0": 100.0,
        "sizing": {"shares": 3},
    }


def test_settled_loss_moves_the_accumulator_and_trips_the_gate(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    # One settled paper trade: 5 shares @ $100, -50% → -$250 < -$200 limit.
    write_trade_log(outputs / "trade_log.jsonl", [settled_entry()])
    verdicts = outputs / "verdicts"
    vpath = verdicts / "20990101T000000Z_MSFT.json"
    verdicts.mkdir(parents=True)
    vpath.write_text(json.dumps(fresh_verdict()), encoding="utf-8")

    summary = process_once([verdicts], cfg)

    # Settlement ingested, and the SAME pass gate-blocked the new order.
    assert summary["ingested"] == 1
    assert summary["gate_blocked"] == 1
    ledger = [json.loads(l) for l in
              (Path(cfg.data_dir) / LEDGER_FILENAME).read_text().splitlines()]
    settle_rows = [r for r in ledger if r.get("type") == "paper_settlement"]
    assert len(settle_rows) == 1
    assert settle_rows[0]["pnl_usd"] == -250.0
    verdict_rows = [r for r in ledger if r.get("verdict_id")]
    assert verdict_rows[0]["gate_reason"] == "daily_loss_limit_reached"


def test_settlement_ingested_exactly_once(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    log = write_trade_log(outputs / "trade_log.jsonl",
                          [settled_entry(pnl_pct=-0.1)])  # -$50
    (outputs / "verdicts").mkdir(parents=True)

    s1 = process_once([outputs / "verdicts"], cfg)
    s2 = process_once([outputs / "verdicts"], cfg)
    assert s1["ingested"] == 1
    assert s2["ingested"] == 0  # never double-counted

    state = json.loads(
        (Path(cfg.data_dir) / "safety_state.json").read_text())
    assert state["daily_pnl"]["usd"] == -50.0  # once, not twice
    assert log.is_file()


def test_halt_survives_restart(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    write_trade_log(outputs / "trade_log.jsonl",
                    [settled_entry(pnl_pct=-0.5)])  # -$250
    (outputs / "verdicts").mkdir(parents=True)
    process_once([outputs / "verdicts"], cfg)

    # A brand-new gates instance (container restart) must still be halted.
    fresh_gates = RobinhoodSafetyGates(cfg, AuditLog(cfg.data_dir))
    verdict = fresh_gates.evaluate(
        "review_equity_order",
        {"symbol": "MSFT", "side": "buy", "quantity": 1, "limit_price": 100.0},
    )
    assert not verdict.allowed
    assert verdict.reason == "daily_loss_limit_reached"


def test_profit_does_not_trip_and_unsizable_is_skipped_once(tmp_path):
    cfg = make_config(tmp_path)
    audit = AuditLog(cfg.data_dir)
    gates = RobinhoodSafetyGates(cfg, audit)
    state = BridgeState.load(cfg.data_dir)
    outputs = tmp_path / "outputs"
    log = write_trade_log(outputs / "trade_log.jsonl", [
        settled_entry(ticker="NVDA", pnl_pct=0.2),          # +$100
        settled_entry(ticker="BRK", s0=0.0, shares=0),      # unsizable
    ])

    s = ingest_settlements(log, cfg, gates=gates, state=state, audit=audit)
    assert s["ingested"] == 1
    assert s["unsizable"] == 1
    ok = gates.evaluate(
        "review_equity_order",
        {"symbol": "MSFT", "side": "buy", "quantity": 1, "limit_price": 100.0},
    )
    assert ok.allowed

    # Second pass: both already tracked.
    s2 = ingest_settlements(log, cfg, gates=gates, state=state, audit=audit)
    assert s2["ingested"] == 0 and s2["unsizable"] == 0
