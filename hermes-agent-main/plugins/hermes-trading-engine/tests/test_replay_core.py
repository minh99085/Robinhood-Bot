"""Replay loader, clock, config-hash, and fail-closed behavior."""

from __future__ import annotations

import time
from pathlib import Path

from engine.replay import ReplayClock, ReplayConfig, ReplayEventLoader, ReplayRunner
from engine.storage import Store

FIXTURE = Path(__file__).parent / "fixtures" / "sample_polymarket_replay.jsonl"


def _write(tmp_path, lines) -> str:
    p = tmp_path / "ev.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_replay_loader_sorts_events_deterministically(tmp_path):
    lines = [
        '{"ts_ms": 100, "event_type": "book", "venue": "polymarket", "market": "m", "asset_id": "a", "sequence": 2}',
        '{"ts_ms": 100, "event_type": "book", "venue": "polymarket", "market": "m", "asset_id": "a", "sequence": 0}',
        '{"ts_ms": 100, "event_type": "book", "venue": "polymarket", "market": "m", "asset_id": "a", "sequence": 1}',
    ]
    path = _write(tmp_path, lines)
    out1 = ReplayEventLoader().from_jsonl(path)
    out2 = ReplayEventLoader().from_jsonl(path)
    assert [e.sequence for e in out1] == [0, 1, 2]
    assert [e.sequence for e in out1] == [e.sequence for e in out2]  # stable


def test_replay_loader_filters_by_market_and_time(tmp_path):
    lines = [
        '{"ts_ms": 100, "event_type": "book", "venue": "polymarket", "market": "m1", "asset_id": "a1"}',
        '{"ts_ms": 200, "event_type": "book", "venue": "polymarket", "market": "m2", "asset_id": "a2"}',
        '{"ts_ms": 300, "event_type": "book", "venue": "polymarket", "market": "m1", "asset_id": "a1"}',
    ]
    path = _write(tmp_path, lines)
    out = ReplayEventLoader().from_jsonl(path, market_ids=["m1"], start_ts_ms=150)
    assert len(out) == 1
    assert out[0].market_id == "m1" and out[0].ts_ms == 300


def test_replay_clock_advances_without_sleep():
    clk = ReplayClock(speed_multiplier=0.0)
    t0 = time.time()
    for ts in (1_700_000_000_000, 1_700_000_001_000, 1_700_000_005_000):
        clk.advance_to(ts)
        assert clk.now_ms() == ts
    assert (time.time() - t0) < 0.5  # no real waiting despite huge sim jumps


def test_replay_config_hash_stable():
    c1 = ReplayConfig(policy_name="simple_edge", seed=42, initial_cash=10000)
    c2 = ReplayConfig(policy_name="simple_edge", seed=42, initial_cash=10000)
    assert c1.config_hash() == c2.config_hash()
    # replay_run_id must not affect the hash (reproducibility)
    c1.replay_run_id = "rp-aaa"
    c2.replay_run_id = "rp-bbb"
    assert c1.config_hash() == c2.config_hash()
    # a real config change changes the hash
    c3 = ReplayConfig(policy_name="simple_edge", seed=43, initial_cash=10000)
    assert c3.config_hash() != c1.config_hash()


def test_replay_fail_closed_when_no_events(tmp_path):
    runner = ReplayRunner(ReplayConfig(policy_name="noop"), Store(tmp_path / "op.sqlite3"), [])
    report = runner.run()
    assert report["status"] == "failed"
    assert report["error"] == "no_events"
