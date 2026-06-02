"""Deterministic tests for Chainlink feature engineering."""

from __future__ import annotations

from engine.feeds.chainlink import ChainlinkReading
from engine.features_chainlink import compute_features


def _r(feed, value, updated_at, *, decimals=8, observed=None, round_id=1):
    return ChainlinkReading(
        feed_key=feed, answer_raw=int(round(value * 10 ** decimals)), decimals=decimals,
        updated_at=updated_at, round_id=round_id,
        observed_ts=observed if observed is not None else updated_at)


def test_reading_value_and_age():
    r = _r("ETH/USD", 3000.0, updated_at=1000.0)
    assert abs(r.value - 3000.0) < 1e-6
    assert r.age_s(now=1100.0) == 100.0
    assert r.is_stale(now=1100.0, heartbeat_s=3600.0) is False


def test_compute_features_basic_trend_and_momentum():
    now = 10_000.0
    readings = [_r("ETH/USD", v, updated_at=now - (5 - i) * 60)
                for i, v in enumerate([3000, 3010, 3025, 3040, 3060])]
    f = compute_features(readings, now=now, heartbeat_s=3600.0)
    assert f is not None
    assert f.value == 3060
    assert f.n_samples == 5
    assert f.trend == "up" and f.momentum > 0
    assert f.volatility >= 0.0
    assert f.freshness > 0.9 and f.stale is False


def test_compute_features_empty_returns_none():
    assert compute_features([], now=1.0, heartbeat_s=3600.0) is None


def test_compute_features_single_sample_is_safe():
    f = compute_features([_r("BTC/USD", 60000.0, updated_at=100.0)], now=130.0,
                         heartbeat_s=3600.0)
    assert f is not None
    assert f.volatility == 0.0 and f.momentum == 0.0 and f.deviation == 0.0


def test_staleness_and_freshness():
    # last update is 2 heartbeats old -> stale, freshness 0
    now = 100_000.0
    readings = [_r("XAU/USD", 2000.0, updated_at=now - 2 * 86400)]
    f = compute_features(readings, now=now, heartbeat_s=86400.0)
    assert f.stale is True
    assert f.freshness == 0.0


def test_inconsistent_flag_on_large_jump():
    now = 5000.0
    base = [_r("ETH/USD", v, updated_at=now - (10 - i) * 60)
            for i, v in enumerate([3000, 3001, 2999, 3000, 3002, 2998, 3001, 3000, 2999])]
    base.append(_r("ETH/USD", 9000.0, updated_at=now))  # 3x jump -> inconsistent
    f = compute_features(base, now=now, heartbeat_s=3600.0)
    assert f.inconsistent is True


def test_heartbeat_gap_normalized():
    now = 50_000.0
    readings = [_r("EUR/USD", 1.08, updated_at=now - 7200),
                _r("EUR/USD", 1.081, updated_at=now)]  # 2h gap vs 1h heartbeat
    f = compute_features(readings, now=now, heartbeat_s=3600.0)
    assert f.heartbeat_gap >= 1.9
