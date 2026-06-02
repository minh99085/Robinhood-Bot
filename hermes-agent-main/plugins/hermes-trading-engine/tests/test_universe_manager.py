"""Tests for the Adaptive Polymarket Market Universe Manager.

All tests are pure/offline — no network. They cover filtering, scoring,
tiering, trade-candidate selection (max-open + duplicate-event), the live
watchlist cap, config loading/clamping, and the offline CLI path.

The universe manager is selection-only: it never places, cancels, or sizes an
order, so nothing here touches the RiskEngine or any execution path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import engine  # noqa: F401  (ensures plugin root on sys.path via conftest)
from engine.markets import universe_manager as um

PLUGIN_ROOT = Path(engine.__file__).resolve().parent.parent


def _future_iso(days: float = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def mk(mid: str, **over) -> dict:
    """A raw Gamma market dict that passes all hard filters by default."""
    base = {
        "id": mid, "question": f"Question {mid}", "slug": mid,
        "active": True, "closed": False, "archived": False,
        "enableOrderBook": True, "acceptingOrders": True,
        "clobTokenIds": json.dumps([f"{mid}-yes", f"{mid}-no"]),
        "outcomePrices": json.dumps(["0.50", "0.50"]),
        "endDate": _future_iso(7),
        "description": "Resolves per the official source.",
        "liquidityNum": 50_000, "volume24hr": 20_000, "volumeNum": 200_000,
        "bestBid": 0.49, "bestAsk": 0.51, "spread": 0.02,
        "topDepthUsd": 1_000, "category": "politics",
        "events": [{"id": f"ev-{mid}"}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_market_filtering_works():
    cfg = um.UniverseConfig()
    ok, reason = um.passes_filters(mk("m1"), cfg)
    assert ok and reason == "ok"
    snap = um.build_universe([mk("m1"), mk("m2")], cfg)
    assert snap.passed_filters == 2
    assert snap.scanned == 2


def test_bad_closed_archived_markets_are_excluded():
    cfg = um.UniverseConfig()
    cases = {
        "inactive": mk("a", active=False),
        "closed": mk("b", closed=True),
        "archived": mk("c", archived=True),
        "orderbook_disabled": mk("d", enableOrderBook=False),
        "not_accepting_orders": mk("e", acceptingOrders=False),
        "expired": mk("f", endDate=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
        "no_end_date": mk("g", endDate=None),
    }
    for expected, raw in cases.items():
        ok, reason = um.passes_filters(raw, cfg)
        assert not ok, f"{expected} should be rejected"
        assert reason == expected, f"expected reason {expected}, got {reason}"

    snap = um.build_universe(list(cases.values()) + [mk("good")], cfg)
    assert snap.passed_filters == 1
    assert sum(snap.rejected_by_reason.values()) == len(cases)


def test_missing_clobtokenids_causes_rejection():
    cfg = um.UniverseConfig()
    ok, reason = um.passes_filters(mk("x", clobTokenIds=None), cfg)
    assert not ok and reason == "missing_clob_token_ids"
    ok2, reason2 = um.passes_filters(mk("y", clobTokenIds="[]"), cfg)
    assert not ok2 and reason2 == "missing_clob_token_ids"


def test_ambiguous_resolution_rejected_when_no_description_or_rules():
    cfg = um.UniverseConfig()
    ok, reason = um.passes_filters(mk("z", description="", rules="", resolutionSource=""), cfg)
    assert not ok and reason == "ambiguous_resolution"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_scoring_ranks_high_liquidity_tight_spread_higher():
    cfg = um.UniverseConfig()
    good = um.MarketRecord.from_raw(mk("good", liquidityNum=200_000, volume24hr=80_000,
                                       spread=0.005, bestBid=0.5, bestAsk=0.505, topDepthUsd=5_000))
    poor = um.MarketRecord.from_raw(mk("poor", liquidityNum=1_200, volume24hr=600,
                                       spread=0.06, bestBid=0.45, bestAsk=0.51, topDepthUsd=80))
    s_good = um.score_market(good, cfg)["score"]
    s_poor = um.score_market(poor, cfg)["score"]
    assert s_good > s_poor


def test_wide_spread_causes_penalty():
    cfg = um.UniverseConfig()
    tight = um.MarketRecord.from_raw(mk("t", spread=0.01))
    wide = um.MarketRecord.from_raw(mk("w", spread=0.09))
    r_tight = um.score_market(tight, cfg)
    r_wide = um.score_market(wide, cfg)
    assert "wide_spread" in r_wide["penalties"]
    assert "wide_spread" not in r_tight["penalties"]
    assert r_wide["score"] < r_tight["score"]


def test_extreme_price_penalised_unless_longshot_allowed():
    base = mk("lp", outcomePrices=json.dumps(["0.99", "0.01"]))
    rec = um.MarketRecord.from_raw(base)
    blocked = um.score_market(rec, um.UniverseConfig(allow_longshot=False))
    allowed = um.score_market(rec, um.UniverseConfig(allow_longshot=True))
    assert "extreme_price" in blocked["penalties"]
    assert "extreme_price" not in allowed["penalties"]


# ---------------------------------------------------------------------------
# Tiers + watchlist cap
# ---------------------------------------------------------------------------

def test_watchlist_does_not_exceed_configured_live_limit():
    cfg = um.UniverseConfig(trade_candidate_limit=2, live_watchlist_limit=3, shortlist_limit=5)
    markets = [mk(f"m{i}", liquidityNum=100_000 - i * 100) for i in range(20)]
    snap = um.build_universe(markets, cfg)
    assert len(snap.tier("A")) <= cfg.trade_candidate_limit
    assert len(snap.tier("B")) <= cfg.live_watchlist_limit
    # live token ids come only from A+B markets (<= 2+3 = 5 markets -> <= 10 tokens)
    assert len(snap.live_token_ids()) <= (cfg.trade_candidate_limit + cfg.live_watchlist_limit) * 2


def test_pipeline_tier_sizes_match_targets():
    cfg = um.UniverseConfig()  # 20 / 80 / 100 defaults
    markets = [mk(f"m{i}", liquidityNum=500_000 - i * 10, category=f"cat{i % 12}")
               for i in range(400)]
    snap = um.build_universe(markets, cfg)
    assert len(snap.tier("A")) == cfg.trade_candidate_limit          # 20
    assert len(snap.tier("B")) == cfg.live_watchlist_limit           # 80
    assert len(snap.trade_candidate_ids()) == cfg.trade_candidate_limit


# ---------------------------------------------------------------------------
# Trade-candidate selection: max-open + duplicate event
# ---------------------------------------------------------------------------

def test_max_open_trade_limit_is_enforced():
    cfg = um.UniverseConfig()  # default max 3 in paper
    markets = [mk(f"m{i}", liquidityNum=100_000 - i, events=[{"id": f"ev{i}"}]) for i in range(10)]
    snap = um.build_universe(markets, cfg)
    assert len(um.select_trade_candidates(snap, open_trades_count=0, paper=True)) == 3
    assert len(um.select_trade_candidates(snap, open_trades_count=2, paper=True)) == 1
    assert um.select_trade_candidates(snap, open_trades_count=3, paper=True) == []


def test_duplicate_event_exposure_is_blocked():
    cfg = um.UniverseConfig()
    # two markets in the same event group + others distinct
    markets = [
        mk("e1a", liquidityNum=300_000, events=[{"id": "E1"}]),
        mk("e1b", liquidityNum=290_000, events=[{"id": "E1"}]),
        mk("e2", liquidityNum=280_000, events=[{"id": "E2"}]),
        mk("e3", liquidityNum=270_000, events=[{"id": "E3"}]),
    ]
    snap = um.build_universe(markets, cfg)
    # without prior exposure: only ONE of the E1 markets is selected (dedup)
    picked = um.select_trade_candidates(snap, open_trades_count=0, paper=True)
    groups = [p.record.group_key for p in picked]
    assert len(groups) == len(set(groups)), "no duplicate event groups may be selected"
    # with E1 already open: neither E1 market may be selected
    picked2 = um.select_trade_candidates(snap, open_event_groups={"event:E1"},
                                         open_trades_count=1, paper=True)
    assert all(p.record.group_key != "event:E1" for p in picked2)


def test_effective_max_open_trades_paper_and_hard_cap():
    assert um.UniverseConfig(max_open_polymarket_trades=3).effective_max_open_trades(paper=True) == 3
    assert um.UniverseConfig(max_open_polymarket_trades=7).effective_max_open_trades(paper=True) == 5
    assert um.UniverseConfig(max_open_polymarket_trades=7).effective_max_open_trades(paper=False) == 7
    # hard cap is 8 even if asked for more
    assert um.UniverseConfig(max_open_polymarket_trades=999).effective_max_open_trades(paper=False) == 8


# ---------------------------------------------------------------------------
# Config loading / clamping
# ---------------------------------------------------------------------------

def test_config_values_are_loaded_correctly(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_LIMIT", "1500")
    monkeypatch.setenv("MARKET_SHORTLIST_LIMIT", "150")
    monkeypatch.setenv("MARKET_LIVE_WATCHLIST_LIMIT", "90")
    monkeypatch.setenv("MARKET_TRADE_CANDIDATE_LIMIT", "22")
    monkeypatch.setenv("MAX_OPEN_POLYMARKET_TRADES", "4")
    monkeypatch.setenv("MAX_ALLOWED_SPREAD", "0.05")
    cfg = um.UniverseConfig.from_env()
    assert cfg.scan_limit == 1500
    assert cfg.shortlist_limit == 150
    assert cfg.live_watchlist_limit == 90
    assert cfg.trade_candidate_limit == 22
    assert cfg.max_open_polymarket_trades == 4
    assert cfg.max_allowed_spread == 0.05


def test_config_clamps_to_safe_maxima(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_LIMIT", "999999")
    monkeypatch.setenv("MARKET_SHORTLIST_LIMIT", "999")
    monkeypatch.setenv("MARKET_LIVE_WATCHLIST_LIMIT", "999")
    monkeypatch.setenv("MARKET_TRADE_CANDIDATE_LIMIT", "999")
    monkeypatch.setenv("MAX_OPEN_POLYMARKET_TRADES", "999")
    cfg = um.UniverseConfig.from_env()
    assert cfg.scan_limit == um.MAX_CATALOG_SCAN == 2000
    assert cfg.shortlist_limit == um.MAX_SHORTLIST == 200
    assert cfg.live_watchlist_limit == um.MAX_LIVE_WATCHLIST == 120
    assert cfg.trade_candidate_limit == um.MAX_TRADE_CANDIDATES == 25
    assert cfg.max_open_polymarket_trades == um.MAX_OPEN_TRADES_HARD_CAP == 8


def test_scan_limit_caps_number_of_markets_processed():
    cfg = um.UniverseConfig(scan_limit=5)
    snap = um.build_universe([mk(f"m{i}") for i in range(50)], cfg)
    assert snap.scanned == 5


# ---------------------------------------------------------------------------
# Manager status + rebalance (no reconnect storms)
# ---------------------------------------------------------------------------

def test_status_reports_required_dashboard_fields():
    cfg = um.UniverseConfig(trade_candidate_limit=2, live_watchlist_limit=2)
    mgr = um.UniverseManager(cfg=cfg, paper=True, live_subscribe_enabled=True)
    mgr.ingest([mk(f"m{i}", liquidityNum=100_000 - i) for i in range(10)] + [mk("bad", closed=True)])
    st = mgr.status(open_polymarket_trades=1)
    for key in ("total_markets_scanned", "markets_passing_filters", "tier_a_count",
                "tier_b_count", "live_websocket_subscriptions", "trade_candidates",
                "rejected_by_reason", "max_open_trades", "open_polymarket_trades",
                "top_markets"):
        assert key in st, f"missing dashboard field {key}"
    assert st["open_polymarket_trades"] == 1
    assert len(st["top_markets"]) <= 10


def test_rebalance_only_on_meaningful_change():
    mgr = um.UniverseManager(cfg=um.UniverseConfig(), live_subscribe_enabled=True)
    mgr.apply_subscription([f"t{i}" for i in range(100)])
    # tiny change -> no rebalance
    small = [f"t{i}" for i in range(100)]
    small[0] = "changed"
    assert mgr.should_rebalance(small) is False
    # large change -> rebalance
    big = [f"u{i}" for i in range(100)]
    assert mgr.should_rebalance(big) is True


# ---------------------------------------------------------------------------
# Offline CLI
# ---------------------------------------------------------------------------

def test_cli_offline_scan_writes_status(tmp_path):
    catalog = [mk(f"m{i}", liquidityNum=200_000 - i * 100) for i in range(30)]
    catalog.append(mk("closed1", closed=True))
    cat_path = tmp_path / "catalog.json"
    cat_path.write_text(json.dumps(catalog), encoding="utf-8")
    out_path = tmp_path / "universe.json"

    res = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "scan_polymarket_universe.py"),
         "--from-json", str(cat_path), "--out", str(out_path)],
        capture_output=True, text=True, cwd=str(PLUGIN_ROOT),
    )
    assert res.returncode == 0, res.stderr
    assert out_path.exists()
    status = json.loads(out_path.read_text(encoding="utf-8"))
    assert status["available"] is True
    assert status["total_markets_scanned"] == 31
    assert status["markets_passing_filters"] == 30
    assert status["rejected_by_reason"].get("closed") == 1
    assert status["live_subscribe_enabled"] is False  # CLOB disabled by default
