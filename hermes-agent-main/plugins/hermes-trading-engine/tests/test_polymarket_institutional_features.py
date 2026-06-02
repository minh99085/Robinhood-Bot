"""Institutional feature-engineering tests (offline, deterministic).

Covers: time-to-resolution, spread/depth quality, depth-weighted microprice,
order-book imbalance, liquidity velocity, volume acceleration, quote
persistence, stale-book score, market entropy, resolution ambiguity, event
correlation, and Chainlink relevance — plus null-rate / coverage accounting.
"""

from __future__ import annotations

import time

import pytest

from engine.markets import universe_manager as um
from engine.training.institutional_features import (
    FEATURE_FIELDS, InstitutionalFeatures, binary_entropy, compute_features,
    feature_coverage)
from tests._pmtrain_helpers import clean_live_env, market


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def _rec(**raw_over):
    now = time.time()
    raw = market(0, now=now)
    raw.update(raw_over)
    return um.MarketRecord.from_raw(raw, now=now), now


def test_feature_vector_populated_and_in_range():
    rec, now = _rec()
    f = compute_features(rec, now=now)
    assert isinstance(f, InstitutionalFeatures)
    # core features derivable from a normal market are present and bounded
    assert f.spread_quality is not None and 0.0 <= f.spread_quality <= 1.0
    assert f.top_depth_quality is not None and 0.0 <= f.top_depth_quality <= 1.0
    assert f.market_entropy is not None and 0.0 <= f.market_entropy <= 1.0
    assert f.time_to_resolution_s is not None and f.time_to_resolution_s > 0
    assert 0.0 <= f.time_to_resolution_score <= 1.0
    assert f.resolution_ambiguity is not None


def test_microprice_and_imbalance_from_sized_book():
    rec, now = _rec(bids=[[0.49, 100], [0.48, 50]],
                    asks=[[0.51, 300], [0.52, 100]])
    f = compute_features(rec, now=now)
    assert f.depth_weighted_microprice is not None
    # microprice sits between best bid and best ask
    assert 0.49 <= f.depth_weighted_microprice <= 0.51
    # more size on the ask -> negative (ask-heavy) imbalance
    assert f.order_book_imbalance is not None and f.order_book_imbalance < 0


def test_microprice_none_without_sized_book():
    rec, now = _rec()  # market() sets bestBid/bestAsk but NO sizes/levels
    f = compute_features(rec, now=now)
    assert f.depth_weighted_microprice is None
    assert f.order_book_imbalance is None
    assert "no_sized_book" in f.notes


def test_binary_entropy_peaks_at_half():
    assert binary_entropy(0.5) == pytest.approx(1.0, abs=1e-9)
    assert binary_entropy(0.0) == 0.0
    assert binary_entropy(1.0) == 0.0
    assert binary_entropy(0.9) < binary_entropy(0.5)
    assert binary_entropy(None) is None


def test_stale_book_score_increases_with_age():
    now = time.time()
    fresh = market(1, now=now)
    fresh["bookUpdatedTs"] = now
    old = market(2, now=now)
    old["bookUpdatedTs"] = now - 120.0
    rf = um.MarketRecord.from_raw(fresh, now=now)
    ro = um.MarketRecord.from_raw(old, now=now)
    ff = compute_features(rf, now=now)
    fo = compute_features(ro, now=now)
    assert ff.stale_book_score is not None and fo.stale_book_score is not None
    assert fo.stale_book_score > ff.stale_book_score
    assert fo.stale_book_score == pytest.approx(1.0)


def test_stale_score_none_when_book_age_unknown():
    now = time.time()
    raw = market(3, fresh=False, now=now)  # no bookUpdatedTs -> book_age None
    rec = um.MarketRecord.from_raw(raw, now=now)
    f = compute_features(rec, now=now)
    assert f.stale_book_score is None


def test_dynamics_from_history():
    rec, now = _rec()
    rec.liquidity_usd = 22000.0
    rec.volume_total_usd = 60000.0
    rec.yes_price = 0.30
    history = [
        {"ts": now - 120, "liquidity_usd": 20000.0, "volume_total_usd": 40000.0, "yes_price": 0.29},
        {"ts": now - 60, "liquidity_usd": 21000.0, "volume_total_usd": 50000.0, "yes_price": 0.30},
    ]
    f = compute_features(rec, history=history, now=now)
    assert f.liquidity_velocity is not None and f.liquidity_velocity > 0
    assert f.volume_acceleration is not None
    assert f.quote_persistence is not None and 0.0 <= f.quote_persistence <= 1.0


def test_dynamics_none_without_history():
    rec, now = _rec()
    f = compute_features(rec, now=now)
    assert f.liquidity_velocity is None
    assert f.volume_acceleration is None
    assert f.quote_persistence is None


def test_event_correlation_scales_with_group_size():
    rec, now = _rec()
    solo = compute_features(rec, group_size=1, now=now)
    grouped = compute_features(rec, group_size=4, now=now)
    assert solo.event_correlation == 0.0
    assert grouped.event_correlation > solo.event_correlation


def test_resolution_ambiguity_uses_raw_then_fallback():
    rec_amb, now = _rec(ambiguity=0.8)
    assert compute_features(rec_amb, now=now).resolution_ambiguity == pytest.approx(0.8)
    # no description/rules -> fallback ambiguity of 0.5
    raw = market(5, desc=False, now=now)
    rec = um.MarketRecord.from_raw(raw, now=now)
    assert compute_features(rec, now=now).resolution_ambiguity == 0.5


def test_chainlink_relevance_optional():
    rec, now = _rec()
    assert compute_features(rec, now=now).chainlink_relevance is None
    f = compute_features(rec, chainlink_relevance=0.7, now=now)
    assert f.chainlink_relevance == pytest.approx(0.7)


def test_feature_coverage_accounting():
    now = time.time()
    full, _ = _rec(bids=[[0.49, 100]], asks=[[0.51, 100]])
    full.yes_price = 0.5
    rich = compute_features(full, chainlink_relevance=0.5,
                            history=[{"ts": now - 1, "liquidity_usd": 1.0,
                                      "volume_total_usd": 1.0, "yes_price": 0.5},
                                     {"ts": now, "liquidity_usd": 2.0,
                                      "volume_total_usd": 2.0, "yes_price": 0.5}],
                            group_size=2, now=now)
    sparse, _ = _rec()
    sparse_f = compute_features(sparse, now=now)
    cov = feature_coverage([rich, sparse_f])
    assert cov["n"] == 2
    assert 0.0 <= cov["null_rate"] <= 1.0
    assert cov["coverage"] == pytest.approx(1.0 - cov["null_rate"], abs=1e-6)
    # microprice is populated for the rich vector but null for the sparse one
    assert cov["per_field"]["depth_weighted_microprice"] == pytest.approx(0.5)
    assert set(cov["per_field"]) == set(FEATURE_FIELDS)


def test_features_never_raise_on_garbage():
    from types import SimpleNamespace
    junk = SimpleNamespace(market_id="x", raw={"bids": "nonsense", "asks": None})
    f = compute_features(junk)
    assert f.market_id == "x"
    # everything degrades to null rather than raising
    assert f.depth_weighted_microprice is None
