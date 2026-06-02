"""Scan-speed + scan-loop metrics for the Polymarket training engine."""

from __future__ import annotations

import pytest

from engine.training import TrainingConfig, MarketScanner
from engine.training.metrics import ScanMetrics

from tests._pmtrain_helpers import clean_live_env, catalog, market


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def test_scan_separates_scan_from_trade():
    sc = MarketScanner(TrainingConfig(shortlist_limit=5))
    res = sc.scan(catalog(20))
    assert res.scanned == 20 and res.kept == 20
    assert res.shortlisted == 5            # shortlist capped
    assert len(res.records) == 5


def test_scan_metrics_records_latency_and_rate():
    sc = MarketScanner(TrainingConfig())
    sc.scan(catalog(15))
    m = sc.metrics
    assert m.scan_latency_ms >= 0.0
    assert m.candidates_per_second >= 0.0
    assert m.scanned == 15


def test_incremental_rescan_updates_metrics():
    sc = MarketScanner(TrainingConfig())
    sc.scan(catalog(10))
    sc.scan(catalog(12))
    assert sc.metrics.scans == 2
    assert sc.metrics.scanned == 12        # reflects latest scan


def test_shortlist_respects_limit():
    sc = MarketScanner(TrainingConfig(shortlist_limit=3))
    res = sc.scan(catalog(30))
    assert res.shortlisted == 3


def test_scan_tracks_book_quality_aggregates():
    sc = MarketScanner(TrainingConfig())
    sc.scan(catalog(8, ask=0.43))   # spread 0.03
    assert sc.metrics.avg_spread > 0.0
    assert sc.metrics.avg_depth > 0.0


def test_scan_metrics_has_subscription_health_fields():
    m = ScanMetrics().to_dict()
    for k in ("stale_books", "reconnects", "parse_errors", "avg_bbo_age_ms",
              "avg_spread", "avg_depth", "subscription_refresh_ms", "subscription_churn"):
        assert k in m


def test_market_scanner_filters_closed_resolved_markets():
    sc = MarketScanner(TrainingConfig())
    cat = catalog(5) + [market(90, closed=True), market(91, active=False)]
    res = sc.scan(cat)
    assert all(r.market_id not in ("m90", "m91") for r in res.records)
    assert res.kept == 5


def test_market_scanner_records_rejection_reasons():
    sc = MarketScanner(TrainingConfig())
    res = sc.scan(catalog(3) + [market(90, closed=True), market(91, desc=False)])
    assert "closed" in res.reject_reasons
    assert sum(res.reject_reasons.values()) >= 2


def test_candidates_per_second_metric():
    sc = MarketScanner(TrainingConfig())
    sc.scan(catalog(20))
    assert sc.metrics.candidates_per_second >= 0.0
    d = sc.metrics.to_dict()
    assert "candidates_per_second" in d and "scan_latency_ms" in d


def test_candidate_ranker_penalizes_ambiguity():
    from engine.training.candidate_ranker import score_candidate
    from engine.markets import universe_manager as um
    cfg = TrainingConfig()
    clear = um.MarketRecord.from_raw(market(0, ambiguity=0.0))
    ambiguous = um.MarketRecord.from_raw(market(1, ambiguity=0.8))
    s_clear, _ = score_candidate(clear, cfg)
    s_amb, comp = score_candidate(ambiguous, cfg)
    assert s_clear > s_amb
    assert comp["ambiguity_penalty"] > 0


def test_scan_records_feature_and_grouping_metrics():
    sc = MarketScanner(TrainingConfig())
    sc.scan(catalog(12))
    d = sc.metrics.to_dict()
    # new institutional data-quality metrics are tracked + serialized
    assert 0.0 < d["feature_coverage"] <= 1.0
    assert 0.0 <= d["null_rate"] <= 1.0
    assert 0.0 <= d["stale_rate"] <= 1.0
    assert d["groups_detected"] >= 1
    assert d["markets_per_second"] >= 0.0


def test_feature_extraction_can_be_disabled():
    sc = MarketScanner(TrainingConfig(feature_extraction_enabled=False))
    res = sc.scan(catalog(8))
    assert res.features == []        # skipped when disabled
    assert res.kept == 8             # scan + filter still work
