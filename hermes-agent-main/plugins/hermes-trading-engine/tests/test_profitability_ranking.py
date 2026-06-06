"""Pass-5: profitability-first ranking + hard after-cost governor.

Candidates compete on conservative, executable, AFTER-COST expected value —
annotated before shortlist truncation, hard-gated at decision time. Bregman-first
priority (Pass 4) and paper realism (Pass 3) are preserved. PAPER ONLY.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.candidate_ranker import annotate_profitability, rank_candidates
from engine.training.market_scanner import MarketScanner

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("max_open_trades", 8)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path)


# --- annotation before truncation (scanner) ---------------------------------

def test_annotation_runs_before_shortlist_truncation(tmp_path, monkeypatch):
    clean_live_env(monkeypatch, tmp_path)
    cfg = TrainingConfig(mode="paper_train", shortlist_limit=3, profitability_first=True)
    sc = MarketScanner(cfg, learner=None)
    raw = [market(i, now=_NOW) for i in range(10)]
    res = sc.scan(raw, now=_NOW)
    # every eligible candidate (not just the shortlist) is annotated pre-truncation
    assert all("profitability" in d for d in res.shortlist)
    ann = res.shortlist[0]["profitability"]
    for f in ("best_ask", "spread", "depth_at_price", "execution_drag",
              "profitability_bucket", "profitability_eligible", "fee_estimate_source"):
        assert f in ann


def test_profitability_first_reranks_tighter_spread_higher(tmp_path, monkeypatch):
    cfg = TrainingConfig(mode="paper_train", profitability_first=True)
    # two equal-liquidity markets; one has a much wider spread (worse executable cost)
    tight = um.MarketRecord.from_raw(market(0, bid=0.49, ask=0.51, liq=50_000, now=_NOW), now=_NOW)
    wide = um.MarketRecord.from_raw(market(1, bid=0.40, ask=0.60, liq=50_000, now=_NOW), now=_NOW)
    scored = rank_candidates([wide, tight], cfg, now=_NOW)
    annotate_profitability(scored, cfg, profitability_first=True, now=_NOW)
    # after profitability-first re-rank, the tighter-spread book ranks first
    assert scored[0]["record"].market_id == "m0"
    assert scored[0]["after_cost_score"] >= scored[1]["after_cost_score"]


def test_missing_ask_is_non_executable_bucket(tmp_path, monkeypatch):
    cfg = TrainingConfig(mode="paper_train")
    raw = market(0, now=_NOW)
    raw.pop("bestAsk", None); raw["bestAsk"] = None
    rec = um.MarketRecord.from_raw(raw, now=_NOW)
    scored = rank_candidates([rec], cfg, now=_NOW)
    annotate_profitability(scored, cfg, now=_NOW)
    ann = scored[0]["profitability"]
    assert ann["profitability_bucket"] == "non_executable"
    assert ann["profitability_eligible"] is False


# --- decision-time governor gate --------------------------------------------

def _gate(t, *, net_edge, exec_price=0.40, notional=5.0, exploratory=False):
    est = SimpleNamespace(market_id="m0", spread=0.02, p_market_mid=0.40)
    edge = SimpleNamespace(net_edge=net_edge, executable_price=exec_price)
    rec = SimpleNamespace(market_id="m0", liquidity_usd=20_000.0, end_ts=None)
    prop = SimpleNamespace(notional_usd=notional)
    return t._profitability_gate(rec, est, edge, prop, exploratory=exploratory)


def test_negative_after_cost_directional_rejected(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    pg = _gate(t, net_edge=-0.01)
    assert pg["decision"] == "reject"
    assert pg["profitability_bucket"] == "negative_after_cost"
    assert t.profitability_metrics["candidates_rejected_negative_after_cost"] == 1
    assert t.profitability_metrics["profitability_governor_hard_rejects"] == 1


def test_positive_after_cost_directional_allowed(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    pg = _gate(t, net_edge=0.08, notional=50.0)   # EV = 0.08 * (50/0.40) = 10 USD
    assert pg["decision"] == "allow"
    assert pg["profitability_bucket"] == "directional_after_cost_positive"
    assert pg["expected_value_usd"] > 0
    assert pg["observed_after_cost_roi"] > 0


def test_sub_threshold_after_cost_is_shadow_only(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    # positive but below min_after_cost_edge (0.01) -> shadow-only, not executed
    pg = _gate(t, net_edge=0.005, notional=5.0)
    assert pg["decision"] == "shadow_only"
    assert pg["profitability_bucket"] == "shadow_theoretical_only"
    assert t.profitability_metrics["candidates_shadow_theoretical_only"] == 1


def test_missing_executable_price_rejected_when_required(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, require_profitability_annotation=True)
    pg = _gate(t, net_edge=0.08, exec_price=0.0)
    assert pg["decision"] == "reject"
    assert pg["profitability_bucket"] == "non_executable"
    assert t.profitability_metrics["candidates_missing_profitability_data"] == 1


def test_exploration_is_bucketed_not_hard_ev_gated(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    pg = _gate(t, net_edge=-0.02, exploratory=True)   # near-miss negative edge
    assert pg["decision"] == "allow"
    assert pg["profitability_bucket"] == "exploration_feedback_positive"
    assert t.profitability_metrics["exploration_profitability_checked"] == 1


# --- Bregman after-cost sorting + priority preserved ------------------------

def _bregman_event(asks, group="elect"):
    recs = []
    for i, a in enumerate(asks):
        raw = market(i, bid=round(a - 0.02, 4), ask=a, liq=20_000, depth=2000,
                     category="crypto", group=group, now=_NOW)
        raw["negRiskComplete"] = True
        recs.append(um.MarketRecord.from_raw(raw, now=_NOW))
    return recs


def test_bregman_bundles_sorted_by_after_cost_quality(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    hi = SimpleNamespace(required_capital=10.0, profit_lower_bound=2.0,
                         fill_feasibility=1.0, legs=[])
    lo = SimpleNamespace(required_capital=10.0, profit_lower_bound=0.5,
                         fill_feasibility=1.0, legs=[])
    assert sorted([lo, hi], key=t._bregman_quality_key, reverse=True)[0] is hi


def test_bregman_priority_preserved_with_profitability(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    opened = t._run_bregman(_bregman_event([0.28, 0.30, 0.30]), _NOW)
    assert opened == 1
    rep = t.profitability_ranking_report()
    assert rep["bregman_first_priority_preserved"] is True
    assert rep["bregman_after_cost_positive"] >= 1
    assert "bregman_certified_positive" in rep["profitability_buckets"]


# --- metrics emitted --------------------------------------------------------

def test_profitability_metrics_emitted(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    rep = t.profitability_ranking_report()
    for key in ("profitability_first_enabled", "profitability_annotation_before_truncation",
                "candidates_annotated", "candidates_missing_profitability_data",
                "candidates_ranked_by_profitability", "candidates_rejected_negative_after_cost",
                "candidates_shadow_theoretical_only", "directional_after_cost_positive",
                "bregman_after_cost_positive", "exploration_profitability_checked",
                "avg_after_cost_edge_executed", "avg_after_cost_roi_executed",
                "total_expected_value_usd_executed", "profitability_governor_hard_rejects",
                "execution_without_annotation", "top_ranked_candidate_reason"):
        assert key in rep
    assert rep["profitability_first_enabled"] is True
