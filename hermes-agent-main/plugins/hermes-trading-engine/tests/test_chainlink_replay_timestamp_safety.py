"""Chainlink replay timestamp-safety + analytics (TDD, deterministic, offline).

Quant scope: Data Acquisition & Ingestion + Backtesting & Simulation. The replay
Chainlink source must never return future data (cursor-capped), and the replay
analytics report snapshot freshness, matched markets, stale rejections, oracle
deviation, and probability impact.
"""

from __future__ import annotations

from engine.chainlink_scanner import ChainlinkScanner
from engine.feeds.chainlink import (ChainlinkReading, ReplayChainlinkSource,
                                     StaticChainlinkSource)
from engine.feeds.chainlink_registry import load_registry
from engine.replay.metrics import chainlink_replay_analytics

_ETH = "ETH/USD"


def _r(value, ts):
    return ChainlinkReading(_ETH, int(value * 1e8), 8, ts, 1, ts)


def _scanner(readings):
    return ChainlinkScanner(StaticChainlinkSource({_ETH: readings}),
                            registry={_ETH: load_registry()[_ETH]})


def test_replay_source_never_returns_future_data():
    readings = [_r(3000, 100), _r(3100, 200), _r(3300, 300)]
    src = ReplayChainlinkSource(readings, cursor=200)
    spec = load_registry()[_ETH]
    assert src.read(spec).value == 3100                 # cursor caps at t=200
    assert all(x.observed_ts <= 200 for x in src.history(_ETH))
    assert src.read(spec, now=999).value == 3100        # cursor wins over a later 'now'


def test_chainlink_replay_analytics_fresh_link():
    now = 10_000.0
    sc = _scanner([_r(3000 + i, now - 480 + i * 60) for i in range(8)])
    sc.scan(now=now)
    market = {"id": "m", "question": "Will ETH be above $2500?", "category": "crypto",
              "slug": "eth-above-2500"}
    sig = sc.signal_for_market(market, p_base=0.5, now=now)
    a = chainlink_replay_analytics([sig.to_dict()])
    assert a["signal_count"] == 1
    assert a["matched_market_count"] == 1
    assert a["stale_rejection_count"] == 0
    assert a["snapshot_freshness"] > 0.0
    assert a["probability_impact"] >= 0.0


def test_chainlink_replay_analytics_counts_stale_rejections():
    now = 1_000_000.0
    sc = _scanner([_r(3000, now - 4 * 3600)])           # 4h old -> stale
    market = {"id": "m", "question": "Will ETH be above $4000?", "category": "crypto",
              "slug": "eth-above-4000"}
    sig = sc.signal_for_market(market, p_base=0.5, now=now)
    a = chainlink_replay_analytics([sig.to_dict()])
    assert a["stale_rejection_count"] == 1
    assert sig.no_trade is True                          # stale oracle blocks


def test_chainlink_replay_analytics_empty():
    a = chainlink_replay_analytics([])
    assert a["signal_count"] == 0 and a["matched_market_count"] == 0
    assert a["snapshot_freshness"] == 0.0
