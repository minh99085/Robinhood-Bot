"""Market scan-throughput env aliases (MARKET_* / POLYMARKET_DECISION_BUDGET)."""

from __future__ import annotations

from engine.training.config import TrainingConfig


def test_market_scan_limit_alias(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_LIMIT", "2000")
    cfg = TrainingConfig.from_env()
    assert cfg.scan_limit == 2000


def test_market_shortlist_and_candidate_aliases(monkeypatch):
    monkeypatch.setenv("MARKET_SHORTLIST_LIMIT", "300")
    monkeypatch.setenv("MARKET_LIVE_WATCHLIST_LIMIT", "200")
    monkeypatch.setenv("MARKET_TRADE_CANDIDATE_LIMIT", "60")
    monkeypatch.setenv("POLYMARKET_DECISION_BUDGET", "120")
    cfg = TrainingConfig.from_env()
    assert cfg.shortlist_limit == 300
    assert cfg.live_watch_limit == 200
    assert cfg.trade_candidate_limit == 60
    assert cfg.paper_decision_budget == 120


def test_polymarket_names_still_work(monkeypatch):
    monkeypatch.delenv("MARKET_SCAN_LIMIT", raising=False)
    monkeypatch.setenv("POLYMARKET_SCAN_LIMIT", "1500")
    cfg = TrainingConfig.from_env()
    assert cfg.scan_limit == 1500


def test_scan_limit_hard_cap(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_LIMIT", "999999")
    cfg = TrainingConfig.from_env()
    assert cfg.scan_limit <= 2000          # hard clamp still applies
