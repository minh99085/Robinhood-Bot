"""Paper micro-exploration mode (PAPER ONLY) — opportunity-pressure + $1 capped trades.

Proves the safety contract: a micro-exploration paper trade opens ONLY on a fresh,
real-CLOB, executable, positive-after-cost opportunity; it is hard-capped at <= $1 and
<= 5 trades/run; it is excluded from readiness PnL; and it is NEVER taken on a stale
book, missing ask, synthetic NO, reference/fake fill, or negative after-cost edge.
Live trading stays disabled throughout.
"""

import time

import pytest

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.markets import universe_manager as um
from engine.training import paper_execution as pe
from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_790_000_000.0


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", max_open_trades=8, **cfg), data_dir=tmp_path)


def _books(yes_ask, no_ask, *, yes_size=22, no_size=20, age=2.0):
    ts = str(_NOW - age)
    return {"tok0a": {"asks": [{"price": str(yes_ask), "size": str(yes_size)}],
                      "bids": [{"price": str(round(yes_ask - 0.01, 4)), "size": "30"}],
                      "timestamp": ts},
            "tok0b": {"asks": [{"price": str(no_ask), "size": str(no_size)}],
                      "bids": [{"price": str(round(no_ask - 0.01, 4)), "size": "30"}],
                      "timestamp": ts}}


def _rec():
    return um.MarketRecord.from_raw(market(0, bid=0.44, ask=0.45, depth=10, now=_NOW), now=_NOW)


def _run(t, books=None, *, fetcher=None, max_age=120.0):
    if fetcher is None and books is not None:
        fetcher = lambda tok: books.get(tok)  # noqa: E731
    if fetcher is not None:
        assert t.enable_clob_hydration(book_fetcher=fetcher, max_book_age_s=max_age)
    return t._run_bregman([_rec()], _NOW)


# --------------------------------------------------------------------------- #
# Allowed path
# --------------------------------------------------------------------------- #
def test_micro_trade_on_fresh_real_positive_edge(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.45, 0.50))                       # YES+NO = 0.95 -> positive after cost
    m = t.bregman_exec_metrics
    assert m["certified_opportunities"] == 0          # full path rejects (thin depth/margin)
    assert m["paper_micro_exploration_enabled"] is True
    assert m["paper_micro_exploration_trades"] >= 1   # a real $1 paper trade DID open
    assert m["hydrated_positive_after_cost_candidates"] >= 1
    assert m["realistic_trade_goal_met_11h"] is True
    assert m["zero_trade_blocker_if_any"] == ""
    expl = [p for p in t.positions if p.exploration]
    assert expl and all(p.strategy_variant == "bregman_micro_exploration" for p in expl)


def test_micro_trade_notional_capped_at_one_dollar(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.45, 0.50))
    expl = [p for p in t.positions if p.exploration]
    assert expl
    bundle_notional = sum(p.entry_price * p.qty for p in expl)
    assert bundle_notional <= 1.0 + 1e-6            # hard <= $1 per bundle


# --------------------------------------------------------------------------- #
# Safety rejections (no trade)
# --------------------------------------------------------------------------- #
def test_no_micro_trade_on_negative_after_cost_edge(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.55, 0.50))                       # YES+NO = 1.05 -> negative edge
    m = t.bregman_exec_metrics
    assert m["paper_micro_exploration_trades"] == 0
    assert not [p for p in t.positions if p.exploration]
    assert "no_positive_after_cost" in m["zero_trade_blocker_if_any"]


def test_no_micro_trade_on_synthetic_no(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t)                                           # NO hydration -> NO leg synthetic
    m = t.bregman_exec_metrics
    assert m["paper_micro_exploration_trades"] == 0
    assert not [p for p in t.positions if p.exploration]


def test_no_micro_trade_on_stale_book(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.45, 0.50, age=9999.0), max_age=20.0)   # books far older than max age
    m = t.bregman_exec_metrics
    assert m["paper_micro_exploration_trades"] == 0
    assert not [p for p in t.positions if p.exploration]


def test_no_micro_trade_on_missing_ask(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    # NO token book has bids only (no executable ask) -> NO leg stays synthetic
    books = _books(0.45, 0.50)
    books["tok0b"] = {"bids": [{"price": "0.49", "size": "30"}], "timestamp": str(_NOW - 2)}
    _run(t, books)
    m = t.bregman_exec_metrics
    assert m["paper_micro_exploration_trades"] == 0
    assert not [p for p in t.positions if p.exploration]


def test_micro_policy_rejects_reference_and_missing_ask(tmp_path, monkeypatch):
    """The micro execution policy relaxes ONLY depth — reference/fake fills and
    missing asks are still NOT executable."""
    t = _trainer(tmp_path, monkeypatch)
    pol = t.bregman_micro_exec_policy
    ref = pe.PaperExecutionContext(fill_source=pe.SRC_REFERENCE, ask=0.45, bid=0.44,
                                   spread=0.01, depth_usd=5.0, book_age_sec=1.0,
                                   fresh_book=True, accepting_orders=True, is_bregman_leg=True)
    assert not pol.evaluate(ref).allow_executable_trade
    miss = pe.PaperExecutionContext(fill_source=pe.SRC_LIVE_CLOB, ask=None, bid=0.44,
                                    spread=0.01, depth_usd=5.0, book_age_sec=1.0,
                                    fresh_book=True, accepting_orders=True, is_bregman_leg=True)
    assert not pol.evaluate(miss).allow_executable_trade
    # but a thin (>= $1) fresh real book IS executable under the micro policy
    ok = pe.PaperExecutionContext(fill_source=pe.SRC_LIVE_CLOB, ask=0.45, bid=0.44,
                                  spread=0.01, depth_usd=5.0, book_age_sec=1.0,
                                  fresh_book=True, accepting_orders=True, is_bregman_leg=True)
    assert pol.evaluate(ok).allow_executable_trade


# --------------------------------------------------------------------------- #
# PnL separation + caps + live disabled
# --------------------------------------------------------------------------- #
def test_exploration_pnl_separated_from_readiness(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.45, 0.50))
    expl = [p for p in t.positions if p.exploration]
    assert expl
    # settle the exploration legs at a profit
    for p in expl:
        p.mark = p.entry_price + 0.10
        t._close(p, "test_settle")
    rep = t.paper_realism_report()
    assert rep["exploration_pnl"] > 0.0              # exploration PnL captured
    assert rep["readiness_pnl"] == 0.0              # but NEVER in readiness PnL


def test_max_trades_cap_enforced(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, paper_micro_exploration_max_trades=2)
    # run several ticks with fresh positive opportunities; cap at 2 total
    t.enable_clob_hydration(book_fetcher=lambda tok: _books(0.45, 0.50).get(tok),
                            max_book_age_s=120.0)
    for _ in range(6):
        t._run_bregman([_rec()], _NOW)
    assert t._micro_exploration_trades_total <= 2
    assert t.realism_counts["paper_micro_exploration_trades"] <= 2


def test_config_hard_clamps():
    cfg = TrainingConfig(mode="paper_train",
                         paper_micro_exploration_max_notional_usd=100.0,
                         paper_micro_exploration_max_trades=999)
    assert cfg.paper_micro_exploration_max_notional_usd <= 1.0
    assert cfg.paper_micro_exploration_max_trades <= 5


def test_micro_exploration_is_paper_only_no_live(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    _run(t, _books(0.45, 0.50))
    # every exploration position is a PAPER fill (has a paper fill/order id, paper broker)
    expl = [p for p in t.positions if p.exploration]
    assert expl
    assert t.mode == "paper_train"
    assert all(p.execution_realism_status == "realistic_executable" for p in expl)
    # disabling the flag stops all micro trades
    t2 = _trainer(tmp_path, monkeypatch, paper_micro_exploration_enabled=False)
    _run(t2, _books(0.45, 0.50))
    assert t2.bregman_exec_metrics["paper_micro_exploration_trades"] == 0
    assert not [p for p in t2.positions if p.exploration]


def test_hydration_pressure_cap_raised(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    assert t._bregman_clob_hydrator.max_groups_per_tick >= 120   # materially > old 40
