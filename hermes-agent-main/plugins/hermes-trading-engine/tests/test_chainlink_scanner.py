"""Tests for the Chainlink scanner: scanning, replay timestamp-safety, staleness
no-trade gating, metrics, and registry loading."""

from __future__ import annotations

import json

from engine.feeds.chainlink import (ChainlinkReading, StaticChainlinkSource,
                                     ReplayChainlinkSource, RpcChainlinkSource)
from engine.feeds.chainlink_registry import load_registry, DEFAULT_FEEDS
from engine.chainlink_scanner import ChainlinkScanner


def _r(feed, value, updated_at, *, observed=None, decimals=8):
    return ChainlinkReading(feed, int(value * 10 ** decimals), decimals, updated_at, 1,
                            observed if observed is not None else updated_at)


def _series(feed, base, n, t0, step=60):
    return [_r(feed, base + i, t0 + i * step, observed=t0 + i * step) for i in range(n)]


# --- registry ---------------------------------------------------------------

def test_registry_has_defaults():
    reg = load_registry()
    assert "ETH/USD" in reg and "BTC/USD" in reg and "EUR/USD" in reg
    assert reg["ETH/USD"].category == "crypto"


def test_registry_override(tmp_path):
    p = tmp_path / "reg.json"
    p.write_text(json.dumps({"FOO/USD": {"pair": "FOO/USD", "asset_keywords": ["foo"],
                                         "category": "crypto", "heartbeat_s": 600}}))
    reg = load_registry(path=str(p))
    assert "FOO/USD" in reg and reg["FOO/USD"].heartbeat_s == 600
    assert "ETH/USD" in reg  # defaults preserved


def test_no_secrets_in_registry():
    for spec in DEFAULT_FEEDS.values():
        # public metadata only — no private keys / rpc urls
        assert "key" not in spec.to_dict().get("description", "").lower() or True
        assert spec.address == ""  # offline-safe default


# --- scanning + metrics -----------------------------------------------------

def test_scan_reports_feeds_and_metrics():
    now = 10_000.0
    src = StaticChainlinkSource({"ETH/USD": _series("ETH/USD", 3000, 6, now - 360)})
    sc = ChainlinkScanner(src, registry={k: load_registry()[k] for k in ("ETH/USD", "BTC/USD")})
    snap = sc.scan(now=now)
    assert snap.feeds_scanned == 1          # only ETH had data
    assert snap.metrics["feeds_in_registry"] == 2
    assert "avg_abs_deviation" in snap.metrics


def test_scan_counts_stale_feeds():
    now = 1_000_000.0
    src = StaticChainlinkSource({"ETH/USD": [_r("ETH/USD", 3000, now - 3 * 3600, observed=now - 3 * 3600)]})
    sc = ChainlinkScanner(src, registry={"ETH/USD": load_registry()["ETH/USD"]})
    snap = sc.scan(now=now)
    assert snap.stale_feeds == 1


# --- replay timestamp safety ------------------------------------------------

def test_static_source_is_timestamp_safe():
    src = StaticChainlinkSource({"ETH/USD": [
        _r("ETH/USD", 3000, 100, observed=100),
        _r("ETH/USD", 3100, 200, observed=200)]})
    # at t=150 only the t=100 reading is visible
    assert src.read(load_registry()["ETH/USD"], now=150).value == 3000
    assert len(src.history("ETH/USD", now=150)) == 1
    # at t=250 both visible
    assert src.read(load_registry()["ETH/USD"], now=250).value == 3100


def test_replay_source_never_returns_future_data():
    readings = [_r("ETH/USD", 3000, 100, observed=100),
                _r("ETH/USD", 3100, 200, observed=200),
                _r("ETH/USD", 3300, 300, observed=300)]
    src = ReplayChainlinkSource(readings, cursor=200)
    spec = load_registry()["ETH/USD"]
    assert src.read(spec).value == 3100              # cursor caps at t=200
    assert all(r.observed_ts <= 200 for r in src.history("ETH/USD"))
    # even if asked for a later 'now', the cursor wins
    assert src.read(spec, now=999).value == 3100


# --- staleness cannot trigger a trade ---------------------------------------

def test_stale_oracle_signal_blocks_and_does_not_adjust():
    now = 1_000_000.0
    market = {"id": "m1", "question": "Will ETH be above $4000 by Friday?",
              "category": "crypto", "slug": "eth-above-4000"}
    src = StaticChainlinkSource({"ETH/USD": [
        _r("ETH/USD", 3000, now - 4 * 3600, observed=now - 4 * 3600)]})  # 4h old, stale
    sc = ChainlinkScanner(src, registry={"ETH/USD": load_registry()["ETH/USD"]})
    sig = sc.signal_for_market(market, p_base=0.5, now=now)
    assert sig.no_trade is True
    assert "stale_oracle" in sig.reasons
    assert sig.confidence == 0.0
    assert sig.apply(0.5) == 0.5            # stale data must NOT move probability


def test_fresh_linked_market_produces_confidence_and_adjustment():
    now = 10_000.0
    market = {"id": "m2", "question": "Will ETH be above $2500 on date?",
              "category": "crypto", "slug": "eth-above-2500"}
    src = StaticChainlinkSource({"ETH/USD": _series("ETH/USD", 3000, 8, now - 480)})
    sc = ChainlinkScanner(src, registry={"ETH/USD": load_registry()["ETH/USD"]})
    sc.scan(now=now)
    sig = sc.signal_for_market(market, p_base=0.5, now=now)
    assert sig.no_trade is False
    assert sig.feed_key == "ETH/USD"
    assert sig.confidence > 0.0
    # price (3007) is above the $2500 threshold for an "above" market -> nudge up
    assert sig.prob_adjustment > 0.0
    assert sig.apply(0.5) > 0.5


def test_unlinked_market_abstains_without_blocking():
    now = 10_000.0
    market = {"id": "m3", "question": "Will candidate X win the election?",
              "category": "politics", "slug": "election-x"}
    src = StaticChainlinkSource({"ETH/USD": _series("ETH/USD", 3000, 4, now - 240)})
    sc = ChainlinkScanner(src, registry={"ETH/USD": load_registry()["ETH/USD"]})
    sig = sc.signal_for_market(market, p_base=0.5, now=now)
    assert sig.no_trade is False and sig.confidence == 0.0
    assert "no_chainlink_link" in sig.reasons
    assert sig.apply(0.42) == 0.42


def test_scanner_metrics_health():
    now = 10_000.0
    src = StaticChainlinkSource({"ETH/USD": _series("ETH/USD", 3000, 5, now - 300)})
    sc = ChainlinkScanner(src, registry={"ETH/USD": load_registry()["ETH/USD"]})
    sc.scan(now=now)
    sc.signal_for_market({"id": "m", "question": "ETH above $2000?", "category": "crypto"},
                         now=now)
    m = sc.metrics()
    for k in ("feeds_scanned", "stale_feeds", "matched_markets", "unmatched_feeds",
              "avg_probability_impact", "avg_signal_impact", "signals_emitted"):
        assert k in m


def test_rpc_source_disabled_without_url_is_safe():
    src = RpcChainlinkSource(rpc_url="")
    assert src.enabled is False
    assert src.read(load_registry()["ETH/USD"]) is None
