"""Tests for the controlled PAPER-trading campaign (paper-only, no live path).

Covers preflight safety, market rejection reasons, max-open enforcement, paper
fill simulation, duplicate-exposure blocking, hourly/final report generation,
and the invariant that live trading stays disabled. All offline + deterministic.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import engine  # noqa: F401  (conftest puts plugin root on sys.path)
from engine.campaigns import paper_campaign as pc

PLUGIN_ROOT = Path(engine.__file__).resolve().parent.parent


def _future_iso(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def mk(mid, **over):
    base = {
        "id": mid, "question": f"[SIM] Q {mid}", "slug": mid,
        "active": True, "closed": False, "archived": False,
        "enableOrderBook": True, "acceptingOrders": True,
        "clobTokenIds": json.dumps([f"{mid}-y", f"{mid}-n"]),
        "outcomePrices": json.dumps(["0.50", "0.50"]),
        "endDate": _future_iso(7), "description": "Resolves per official source.",
        "liquidityNum": 80_000, "volume24hr": 30_000, "volumeNum": 300_000,
        "bestBid": 0.49, "bestAsk": 0.51, "spread": 0.02, "topDepthUsd": 2_000,
        "category": "politics", "events": [{"id": f"ev-{mid}"}],
    }
    base.update(over)
    return base


CLEAN_ENV = {"HTE_MODE": "paper", "HTE_AUTOTRADE": "0"}


class FixedRng:
    def __init__(self, r=0.5, u=0.0):
        self._r, self._u = r, u

    def random(self):
        return self._r

    def uniform(self, a, b):
        return self._u


# ---------------------------------------------------------------------------
# Phase 0 — preflight
# ---------------------------------------------------------------------------

def test_preflight_passes_in_clean_paper_env(tmp_path):
    cfg = pc.CampaignConfig()
    res = pc.preflight_check(cfg, env=dict(CLEAN_ENV), data_dir=tmp_path)
    assert res.ok is True
    assert res.live_config_detected is False
    assert res.red_warning is None
    assert all(c["passed"] for c in res.checks)


def test_preflight_blocks_when_micro_live_enabled(tmp_path):
    cfg = pc.CampaignConfig()
    env = dict(CLEAN_ENV, MICRO_LIVE_ENABLED="1")
    res = pc.preflight_check(cfg, env=env, data_dir=tmp_path)
    assert res.ok is False
    assert res.live_config_detected is True
    assert res.red_warning and "RED WARNING" in res.red_warning
    assert any(c["name"] == "micro_live_disabled" and not c["passed"] for c in res.checks)


def test_preflight_blocks_live_flags(tmp_path):
    cfg = pc.CampaignConfig()
    for flag in ("LIVE_BROKER_ENABLED", "PRODUCTION_EXECUTION_ENABLED", "MICRO_LIVE_ALLOW_PRODUCTION"):
        res = pc.preflight_check(cfg, env=dict(CLEAN_ENV, **{flag: "1"}), data_dir=tmp_path)
        assert res.ok is False and res.live_config_detected is True


def test_preflight_blocks_non_paper_or_autotrade(tmp_path):
    cfg = pc.CampaignConfig()
    r1 = pc.preflight_check(cfg, env={"HTE_MODE": "live", "HTE_AUTOTRADE": "0"}, data_dir=tmp_path)
    assert not r1.ok and any(c["name"] == "paper_mode_enabled" and not c["passed"] for c in r1.checks)
    r2 = pc.preflight_check(cfg, env={"HTE_MODE": "paper", "HTE_AUTOTRADE": "1"}, data_dir=tmp_path)
    assert not r2.ok and any(c["name"] == "real_trading_disabled" and not c["passed"] for c in r2.checks)


def test_config_clamps_paper_caps():
    # even if a yaml tried to raise these, they are hard-clamped
    cfg = pc.CampaignConfig(max_open_trades=10, max_trade_size_paper_usd=500)
    assert cfg.max_open_trades == 3
    assert cfg.max_trade_size_paper_usd == 25.0


def test_config_loads_from_yaml():
    cfg = pc.CampaignConfig.from_yaml(PLUGIN_ROOT / "config" / "paper_campaign.yaml")
    assert cfg.campaign_name == "controlled_paper_campaign_001"
    assert cfg.max_open_trades == 3
    assert cfg.max_trade_size_paper_usd == 25.0
    assert cfg.min_net_edge == 0.025


# ---------------------------------------------------------------------------
# Phase 1 — market rejection reasons
# ---------------------------------------------------------------------------

def test_market_rejection_reasons(tmp_path):
    cfg = pc.CampaignConfig()
    camp = pc.PaperCampaign(cfg, data_dir=tmp_path, seed=1)
    catalog = [
        mk("good1"), mk("good2"),
        mk("c", closed=True), mk("a", archived=True), mk("i", active=False),
        mk("nob", enableOrderBook=False), mk("nt", clobTokenIds=None),
    ]
    snap = camp.discover(catalog)
    assert camp.rejected_by_reason.get("closed") == 1
    assert camp.rejected_by_reason.get("archived") == 1
    assert camp.rejected_by_reason.get("inactive") == 1
    assert camp.rejected_by_reason.get("orderbook_disabled") == 1
    assert camp.rejected_by_reason.get("missing_clob_token_ids") == 1
    assert camp.passed_filters == 2


# ---------------------------------------------------------------------------
# Phase 3 — paper fill simulation
# ---------------------------------------------------------------------------

def test_paper_fill_simulation_outcomes():
    cfg = pc.CampaignConfig()
    sim = pc.PaperFillSimulator(cfg)
    # stale book -> rejected regardless of rng
    o = sim.simulate(side="BUY", intended_price=0.5, size_usd=25, top_depth_usd=2000,
                     book_age_ms=9999, rng=FixedRng())
    assert o.status == "rejected" and o.reason == "stale_book"
    # timeout / no_fill / cancelled gated by the random roll
    assert sim.simulate(side="BUY", intended_price=0.5, size_usd=25, top_depth_usd=2000,
                        book_age_ms=None, rng=FixedRng(r=0.02)).status == "rejected"
    assert sim.simulate(side="BUY", intended_price=0.5, size_usd=25, top_depth_usd=2000,
                        book_age_ms=None, rng=FixedRng(r=0.06)).status == "no_fill"
    assert sim.simulate(side="BUY", intended_price=0.5, size_usd=25, top_depth_usd=2000,
                        book_age_ms=None, rng=FixedRng(r=0.11)).status == "cancelled"
    # full fill (size <= depth)
    full = sim.simulate(side="BUY", intended_price=0.5, size_usd=25, top_depth_usd=2000,
                        book_age_ms=None, rng=FixedRng(r=0.5))
    assert full.status == "filled" and full.filled_size == 25
    assert full.fill_price >= 0.5  # BUY slips up
    # partial fill (size > depth)
    part = sim.simulate(side="BUY", intended_price=0.5, size_usd=500, top_depth_usd=100,
                        book_age_ms=None, rng=FixedRng(r=0.5))
    assert part.status == "partial" and part.filled_size == 100


# ---------------------------------------------------------------------------
# Risk gate — max-open + duplicate exposure
# ---------------------------------------------------------------------------

def test_risk_gate_blocks_max_open():
    cfg = pc.CampaignConfig()
    gate = pc.CampaignRiskGate(cfg)
    ok, reason = gate.evaluate(net_edge=0.05, spread=0.02, top_depth_usd=2000, size_usd=25,
                               open_trades=3, current_exposure=75, daily_new_trades=0,
                               event_group="g", open_event_groups=set())
    assert not ok and reason == "max_open_trades_reached"


def test_risk_gate_blocks_duplicate_event():
    cfg = pc.CampaignConfig()
    gate = pc.CampaignRiskGate(cfg)
    ok, reason = gate.evaluate(net_edge=0.05, spread=0.02, top_depth_usd=2000, size_usd=25,
                               open_trades=0, current_exposure=0, daily_new_trades=0,
                               event_group="event:E1", open_event_groups={"event:E1"})
    assert not ok and reason == "duplicate_event_exposure"


def test_risk_gate_blocks_low_edge_and_exposure():
    cfg = pc.CampaignConfig()
    gate = pc.CampaignRiskGate(cfg)
    assert gate.evaluate(net_edge=0.01, spread=0.02, top_depth_usd=2000, size_usd=25,
                         open_trades=0, current_exposure=0, daily_new_trades=0,
                         event_group="g", open_event_groups=set())[1] == "net_edge_below_min"
    assert gate.evaluate(net_edge=0.05, spread=0.02, top_depth_usd=2000, size_usd=25,
                         open_trades=0, current_exposure=90, daily_new_trades=0,
                         event_group="g", open_event_groups=set())[1] == "exposure_cap_reached"


# ---------------------------------------------------------------------------
# Full run — enforcement + reports + safety invariants
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for f in pc._LIVE_FLAGS + ("KALSHI_MICRO_LIVE_ENABLED",):
        monkeypatch.delenv(f, raising=False)
    monkeypatch.setenv("HTE_MODE", "paper")
    monkeypatch.setenv("HTE_AUTOTRADE", "0")


def _run(tmp_path, minutes=15, n=400, seed=1):
    cfg = pc.CampaignConfig()
    camp = pc.PaperCampaign(cfg, data_dir=tmp_path, seed=seed, accelerated=True)
    res = camp.run(lambda: pc.synthetic_catalog(n, seed=7), minutes=minutes, tick_seconds=60)
    return camp, res


def test_max_open_trades_never_exceeded_in_run(clean_env, tmp_path):
    camp, res = _run(tmp_path)
    assert res["started"] is True
    assert camp._max_open_seen() <= camp.cfg.max_open_trades
    assert camp.open_trade_count() <= camp.cfg.max_open_trades


def test_no_duplicate_event_exposure_in_run(clean_env, tmp_path):
    camp, _ = _run(tmp_path)
    groups = [p.event_group for p in camp.positions if p.status == "open"]
    assert len(groups) == len(set(groups))
    # also no duplicate order ids among open positions
    assert not camp._has_duplicate_positions()


def test_hourly_and_final_reports_generated(clean_env, tmp_path):
    camp, res = _run(tmp_path)
    root = camp.reports_root
    assert (root / "latest_report.md").exists()
    assert (root / "final_report.md").exists()
    hourly = list((root / "hourly").glob("report_*.md"))
    assert len(hourly) >= 1, "at least one hourly report must be written"
    final_txt = (root / "final_report.md").read_text(encoding="utf-8")
    assert "PASS/FAIL" in final_txt and "Recommended next" in final_txt


def test_live_trading_remains_disabled(clean_env, tmp_path):
    camp, res = _run(tmp_path)
    pf = res["pass_fail"]
    assert pf["checks"]["no_real_orders"] is True
    assert pf["checks"]["no_micro_live"] is True
    assert camp._live_violation is False
    assert camp.status()["mode"] == "PAPER / SIMULATED"


def test_campaign_aborts_on_live_config(tmp_path, monkeypatch):
    for f in pc._LIVE_FLAGS:
        monkeypatch.delenv(f, raising=False)
    monkeypatch.setenv("HTE_MODE", "paper")
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")  # simulate a live flag left on
    cfg = pc.CampaignConfig()
    camp = pc.PaperCampaign(cfg, data_dir=tmp_path, seed=1, accelerated=True)
    res = camp.run(lambda: pc.synthetic_catalog(50, seed=7), minutes=5, tick_seconds=60)
    assert res["started"] is False
    assert "RED WARNING" in (res.get("red_warning") or "")
    assert camp.status_state == "aborted_preflight"


def test_run_passes_and_reconciles(clean_env, tmp_path):
    camp, res = _run(tmp_path)
    pf = res["pass_fail"]
    assert pf["checks"]["pnl_reconciles"] is True
    assert pf["checks"]["every_rejection_has_reason"] is True
    assert pf["checks"]["drawdown_within_limit"] is True
    assert pf["decision"] == "PASS"
    # P&L identity: equity == starting + realized + unrealized
    m = camp.metrics()
    assert abs(camp.equity() - (camp.starting_cash + m["realized_pnl"] + m["unrealized_pnl"])) < 0.01


from engine.campaigns import signal_models as sm  # noqa: E402


def test_research_signal_model_offline_stub_without_key(monkeypatch):
    for k in ("XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCH_MODE", "offline_cache")
    model = sm.ResearchSignalModel(store=None, seed=1)
    assert model.grok_online is False
    rec = __import__("engine.markets.universe_manager", fromlist=["MarketRecord"]).MarketRecord.from_raw(mk("m1"))
    res = model.evaluate(rec)
    assert res.source == "offline_research_stub"
    assert 0.02 <= res.fair_value <= 0.98
    st = model.status()
    assert st["grok_enabled"] is False and st["grok_source"] == "offline_cache"


def test_research_model_grok_online_requires_key_and_mode(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    # construction may build a client but must never crash; status reflects online intent
    model = sm.ResearchSignalModel(store=None, seed=1)
    assert model.status()["grok_source"] in ("online_research", "offline_cache", "disabled")
    # without the key it must be offline regardless of mode
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    assert sm.ResearchSignalModel(store=None, seed=1).grok_online is False


def test_feedback_calibrator_recursive_adjustment(tmp_path):
    fb = sm.FeedbackCalibrator(path=tmp_path / "fb.json", min_samples=5)
    assert fb.edge_adjustment() == 1.0  # no samples yet
    for _ in range(8):  # mostly winners -> adjustment rises above 1.0
        fb.record_outcome(predicted_prob=0.7, predicted_edge=0.05, realized_pnl=2.0, size_usd=25)
    assert fb.edge_adjustment() > 1.0
    summ = fb.summary()
    assert summ["samples"] >= 5 and summ["hit_rate"] == 1.0
    # a fresh calibrator with mostly losers shrinks below 1.0
    fb2 = sm.FeedbackCalibrator(path=tmp_path / "fb2.json", min_samples=5)
    for _ in range(8):
        fb2.record_outcome(predicted_prob=0.6, predicted_edge=0.05, realized_pnl=-2.0, size_usd=25)
    assert fb2.edge_adjustment() < 1.0
    # state persists across reloads
    fb3 = sm.FeedbackCalibrator(path=tmp_path / "fb.json", min_samples=5)
    assert fb3.summary()["samples"] >= 5


def test_grok_remains_research_only_no_order_methods():
    # the research client + signal model must expose no order-placement surface
    src = (PLUGIN_ROOT / "engine" / "research" / "grok_client.py").read_text(encoding="utf-8").lower()
    sig = (PLUGIN_ROOT / "engine" / "campaigns" / "signal_models.py").read_text(encoding="utf-8").lower()
    for needle in ("submit_order", "place_order", "cancel_order", "oms.submit", "broker.submit"):
        assert needle not in src, f"grok_client has order surface: {needle}"
        assert needle not in sig, f"signal_models has order surface: {needle}"


def test_campaign_with_research_model_runs_and_feeds_back(clean_env, tmp_path):
    cfg = pc.CampaignConfig(signal_model="research", feedback_enabled=True)
    camp = pc.PaperCampaign(cfg, data_dir=tmp_path, seed=1, accelerated=True)
    res = camp.run(lambda: pc.synthetic_catalog(400, seed=7), minutes=60, tick_seconds=60)
    assert res["started"] is True
    st = camp.status()
    assert st["signal_model"]["name"] == "research"
    assert "offline_research_stub" in (st["signal_source_counts"] or {})
    assert st["feedback"]["samples"] >= 1  # closed trades fed back into the calibrator
    assert (tmp_path / "campaign_feedback_controlled_paper_campaign_001.json").exists()
    assert res["pass_fail"]["decision"] == "PASS"


def test_status_has_all_dashboard_fields(clean_env, tmp_path):
    camp, _ = _run(tmp_path)
    st = camp.status()
    for key in ("campaign_name", "status", "uptime_seconds", "total_markets_scanned",
                "markets_passing_filters", "tier_a_count", "tier_b_count",
                "rejected_market_count", "rejection_reasons", "current_open_trades",
                "max_open_trades", "paper_cash_balance", "paper_exposure", "realized_pnl",
                "unrealized_pnl", "total_paper_pnl", "win_rate", "avg_edge_at_entry",
                "avg_slippage", "fill_rate", "no_fill_rate", "rejected_order_count",
                "top_candidates", "risk_gate_status", "last_catalog_refresh",
                "last_score_refresh", "pass_fail"):
        assert key in st, f"missing dashboard field {key}"
