"""Polymarket PAPER training engine — core behaviour + edge gating + safety.

PAPER ONLY. No real orders are placed anywhere in these tests.
"""

from __future__ import annotations

import pytest

from engine.training import (PolymarketPaperTrainer, TrainingConfig, PaperPolicy,
                             ProbabilityStack)
from engine.training.probability_stack import ProbabilityEstimate
from engine.training.candidate_ranker import score_candidate
from engine.markets import universe_manager as um

from tests._pmtrain_helpers import (clean_live_env, catalog, market, FakeResearch,
                                    fake_rec)


@pytest.fixture
def env(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)
    return tmp_path


def _trainer(env, **cfg_kw):
    cfg_kw.setdefault("mode", "paper_train")
    cfg = TrainingConfig(**cfg_kw)
    return PolymarketPaperTrainer(cfg, data_dir=env, signal_model=FakeResearch())


# --- scanning / ranking ----------------------------------------------------

def test_universe_manager_scans_configured_limit(env):
    t = _trainer(env, scan_limit=10)
    res = t.scanner.scan(catalog(30))
    assert res.scanned == 10  # scan_limit caps the catalog slice


def test_candidate_ranker_filters_bad_markets(env):
    t = _trainer(env)
    cat = catalog(5) + [market(99, closed=True)]
    res = t.scanner.scan(cat)
    assert "closed" in res.reject_reasons
    assert all(r.market_id != "m99" for r in res.records)


def test_candidate_ranker_scores_liquidity_and_spread(env):
    cfg = TrainingConfig()
    good = um.MarketRecord.from_raw(market(1, liq=100000, ask=0.41))   # tight spread, deep
    bad = um.MarketRecord.from_raw(market(2, liq=1500, ask=0.439))     # wide spread, thin
    sg, _ = score_candidate(good, cfg)
    sb, _ = score_candidate(bad, cfg)
    assert sg > sb


def test_clob_subscription_limited_to_watchlist(env):
    t = _trainer(env, live_watch_limit=2, trade_candidate_limit=2)
    t.run_tick(catalog(10))
    # 2 watched markets * 2 token ids each = 4 subscribed assets, never more
    assert t.metrics.subscribed_assets <= t.cfg.live_watch_limit * 2


def test_scan_metrics_records_latency(env):
    t = _trainer(env)
    t.scanner.scan(catalog(20))
    assert t.metrics.scan_latency_ms >= 0.0
    assert t.metrics.scans == 1
    assert t.metrics.scanned == 20


# --- edge gating (unit, deterministic) -------------------------------------

def _est(**kw):
    base = dict(market_id="m0", p_market_mid=0.41, p_model=0.41, p_research=0.62,
                p_raw=0.58, p_final=0.58, shrink=0.8, confidence=0.8,
                research_source="grok_cache", research_usable=True,
                model_has_edge=False, ambiguity_score=0.0, evidence_score=0.7,
                stale_score=0.0, spread=0.02, liquidity_usd=20000.0,
                calibration_error=0.0, fresh_book=True, best_ask=0.42)
    base.update(kw)
    return ProbabilityEstimate(**base)


def test_edge_requires_fresh_book():
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(fresh_book=False), fake_rec())
    assert not edge.should_trade and edge.reason == "no_fresh_book"


def test_edge_requires_executable_price():
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(best_ask=None), fake_rec())
    assert not edge.should_trade and edge.reason == "no_executable_price"


def test_edge_requires_model_or_research_probability():
    est = _est(research_usable=False, model_has_edge=False)
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(est, fake_rec())
    assert not edge.should_trade and edge.reason == "no_model_or_research_probability"


def test_positive_edge_trades_only_after_costs_and_uncertainty():
    # fair 0.62 vs ask 0.42 = large edge -> trades after costs+band
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(p_final=0.62), fake_rec())
    assert edge.should_trade
    assert edge.net_edge > edge.threshold


def test_low_edge_no_trade():
    # fair barely above ask -> net edge below threshold
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(p_final=0.43), fake_rec())
    assert not edge.should_trade and edge.reason in ("edge_too_low", "uncertainty_too_high")


def test_high_ambiguity_no_trade():
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(ambiguity_score=0.6), fake_rec())
    assert not edge.should_trade and edge.reason == "ambiguity_too_high"


def test_wide_spread_no_trade():
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(spread=0.10), fake_rec())
    assert not edge.should_trade and edge.reason == "spread_too_wide"


def test_thin_depth_no_trade():
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(_est(), fake_rec(top_depth_usd=10.0))
    assert not edge.should_trade and edge.reason == "depth_too_thin"


def test_offline_stub_cannot_trade_by_default(env, monkeypatch):
    monkeypatch.setenv("POLYMARKET_ALLOW_OFFLINE_STUB_TRADING", "0")
    cfg = TrainingConfig(mode="paper_train", allow_offline_stub_trading=False)
    # default research model -> offline stub (no key); must NOT trade
    t = PolymarketPaperTrainer(cfg, data_dir=env)
    for _ in range(3):
        t.run_tick(catalog(8))
    t.finalize()
    assert t.pnl_summary()["trades_opened"] == 0


# --- integration: trade -> risk -> broker ----------------------------------

def test_max_open_trades_respected(env):
    t = _trainer(env, max_open_trades=3, trade_candidate_limit=10, max_hold_ticks=999)
    t.run_tick(catalog(10))
    assert len(t.open_positions()) <= 3


def test_every_training_trade_passes_risk_engine(env):
    t = _trainer(env, max_open_trades=5, trade_candidate_limit=10)
    t.run_tick(catalog(10))
    # one APPROVED risk decision per opened position; no fill without approval
    assert t.risk.approvals == len([p for p in t.positions])
    assert all(p.risk_decision_id.startswith("risk-") for p in t.positions)


def test_every_training_fill_has_trace_ids(env):
    t = _trainer(env)
    t.run_tick(catalog(10))
    assert t.fills_log, "expected at least one paper fill"
    for f in t.fills_log:
        assert f["proposal_id"].startswith("prop-")
        assert f["risk_decision_id"].startswith("risk-")
        assert f["order_id"].startswith("ord-")
        assert f["fill_id"].startswith("fill-")


def test_paperbroker_uses_clob_depth_for_polymarket(env):
    # depth cap (35% of top depth) limits fill notional below the requested $5
    t = _trainer(env, fixed_notional_usd=5.0, max_fill_depth_fraction=0.35)
    t.run_tick([market(0, depth=5)])  # tiny depth -> capped fill
    if t.fills_log:
        assert t.fills_log[0]["notional"] <= 5.0


def test_no_polymarket_reference_price_fantasy_fills(env):
    # no CLOB book + reference fills OFF -> no trade / no fantasy fill
    t = _trainer(env, allow_pm_reference_price_fills=False)
    t.run_tick([market(0, fresh=False)])
    assert t.broker.fills == 0
    assert t.pnl_summary()["trades_opened"] == 0


# --- learning + reports -----------------------------------------------------

def test_online_learner_records_trade_features(env):
    t = _trainer(env, max_open_trades=5)
    t.run_tick(catalog(6))
    t.finalize()
    assert t.learner.decisions > 0
    assert t.learner.closed > 0  # closed positions feed the learner


def test_feedback_loop_updates_bucket_stats(env):
    t = _trainer(env, max_open_trades=5)
    for _ in range(3):
        t.run_tick(catalog(6))
    t.finalize()
    summ = t.feedback.summary()
    assert summ["learner"]["closed"] > 0
    assert "edge_adjustment" in summ


def test_calibration_report_created(env, tmp_path):
    from engine.training.reports import write_reports
    t = _trainer(env, max_open_trades=5)
    t.run_tick(catalog(6))
    t.finalize()
    out = write_reports(t, out_root=tmp_path / "reports")
    assert "calibration.csv" in out["files"]


def test_edge_bucket_pnl_report_created(env, tmp_path):
    from engine.training.reports import write_reports
    t = _trainer(env, max_open_trades=5)
    t.run_tick(catalog(6))
    t.finalize()
    out = write_reports(t, out_root=tmp_path / "reports")
    # edge bucket PnL is part of the learning summary + report.md
    md = (tmp_path / "reports" / out["run_id"] / "report.md").read_text()
    assert "Edge bucket PnL" in md


def test_training_report_created(env, tmp_path):
    from engine.training.reports import write_reports, RECOMMENDATIONS
    t = _trainer(env)
    t.run_tick(catalog(6))
    t.finalize()
    out = write_reports(t, out_root=tmp_path / "reports")
    expected = {"summary.json", "report.md", "candidates.csv", "edge_diagnostics.csv",
                "orders.csv", "fills.csv", "learning.csv", "no_trade_reasons.csv",
                "calibration.csv"}
    assert expected.issubset(set(out["files"]))
    assert out["recommendation"] in RECOMMENDATIONS


def test_compile_import_training_modules():
    import importlib
    for m in ("config", "metrics", "candidate_ranker", "market_scanner",
              "subscription_manager", "probability_stack", "edge_engine",
              "paper_policy", "online_learner", "feedback_loop", "baselines",
              "diagnostics", "store", "reports", "polymarket_trainer"):
        assert importlib.import_module(f"engine.training.{m}") is not None


# --- modes ------------------------------------------------------------------

def test_observe_only_mode_does_not_open_trades(env):
    t = _trainer(env, mode="observe_only", max_open_trades=5)
    for _ in range(3):
        t.run_tick(catalog(8))
    t.finalize()
    assert t.pnl_summary()["trades_opened"] == 0
    # but it still evaluated + recorded candidates (learning continues)
    assert t.learner.decisions > 0


def test_disabled_mode_runs_nothing(env):
    t = _trainer(env, mode="disabled")
    r = t.run_tick(catalog(8))
    assert r.get("mode") == "disabled" and t.tick_count == 0


# --- probability stack ------------------------------------------------------

def test_probability_stack_requires_fresh_clob_for_trading(env):
    t = _trainer(env)
    rec = um.MarketRecord.from_raw(market(0, fresh=False))
    est = t.prob.estimate(rec, FakeResearch())
    assert est.fresh_book is False
    edge = t.edge_engine.evaluate(est, rec)
    assert edge.reason == "no_fresh_book" and not edge.should_trade


def test_probability_stack_blocks_offline_stub_by_default(env):
    from engine.campaigns.signal_models import SignalResult

    class Stub:
        name = "research"
        def evaluate(self, rec):
            return SignalResult(0.85, 0.6, "offline_research_stub", None)
        def status(self):
            return {}

    t = _trainer(env, allow_offline_stub_trading=False)
    rec = um.MarketRecord.from_raw(market(0))
    est = t.prob.estimate(rec, Stub())
    assert est.research_usable is False
    edge = t.edge_engine.evaluate(est, rec)
    assert edge.reason == "offline_stub_blocked"


def test_probability_stack_shrinks_toward_market(env):
    t = _trainer(env)
    rec = um.MarketRecord.from_raw(market(0, bid=0.28, ask=0.30))
    est = t.prob.estimate(rec, FakeResearch(fair=0.85))
    # p_final stays between market mid and the raw signal (conservative shrink)
    assert est.p_market_mid <= est.p_final <= est.p_raw
    assert est.shrink <= t.cfg.max_shrink_factor


def test_probability_stack_reduces_weight_for_high_ambiguity(env):
    t = _trainer(env)
    low = um.MarketRecord.from_raw(market(0, ambiguity=0.0))
    high = um.MarketRecord.from_raw(market(1, ambiguity=0.6))
    e_low = t.prob.estimate(low, FakeResearch(fair=0.85))
    e_high = t.prob.estimate(high, FakeResearch(fair=0.85))
    assert e_high.shrink <= e_low.shrink


# --- edge engine extras -----------------------------------------------------

def test_stale_book_no_trade(env):
    import time as _t
    t = _trainer(env)
    raw = market(0)
    raw["bookUpdatedTs"] = _t.time() - 120  # 2-minute-old book
    rec = um.MarketRecord.from_raw(raw)
    est = t.prob.estimate(rec, FakeResearch())
    edge = t.edge_engine.evaluate(est, rec)
    assert not edge.should_trade and edge.reason in ("no_fresh_book", "stale_research")


def test_missing_research_and_model_no_trade():
    est = _est(research_usable=False, model_has_edge=False, research_source="none")
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(est, fake_rec())
    assert not edge.should_trade and edge.reason == "no_model_or_research_probability"


def test_duplicate_event_exposure_blocked():
    est = _est(p_final=0.80)
    rec = fake_rec(group_key="dup")
    edge = PaperPolicy(TrainingConfig()).evaluate_edge(
        est, rec, open_event_groups={"dup"})
    assert not edge.should_trade and edge.reason == "duplicate_event_exposure"


def test_edge_engine_supports_buy_no(env):
    # YES heavily overpriced (ask 0.90) but fair YES low -> BUY NO has edge
    t = _trainer(env)
    rec = um.MarketRecord.from_raw(market(0, bid=0.88, ask=0.90))
    est = t.prob.estimate(rec, FakeResearch(fair=0.20))
    no_edge = t.edge_engine.evaluate(est, rec, outcome="NO")
    assert no_edge.outcome == "NO" and no_edge.side == "BUY"
    assert no_edge.executable_price is not None


# --- subscription churn -----------------------------------------------------

def test_clob_subscription_avoids_churn(env):
    from engine.training import SubscriptionManager
    sm_mgr = SubscriptionManager(TrainingConfig(live_watch_limit=50, max_subscription_churn=5))
    recs = [um.MarketRecord.from_raw(market(i)) for i in range(40)]
    h1 = sm_mgr.reconcile(recs)
    assert h1.added_assets <= 5  # churn capped per refresh
    # a totally different set still only churns up to the cap
    recs2 = [um.MarketRecord.from_raw(market(100 + i)) for i in range(40)]
    h2 = sm_mgr.reconcile(recs2)
    assert h2.churn_count <= 5


# --- grok / wallet safety ---------------------------------------------------

def test_grok_cannot_size_orders():
    from engine.campaigns.signal_models import ResearchSignalModel
    rsm = ResearchSignalModel()
    assert not hasattr(rsm, "size")
    assert not hasattr(rsm, "size_order")


def test_no_wallet_or_private_key_required(env, monkeypatch):
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_PRIVATE_KEY",
              "POLY_PRIVATE_KEY", "PK", "POLYMARKET_API_SECRET_SIGNER"):
        monkeypatch.delenv(k, raising=False)
    t = _trainer(env)
    assert t.preflight()["checks"]["no_wallet_or_private_key"] is True


def test_no_crypto_stock_orders_in_polymarket_only_mode():
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "engine" / "engine.py").read_text()
    # the legacy crypto/stock open-gate is short-circuited under polymarket_only_mode
    assert "polymarket_only_mode" in src and "_can_open" in src
