"""Aggressive vs conservative paper-scan coverage tests (offline).

Aggressive mode must scan wider, shortlist more candidates, refresh faster, use a
larger paper decision budget and a lower edge floor, and enable exploration +
feature/Chainlink layers — while NEVER selecting invalid, missing-token, or
ambiguous markets, and always FLAGGING stale books. PAPER ONLY throughout.
"""

from __future__ import annotations

import time

import pytest

from engine.training import TrainingConfig, MarketScanner
from engine.training.config import AggressivePaperTrainingConfig
from tests._pmtrain_helpers import clean_live_env, catalog, market


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def test_aggressive_profile_widens_scan_knobs():
    cons = TrainingConfig()
    agg = AggressivePaperTrainingConfig()
    assert agg.scan_limit >= cons.scan_limit
    assert agg.shortlist_limit > cons.shortlist_limit
    assert agg.trade_candidate_limit > cons.trade_candidate_limit
    assert agg.paper_decision_budget > cons.paper_decision_budget
    assert agg.min_net_edge < cons.min_net_edge
    assert agg.scan_interval_seconds < cons.scan_interval_seconds
    assert agg.subscription_refresh_s < cons.subscription_refresh_s
    # every non-live learning feature on
    assert agg.exploration_enabled and not cons.exploration_enabled
    assert agg.feature_extraction_enabled and agg.grouping_enabled
    assert agg.chainlink_enabled
    # safety: still paper-only with hard caps intact
    assert agg.is_paper_only and agg.mode == "paper_train"
    assert agg.max_open_trades <= agg.max_open_trades_hard_cap <= 8
    assert agg.max_order_notional_usd <= 50.0


def test_aggressive_shortlists_more_candidates():
    big = catalog(200)
    cons = MarketScanner(TrainingConfig()).scan(big)
    agg = MarketScanner(AggressivePaperTrainingConfig()).scan(big)
    assert cons.kept == agg.kept == 200
    assert agg.shortlisted > cons.shortlisted
    assert agg.shortlisted == 200 and cons.shortlisted == 150


def test_aggressive_never_selects_invalid_or_missing_token_markets():
    now = time.time()
    good = catalog(8, now=now)
    closed = market(90, closed=True, now=now)
    ambiguous = market(91, desc=False, now=now)
    missing_tok = market(92, now=now)
    missing_tok["clobTokenIds"] = []
    cat = good + [closed, ambiguous, missing_tok]

    res = MarketScanner(AggressivePaperTrainingConfig()).scan(cat, now=now)
    kept_ids = {r.market_id for r in res.records}
    # invalid markets are filtered out at the scan gate (a no-trade boundary)
    assert "m90" not in kept_ids and "m91" not in kept_ids and "m92" not in kept_ids
    assert "closed" in res.reject_reasons
    assert "ambiguous_resolution" in res.reject_reasons
    assert "missing_clob_token_ids" in res.reject_reasons


def test_aggressive_flags_stale_books_rather_than_hiding_them():
    now = time.time()
    fresh = catalog(5, now=now)
    stale = market(93, now=now)
    stale["bookUpdatedTs"] = now - 3600.0  # very old book, but market still valid
    res = MarketScanner(AggressivePaperTrainingConfig()).scan(fresh + [stale], now=now)

    # the stale market survives filters (it is a valid market) ...
    kept_ids = {r.market_id for r in res.records}
    assert "m93" in kept_ids
    # ... but it is explicitly FLAGGED: stale_rate > 0 and stale_book_score ~ 1
    assert res.stale_rate > 0.0
    stale_feat = next(d["features"] for d in res.shortlist
                      if d["record"].market_id == "m93")
    assert stale_feat.stale_book_score == pytest.approx(1.0)


def test_aggressive_scan_respects_scan_limit():
    agg = AggressivePaperTrainingConfig(scan_limit=25)
    res = MarketScanner(agg).scan(catalog(100))
    assert res.scanned == 25  # never processes more than the configured limit
