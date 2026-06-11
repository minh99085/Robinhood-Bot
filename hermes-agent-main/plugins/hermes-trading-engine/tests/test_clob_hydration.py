"""Read-only CLOB orderbook hydration for Bregman groups (PAPER ONLY).

Proves: real YES/NO books replace the synthetic NO price (best ASK, never midpoint);
synthetic NO stays diagnostic-only and never executable; hydration failure keeps the
group shadow/diagnostic-only with an exact reason; metrics are emitted; and hydration
never trades/sizes/loosens a gate.
"""

import time

from engine.training.clob_hydration import (BregmanClobHydrator, parse_clob_book,
                                            default_clob_book_fetcher)
from engine.training.bregman_grouping import build_binary_group
from engine.training.bregman_execution import BregmanArbitrageEngine

_NOW = 1_781_000_000.0


def _binary_rec(mid="573655", yes_bid="0.44", yes_ask="0.45", depth=5):
    return {"market_id": mid, "raw": {"bestAsk": yes_ask, "bestBid": yes_bid},
            "top_depth_usd": depth, "clob_token_ids": ["tokYES", "tokNO"],
            "book_age_s": 5.0, "question": "Will X happen?"}


def _books(yes_ask="0.46", no_ask="0.50", ts=None):
    ts = str(ts if ts is not None else _NOW - 3)
    return {"tokYES": {"asks": [{"price": yes_ask, "size": "500"}],
                       "bids": [{"price": "0.45", "size": "400"}], "timestamp": ts},
            "tokNO": {"asks": [{"price": no_ask, "size": "600"}],
                      "bids": [{"price": "0.49", "size": "300"}], "timestamp": ts}}


# --- parser -------------------------------------------------------------- #
def test_parse_clob_book_uses_best_ask_not_midpoint():
    p = parse_clob_book({"asks": [{"price": "0.55", "size": "10"},
                                  {"price": "0.50", "size": "20"}],
                         "bids": [{"price": "0.40", "size": "30"}], "timestamp": str(_NOW)})
    assert p["best_ask"] == 0.50          # LOWEST ask, never (0.50+0.40)/2 midpoint
    assert p["best_bid"] == 0.40
    assert p["ask_depth_usd"] == 0.50 * 20


def test_parse_clob_book_missing_ask_is_none():
    assert parse_clob_book({"asks": [], "bids": [{"price": "0.4", "size": "10"}]}) is None


# --- hydration: real books replace synthetic NO -------------------------- #
def test_real_yes_no_books_replace_synthetic_no():
    g = build_binary_group(_binary_rec())
    no_leg = [l for l in g.legs if l.outcome == "NO"][0]
    assert no_leg.synthetic_price is True                # synthetic before hydration
    books = _books()
    h = BregmanClobHydrator(book_fetcher=lambda t: books.get(t),
                            max_book_age_s=20.0, clock=lambda: _NOW)
    tel = h.hydrate([g], now=_NOW)
    assert no_leg.synthetic_price is False               # REAL book now
    assert no_leg.ask == 0.50                            # real best ask (not 1-YESbid, not mid)
    assert no_leg.hydrated_from_clob is True
    assert no_leg.visible_ask_depth_usd == 0.50 * 600
    assert tel["bregman_clob_hydration_attempted"] == 1
    assert tel["bregman_clob_hydration_success"] == 1
    assert tel["bregman_real_yes_no_books_seen"] == 2
    assert tel["bregman_certifier_used_real_clob_books"] is True
    assert tel["bregman_synthetic_no_diagnostic_only_count"] == 0


def test_hydration_failure_keeps_synthetic_shadow_only():
    g = build_binary_group(_binary_rec())
    h = BregmanClobHydrator(book_fetcher=lambda t: None,   # CLOB unavailable
                            max_book_age_s=20.0, clock=lambda: _NOW)
    tel = h.hydrate([g], now=_NOW)
    no_leg = [l for l in g.legs if l.outcome == "NO"][0]
    assert no_leg.synthetic_price is True                 # still synthetic -> diagnostic
    assert tel["bregman_clob_hydration_success"] == 0
    assert tel["bregman_certifier_used_real_clob_books"] is False
    assert tel["bregman_synthetic_no_diagnostic_only_count"] == 1
    assert tel["bregman_hydration_failure_reasons"]       # exact reason recorded


def test_disabled_hydrator_is_noop():
    h = BregmanClobHydrator(book_fetcher=None)             # no client -> disabled
    tel = h.hydrate([build_binary_group(_binary_rec())], now=_NOW)
    assert tel["bregman_clob_hydration_enabled"] is False
    assert tel["bregman_clob_hydration_attempted"] == 0


# --- synthetic flag drives executability (open-gate), never the certifier gates -- #
def _synthetic_certifiable_group(synthetic: bool):
    """A 2-leg exhaustive group with positive edge + ample depth that REACHES the
    certifier success path; one leg flagged synthetic per ``synthetic``."""
    from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
    legs = [SimplexLeg("a", "YES", "tA", ask=0.40, bid=0.39, depth_usd=5000, fresh_book=True),
            SimplexLeg("b", "NO", "tB", ask=0.40, bid=0.39, depth_usd=5000, fresh_book=True,
                       synthetic_price=synthetic)]
    return SimplexGroup("g", "binary_yes_no", legs, mutually_exclusive=True, exhaustive=True)


def test_certify_flags_synthetic_leg_on_success_path():
    eng = BregmanArbitrageEngine()
    opp_syn = eng.certify(_synthetic_certifiable_group(synthetic=True), now=_NOW)
    opp_real = eng.certify(_synthetic_certifiable_group(synthetic=False), now=_NOW)
    assert opp_syn.has_synthetic_leg is True     # synthetic leg -> flagged (blocked at open)
    assert opp_real.has_synthetic_leg is False   # real CLOB books -> can open


def test_open_gate_blocks_synthetic_allows_real(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=8),
                               data_dir=tmp_path)
    syn = SimpleNamespace(group_type="binary_yes_no", has_synthetic_leg=True,
                          profit_lower_bound=1.0, required_capital=10.0, certificate=None)
    assert t._open_bregman_sets([syn], [], time.time(), cap=None) == 0
    assert t.bregman_reject_reasons.get("synthetic_binary_not_executable", 0) >= 1
    # a REAL (hydrated) binary is NOT blocked by the synthetic gate (it advances to the
    # next strict gate — here the missing certificate -> a DIFFERENT reason, not synthetic)
    before = t.bregman_reject_reasons.get("synthetic_binary_not_executable", 0)
    real = SimpleNamespace(group_type="binary_yes_no", has_synthetic_leg=False,
                           profit_lower_bound=1.0, required_capital=10.0,
                           certificate=SimpleNamespace(full_hedge=False))
    t._open_bregman_sets([real], [], time.time(), cap=None)
    # real binary cleared the synthetic gate (count unchanged) -> stopped by a DIFFERENT,
    # unchanged strict gate (incomplete exhaustive set), never opening unsafely.
    assert t.bregman_reject_reasons.get("synthetic_binary_not_executable", 0) == before
    assert t.bregman_reject_reasons.get("incomplete_or_uncertain_exhaustive_set", 0) >= 1


# --- trainer path: a targeted binary group actually triggers hydration ----- #
def test_trainer_scan_bregman_hydrates_binary_group(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from engine.markets import universe_manager as um
    from tests._pmtrain_helpers import clean_live_env, market
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", max_open_trades=8),
                               data_dir=tmp_path)
    now = time.time()
    books = {"tok0a": {"asks": [{"price": "0.46", "size": "800"}],
                       "bids": [{"price": "0.45", "size": "700"}], "timestamp": str(now - 2)},
             "tok0b": {"asks": [{"price": "0.50", "size": "900"}],
                       "bids": [{"price": "0.49", "size": "600"}], "timestamp": str(now - 2)}}
    # inject a READ-ONLY mock fetcher (no network) — mirrors the live entrypoint wiring
    assert t.enable_clob_hydration(book_fetcher=lambda tok: books.get(tok),
                                   max_book_age_s=120.0) is True
    raw = market(0, bid=0.44, ask=0.45, now=now)   # binary YES/NO w/ tok0a/tok0b
    rec = um.MarketRecord.from_raw(raw, now=now)
    t.scan_bregman([rec], now)
    m = t.bregman_exec_metrics
    assert m["bregman_clob_hydration_attempted"] >= 1     # the user's failing metric
    assert m["bregman_clob_hydration_success"] >= 1
    assert m["bregman_real_yes_no_books_seen"] >= 2
    assert m["bregman_certifier_used_real_clob_books"] is True


def test_trainer_hydration_off_by_default_offline(tmp_path, monkeypatch):
    """A trainer built directly (no entrypoint, no env) stays offline: attempted=0."""
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from engine.markets import universe_manager as um
    from tests._pmtrain_helpers import clean_live_env, market
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.delenv("BREGMAN_CLOB_HYDRATION_ENABLED", raising=False)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    now = time.time()
    rec = um.MarketRecord.from_raw(market(0, now=now), now=now)
    t.scan_bregman([rec], now)
    assert t.bregman_exec_metrics["bregman_clob_hydration_attempted"] == 0


def test_default_fetcher_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("BREGMAN_CLOB_HYDRATION_ENABLED", raising=False)
    assert default_clob_book_fetcher() is None            # OFF by default (no network)
    monkeypatch.setenv("BREGMAN_CLOB_HYDRATION_ENABLED", "1")
    assert default_clob_book_fetcher() is not None        # a callable, but never auto-runs here
