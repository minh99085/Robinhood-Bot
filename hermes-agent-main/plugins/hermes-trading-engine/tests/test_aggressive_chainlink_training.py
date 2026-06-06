"""Aggressive mode + Chainlink: read-only oracle relevance expands market coverage
and feedback volume, while stale data can never boost rank/edge or open a trade.

All deterministic + offline (mocked Chainlink source; no RPC, no network)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from engine.feeds.chainlink import (ChainlinkReading, StaticChainlinkSource,
                                     ReplayChainlinkSource)
from engine.feeds.chainlink_registry import load_registry
from engine.chainlink_scanner import ChainlinkScanner
from engine.training import (AggressivePaperTrainingConfig, TrainingConfig,
                             PolymarketPaperTrainer)
from engine.training.candidate_ranker import rank_candidates
from engine.markets import universe_manager as um

from tests._pmtrain_helpers import clean_live_env, FakeResearch

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_chainlink_snapshots.jsonl"


def _r(feed, value, updated_at, *, observed=None, decimals=8):
    return ChainlinkReading(feed, int(value * 10 ** decimals), decimals, updated_at, 1,
                            observed if observed is not None else updated_at)


def _series(feed, base, n, t0, step=60):
    return [_r(feed, base, t0 + i * step, observed=t0 + i * step) for i in range(n)]


def _fresh_scanner(now, *, stale=False):
    eth = ([_r("ETH/USD", 3000, now - 4 * 3600, observed=now - 4 * 3600)] if stale
           else _series("ETH/USD", 3000, 8, now - 480))
    src = StaticChainlinkSource({"ETH/USD": eth,
                                 "BTC/USD": _series("BTC/USD", 60000, 8, now - 480)})
    reg = {k: load_registry()[k] for k in ("ETH/USD", "BTC/USD", "EUR/USD")}
    return ChainlinkScanner(src, registry=reg)


def _mkt(mid, q, *, category="crypto", slug=None, liq=20000):
    return {"id": mid, "question": q, "category": category, "slug": slug or mid,
            "active": True, "closed": False, "archived": False, "enableOrderBook": True,
            "acceptingOrders": True, "clobTokenIds": [f"{mid}a", f"{mid}b"],
            "outcomePrices": ["0.29", "0.71"], "bestBid": 0.28, "bestAsk": 0.30,
            "spread": 0.02, "liquidityNum": liq, "volume24hr": 8000, "topDepthUsd": 1500,
            "volumeNum": 40000, "endDate": "2030-01-01T00:00:00Z",
            "description": "Resolves YES per official price by end date. " * 4,
            "bookUpdatedTs": None}


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


# --- aggressive auto-enables Chainlink --------------------------------------

def test_aggressive_profile_auto_enables_chainlink():
    assert AggressivePaperTrainingConfig().chainlink_enabled is True


def test_trainer_builds_chainlink_in_aggressive_mode(env):
    t = PolymarketPaperTrainer(AggressivePaperTrainingConfig(), data_dir=env)
    assert t.chainlink is not None
    assert t.scanner.chainlink is t.chainlink and t.ranker.chainlink is t.chainlink


# --- coverage expansion via fresh relevance boost ---------------------------

def test_chainlink_boost_lifts_crypto_market_rank(env):
    now = 10_000.0
    sc = _fresh_scanner(now)
    cfg = TrainingConfig()
    # crypto market has LOWER liquidity than the politics market -> ranks lower
    # without Chainlink, but the fresh-oracle boost should lift it above.
    politics = um.MarketRecord.from_raw(_mkt("pol", "Will candidate X win?",
                                             category="politics", liq=80000))
    crypto = um.MarketRecord.from_raw(_mkt("eth", "Will ETH be above $2000?",
                                           category="crypto", slug="eth-2000", liq=2000))
    recs = [politics, crypto]
    base = [d["record"].market_id for d in rank_candidates(recs, cfg, now=now)]
    boosted = [d["record"].market_id for d in
               rank_candidates(recs, cfg, chainlink=sc, now=now)]
    assert base[0] == "pol"           # politics ranks first without Chainlink
    assert boosted[0] == "eth"        # crypto lifted to first WITH fresh Chainlink


def test_stale_chainlink_gives_no_rank_boost(env):
    now = 1_000_000.0
    sc = _fresh_scanner(now, stale=True)            # ETH feed 4h old -> stale
    crypto = um.MarketRecord.from_raw(_mkt("eth", "Will ETH be above $2000?",
                                           slug="eth-2000", liq=2000))
    assert sc.chainlink_boost(crypto, now=now) == 0.0     # stale -> no boost


def test_aggressive_chainlink_expands_coverage_and_feedback(env):
    now = time.time()
    catalog = [
        _mkt("pol0", "Will candidate A win the seat?", category="politics", liq=90000),
        _mkt("pol1", "Will the bill pass committee?", category="politics", liq=85000),
        _mkt("pol2", "Will the summit be held?", category="politics", liq=82000),
        _mkt("eth0", "Will ETH be above $2000 by date?", category="crypto",
             slug="eth-2000", liq=2500),
        _mkt("btc0", "Will Bitcoin be above $40000?", category="crypto",
             slug="btc-40000", liq=2500),
    ]

    def run(with_chainlink: bool):
        cfg = AggressivePaperTrainingConfig(max_open_trades=3, max_hold_ticks=1,
                                            trade_candidate_limit=3)
        sc = _fresh_scanner(now) if with_chainlink else None
        t = PolymarketPaperTrainer(cfg, data_dir=env, signal_model=FakeResearch(fair=0.80),
                                   chainlink=sc if with_chainlink else None)
        if not with_chainlink:
            t.chainlink = None
            t.scanner.chainlink = None
            t.ranker.chainlink = None
            t.prob.chainlink = None
        for _ in range(3):
            t.run_tick(catalog, now=now)
        t.finalize()
        crypto_ids = {c["market_id"] for c in t.candidates_log
                      if c["market_id"] in ("eth0", "btc0")}
        return t, crypto_ids

    base_t, base_crypto = run(False)
    cl_t, cl_crypto = run(True)

    # without Chainlink the 3 high-liquidity politics markets crowd out crypto;
    # with Chainlink the fresh oracle boost pulls crypto markets into coverage.
    assert len(cl_crypto) > len(base_crypto)
    assert cl_t.learner.closed >= base_t.learner.closed          # >= feedback samples
    # Pass-6: exploration is now information-value-selected (not random/hash), so
    # raw trade COUNT can shift slightly; chainlink coverage expansion is the
    # invariant (asserted above). Total opens stay comparable (within one trade).
    assert cl_t.pnl_summary()["trades_opened"] >= base_t.pnl_summary()["trades_opened"] - 1


# --- stale oracle can never open a trade ------------------------------------

def test_stale_chainlink_blocks_trade_in_aggressive_mode(env):
    now = 1_000_000.0
    sc = _fresh_scanner(now, stale=True)
    cfg = AggressivePaperTrainingConfig(max_hold_ticks=5)
    t = PolymarketPaperTrainer(cfg, data_dir=env, signal_model=FakeResearch(fair=0.80),
                               chainlink=sc)
    raw = _mkt("eth", "Will ETH be above $2000?", slug="eth-2000")
    raw["bookUpdatedTs"] = now
    rec = um.MarketRecord.from_raw(raw, now=now)
    est = t.prob.estimate(rec, FakeResearch(fair=0.80), now=now)
    assert est.chainlink_no_trade is True
    edge = t.edge_engine.evaluate(est, rec)
    assert not edge.should_trade and edge.reason == "chainlink_stale_or_irrelevant"
    assert edge.chainlink_no_trade is True       # surfaced in EdgeResult diagnostics


# --- replay-safe snapshots --------------------------------------------------

def test_replay_source_from_jsonl_is_timestamp_safe():
    src = ReplayChainlinkSource.from_jsonl(FIXTURE, cursor=1700000600)
    spec = load_registry()["ETH/USD"]
    # cursor caps at t=1700000600 -> the 1700001200/1700001800 readings are unseen
    r = src.read(spec)
    assert r is not None and r.updated_at <= 1700000600
    assert all(x.observed_ts <= 1700000600 for x in src.history("ETH/USD"))
    # a later cursor reveals more history (still no future beyond it)
    src2 = ReplayChainlinkSource.from_jsonl(FIXTURE, cursor=1700001800)
    assert len(src2.history("ETH/USD")) >= len(src.history("ETH/USD"))


def test_edge_result_carries_chainlink_diagnostics(env):
    now = 10_000.0
    sc = _fresh_scanner(now)
    cfg = AggressivePaperTrainingConfig()
    t = PolymarketPaperTrainer(cfg, data_dir=env, signal_model=FakeResearch(fair=0.80),
                               chainlink=sc)
    rec = um.MarketRecord.from_raw(_mkt("eth", "Will ETH be above $2000?", slug="eth-2000"))
    est = t.prob.estimate(rec, FakeResearch(fair=0.80), now=now)
    edge = t.edge_engine.evaluate(est, rec)
    d = edge.to_dict()
    assert "chainlink_confidence" in d and "chainlink_feed" in d
    assert d["chainlink_feed"] == "ETH/USD"
