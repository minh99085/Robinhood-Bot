"""Pass-6: profitability-aware active learning is the exploration authority.

Exploration is selected by ActiveLearningSelector (not random/hash), is strict-
realism + bounded-loss gated, diversity-capped, excluded from readiness, and
produces structured learning feedback. Bregman-first (Pass 4), profitability-first
(Pass 5), and paper realism (Pass 3) are preserved. PAPER ONLY.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.active_learning import ActiveLearningSelector

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("max_open_trades", 8)
    cfg.setdefault("exploration_enabled", True)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path)
    t._begin_exploration_phase()
    return t


def _rec(mid="m0", *, depth=2000, spread=0.02, ask=0.40, fresh=True, amb=0.05,
         category="crypto", group="market:m0", cluster=None):
    raw = market(0, bid=round(ask - spread, 4), ask=ask, depth=depth, category=category,
                 now=_NOW)
    raw["id"] = mid
    rec = um.MarketRecord.from_raw(raw, now=_NOW)
    rec.market_id = mid
    rec.group_key = group
    rec.cluster_id = cluster
    if not fresh:
        rec.book_age_s = 9999.0
    return rec


def _est(*, fresh=True, spread=0.02, amb=0.05, mid=0.40, conf=0.5, unc=0.6):
    return SimpleNamespace(market_id="m0", fresh_book=fresh, spread=spread,
                           ambiguity_score=amb, p_market_mid=mid, confidence=conf,
                           total_uncertainty=unc)


def _edge(*, net_edge=0.005, exec_price=0.40, p_final=0.45):
    return SimpleNamespace(net_edge=net_edge, executable_price=exec_price, p_final=p_final)


# --- selector ranking -------------------------------------------------------

def test_selector_ranks_informative_above_uninformative(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    sel = ActiveLearningSelector(t.cfg, learner=t.learner)
    informative = sel.score_candidate(rec=_rec(), est=_est(unc=0.9), reason="edge_too_low",
                                      edge=_edge(net_edge=0.008, p_final=0.55))
    flat = sel.score_candidate(rec=_rec(), est=_est(unc=0.05), reason="edge_too_low",
                               edge=_edge(net_edge=-0.04, p_final=0.40))
    assert informative["active_learning_score"] > flat["active_learning_score"]


# --- random/hash exploration disabled ---------------------------------------

def test_random_hash_exploration_cannot_open_when_disabled(tmp_path, monkeypatch):
    # active learning ON, random OFF -> the legacy hash path may never select.
    t = _trainer(tmp_path, monkeypatch, active_learning_enabled=True,
                 random_exploration_enabled=False)
    # force a market_id the hash gate would pass, with a near-threshold reason
    t.cfg.exploration_rate = 1.0
    d = t._active_learning_admit(_rec(), _est(), _edge(net_edge=0.005), "edge_too_low")
    assert d["decision"] in ("explore", "near_miss", "skip")
    # whatever the AL decision, the random-hash path did not open it
    assert t.active_learning_metrics["random_exploration_opened_trades"] == 0


def test_legacy_random_used_only_when_active_learning_off(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, active_learning_enabled=False,
                 random_exploration_enabled=True)
    t.cfg.exploration_rate = 1.0    # hash gate always passes
    d = t._active_learning_admit(_rec(), _est(), _edge(net_edge=0.005), "edge_too_low")
    assert d["decision"] == "explore" and d["learning_bucket"] == "random_legacy"
    assert t.active_learning_metrics["random_exploration_opened_trades"] == 1


# --- realism + bounded loss eligibility -------------------------------------

def test_exploration_requires_executable_ask(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    d = t._active_learning_admit(_rec(), _est(), _edge(exec_price=0.0), "edge_too_low")
    assert d["decision"] == "near_miss"
    assert t.active_learning_metrics["exploration_rejected_by_realism"] == 1


def test_exploration_rejects_thin_depth_as_near_miss(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    d = t._active_learning_admit(_rec(depth=10), _est(), _edge(), "edge_too_low")
    assert d["decision"] == "near_miss"
    assert t.near_miss_log and t.near_miss_log[-1]["failed_gate"] == "thin_depth"
    assert t.near_miss_log[-1]["distance_to_threshold"] == 15.0   # 25 - 10


def test_exploration_rejects_wide_spread_as_near_miss(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    d = t._active_learning_admit(_rec(spread=0.20, ask=0.50), _est(spread=0.20),
                                 _edge(exec_price=0.50), "edge_too_low")
    assert d["decision"] == "near_miss"
    assert t.near_miss_log[-1]["failed_gate"] == "wide_spread"


def test_exploration_rejects_expected_loss_over_cap(tmp_path, monkeypatch):
    # tiny loss cap forces rejection even on a clean book
    t = _trainer(tmp_path, monkeypatch, exploration_max_expected_loss_usd=0.001)
    d = t._active_learning_admit(_rec(), _est(), _edge(), "edge_too_low")
    assert d["decision"] == "near_miss"
    assert t.near_miss_log[-1]["failed_gate"] == "expected_loss_exceeds_cap"


# --- diversity + budget caps ------------------------------------------------

def test_diversity_cap_blocks_second_same_category(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, exploration_max_per_category_per_tick=1,
                 exploration_max_trades_per_tick=5, exploration_max_per_event=5,
                 exploration_max_per_cluster=5)
    a = t._active_learning_admit(_rec("a", category="crypto", group="event:a", cluster="ca"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    b = t._active_learning_admit(_rec("b", category="crypto", group="event:b", cluster="cb"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    assert a["decision"] == "explore"
    assert b["decision"] == "skip" and b["reason"] == "max_per_category_per_tick"


def test_max_trades_per_tick_cap(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, exploration_max_trades_per_tick=1,
                 exploration_max_per_category_per_tick=9, exploration_max_per_event=9,
                 exploration_max_per_cluster=9)
    a = t._active_learning_admit(_rec("a", group="event:a", cluster="ca"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    b = t._active_learning_admit(_rec("b", group="event:b", cluster="cb"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    assert a["decision"] == "explore" and b["decision"] == "skip"
    assert b["reason"] == "max_trades_per_tick"


# --- Bregman collision ------------------------------------------------------

def test_exploration_skips_bregman_market_collision(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t._bregman_open_markets = {"m0"}
    t._bregman_open_events = set()
    d = t._active_learning_admit(_rec("m0"), _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    assert d["decision"] == "skip" and d["reason"] == "bregman_collision"
    assert t.active_learning_metrics["exploration_rejected_by_collision"] == 1


# --- readiness separation ---------------------------------------------------

def test_exploration_excluded_from_readiness(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    rep = t.active_learning_report()
    assert rep["exploration_counted_toward_readiness"] is False
    assert rep["exploration_consumes_bregman_reserved_capacity"] is False
    pr = t.paper_realism_report()
    # readiness PnL only sums realistic non-exploration trades
    assert "readiness_pnl" in pr


# --- metrics emitted --------------------------------------------------------

def test_active_learning_metrics_emitted(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    rep = t.active_learning_report()
    for key in ("active_learning_enabled", "random_exploration_enabled",
                "random_exploration_opened_trades", "active_learning_candidates_considered",
                "active_learning_candidates_selected", "exploration_trades_opened",
                "exploration_shadow_only", "exploration_rejected_by_realism",
                "exploration_rejected_by_budget", "exploration_rejected_by_collision",
                "exploration_budget_used_usd", "exploration_expected_loss_usd",
                "exploration_pnl", "exploration_counted_toward_readiness",
                "top_learning_buckets", "category_coverage", "cluster_diversity",
                "avg_active_learning_score_selected", "avg_execution_quality_selected",
                "pending_feedback_count", "completed_feedback_count"):
        assert key in rep
    assert rep["active_learning_enabled"] is True
    assert rep["random_exploration_enabled"] is False
