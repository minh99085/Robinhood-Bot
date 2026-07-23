"""Verdicts without a gauntlet-pass marker are paper-only, never executable.

The marker gates *execution eligibility* (any future phase-2 real-order
path), NOT paper logging — an unmarked verdict still flows through the
paper ledger exactly as before, it just can never become an order.
Also covers the dir-qualified verdict identity (verdicts/ vs
paper_verdicts/ no longer collide) with the legacy bare-filename fallback.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.mc_bridge import (
    LEDGER_FILENAME,
    BridgeState,
    execution_allowed,
    process_once,
    verdict_id,
)


def make_config(tmp_path: Path) -> RobinhoodConfig:
    cfg = RobinhoodConfig.from_env()
    return replace(
        cfg,
        data_dir=str(tmp_path / "data"),
        live_trading_enabled=False,
        max_order_notional_usd=1000.0,
    )


def make_verdict(ticker="AAPL", **overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    v = {
        "timestamp_utc": now.isoformat(),
        "ticker": ticker,
        "verdict": "TRADE",
        "side": "long",
        "horizon_days": 5,
        "s0": 100.0,
        "sizing": {"shares": 3},
    }
    v.update(overrides)
    return v


def _ledger_rows(cfg) -> list[dict]:
    path = Path(cfg.data_dir) / LEDGER_FILENAME
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_execution_allowed_requires_explicit_marker():
    ok, _ = execution_allowed(make_verdict(gauntlet_pass=True))
    assert ok
    for bad in (make_verdict(),                      # missing
                make_verdict(gauntlet_pass=False),   # explicit false
                make_verdict(gauntlet_pass="yes")):  # wrong type
        allowed, reason = execution_allowed(bad)
        assert not allowed
        assert "paper-only" in reason


def test_unmarked_verdict_is_paper_logged_but_ineligible(tmp_path):
    cfg = make_config(tmp_path)
    vdir = tmp_path / "outputs" / "verdicts"
    vdir.mkdir(parents=True)
    (vdir / "a_AAPL.json").write_text(json.dumps(make_verdict()))
    (vdir / "b_NVDA.json").write_text(
        json.dumps(make_verdict(ticker="NVDA", gauntlet_pass=True)))

    summary = process_once([vdir], cfg)
    assert summary["planned"] == 2  # paper logging unaffected by the marker

    rows = {r["ticker"]: r for r in _ledger_rows(cfg) if r.get("verdict_id")}
    assert rows["AAPL"]["execution_eligible"] is False
    assert "paper-only" in rows["AAPL"]["eligibility_reason"]
    assert rows["NVDA"]["execution_eligible"] is True


def test_same_filename_in_both_dirs_is_two_verdicts(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    d1 = outputs / "verdicts"
    d2 = outputs / "paper_verdicts"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    name = "20990101T000000Z_AAPL.json"
    (d1 / name).write_text(json.dumps(make_verdict()))
    (d2 / name).write_text(json.dumps(make_verdict()))

    assert verdict_id(d1 / name) != verdict_id(d2 / name)
    summary = process_once([d1, d2], cfg)
    assert summary["new"] == 2  # both processed, no collision


def test_legacy_bare_filename_state_not_reprocessed(tmp_path):
    cfg = make_config(tmp_path)
    vdir = tmp_path / "outputs" / "verdicts"
    vdir.mkdir(parents=True)
    name = "20990101T000000Z_AAPL.json"
    (vdir / name).write_text(json.dumps(make_verdict()))

    # Simulate pre-upgrade state that recorded the bare filename.
    state = BridgeState.load(cfg.data_dir)
    state.mark(name, "paper_planned")
    state.save()

    summary = process_once([vdir], cfg)
    assert summary["new"] == 0  # honoured, not double-processed
