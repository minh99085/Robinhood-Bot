"""Scanner feature-quality + ranking tests (offline, deterministic).

Asserts the scanner reports feature null-rate / coverage, stale-book rate and
group coverage; that ranking surfaces deeper / tighter / grouped markets; that
normalization caching avoids re-parsing unchanged markets; and that
institutional features (microprice / imbalance) are attached to the shortlist.
"""

from __future__ import annotations

import time

import pytest

from engine.training import TrainingConfig, MarketScanner
from tests._pmtrain_helpers import clean_live_env, catalog, market


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def test_scan_reports_feature_and_group_quality():
    sc = MarketScanner(TrainingConfig(shortlist_limit=20))
    res = sc.scan(catalog(15))
    assert res.shortlisted > 0
    assert 0.0 < res.feature_coverage <= 1.0
    assert 0.0 <= res.null_rate <= 1.0
    assert 0.0 <= res.stale_rate <= 1.0
    assert res.group_coverage > 0.0
    assert len(res.features) == res.shortlisted
    d = sc.metrics.to_dict()
    for key in ("feature_coverage", "null_rate", "stale_rate", "group_coverage",
                "groups_detected", "markets_per_second", "norm_cache_hits"):
        assert key in d


def test_ranking_surfaces_tighter_deeper_market_first():
    tight = market(0, bid=0.49, ask=0.51, liq=20000, depth=5000)   # spread 0.02, deep
    wide = market(1, bid=0.30, ask=0.60, liq=20000, depth=50)      # spread 0.30, thin
    res = MarketScanner(TrainingConfig()).scan([wide, tight])
    assert res.shortlist[0]["record"].market_id == "m0"


def test_grouped_market_gets_bregman_bonus_over_incomplete():
    now = time.time()
    grouped = []
    for i in range(3):
        raw = market(i, bid=0.32, ask=0.34, group="evt", now=now)
        grouped.append(raw)
    lone = market(50, bid=0.32, ask=0.34, now=now)
    lone["clobTokenIds"] = ["only-one"]  # 1 token -> same_event, incomplete
    res = MarketScanner(TrainingConfig()).scan(grouped + [lone], now=now)
    comp = {d["record"].market_id: d["components"] for d in res.shortlist}
    # every shortlist entry exposes the component for auditability
    assert all("bregman_suitability" in c for c in comp.values())
    grouped_bonus = comp["m0"]["bregman_suitability"]
    lone_bonus = comp["m50"]["bregman_suitability"]
    assert grouped_bonus > lone_bonus


def test_normalization_cache_avoids_reparsing():
    sc = MarketScanner(TrainingConfig())
    cat = catalog(10)
    first = sc.scan(cat)
    assert first.cache_hits == 0          # cold cache
    second = sc.scan(cat)                 # identical catalog -> all hits
    assert second.cache_hits == second.kept > 0
    assert sc.metrics.norm_cache_hits == second.cache_hits


def test_microprice_attached_when_book_present():
    now = time.time()
    raw = market(0, now=now)
    raw["bids"] = [[0.49, 200]]
    raw["asks"] = [[0.51, 100]]
    res = MarketScanner(TrainingConfig()).scan([raw], now=now)
    feat = res.shortlist[0]["features"]
    assert feat.depth_weighted_microprice is not None
    assert feat.order_book_imbalance is not None and feat.order_book_imbalance > 0


def test_scan_offline_when_catalog_supplied(monkeypatch):
    sc = MarketScanner(TrainingConfig())

    def _boom(*a, **k):  # fetch must NOT be called when a catalog is supplied
        raise AssertionError("network fetch attempted in offline scan")

    monkeypatch.setattr(sc, "fetch", _boom)
    res = sc.scan(catalog(5))
    assert res.kept == 5
