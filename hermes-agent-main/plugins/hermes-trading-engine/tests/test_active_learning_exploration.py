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
    # genuinely thin: depth below the probe-sized floor (a <=$1 fill can't be supported)
    t = _trainer(tmp_path, monkeypatch)
    floor = t._exploration_micro_min_depth()
    thin = round(floor / 2.0, 2)
    d = t._active_learning_admit(_rec(depth=thin), _est(), _edge(), "edge_too_low")
    assert d["decision"] == "near_miss"
    assert t.near_miss_log and t.near_miss_log[-1]["failed_gate"] == "thin_depth"


def test_exploration_allows_fresh_book_thin_for_full_size(tmp_path, monkeypatch):
    # GATE-PRESERVING FIX: a FRESH book with depth >= the <=$1 probe floor but below the
    # full-size $25 gate is now EXPLORABLE at the tiny size (it was wrongly rejected as
    # thin before). EdgeEngine emits depth_too_thin only after the fresh-book check, so a
    # depth_too_thin reason routes the fresh candidate into the tiny evaluator.
    t = _trainer(tmp_path, monkeypatch)
    floor = t._exploration_micro_min_depth()
    assert floor < 25.0                                   # probe floor is sized, not full
    d = t._active_learning_admit(_rec(depth=10), _est(), _edge(), "depth_too_thin")
    assert d["decision"] == "explore"
    assert d.get("exploration_size", 0) > 0


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


# --- learning-probe QUALITY gate (Fix 1) ------------------------------------
def test_learning_probe_quality_scores_high_vs_low(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    hi = t._learning_probe_quality(_rec(depth=2000, spread=0.01), _est(spread=0.01),
                                   _edge(net_edge=0.01),
                                   {"active_learning_score": 0.8, "execution_quality_score": 0.9}, 1.0)
    lo = t._learning_probe_quality(_rec(depth=2000, spread=0.07), _est(spread=0.07),
                                   _edge(net_edge=-0.05),
                                   {"active_learning_score": 0.1, "execution_quality_score": 0.1}, 1.0)
    assert hi["probe_quality_score"] > lo["probe_quality_score"]
    assert hi["ev_class"] == "expected_value_positive"
    assert lo["ev_class"] == "controlled_negative_ev_learning"


def test_quality_gate_rejects_low_quality_probe_records_reason(tmp_path, monkeypatch):
    # an impossibly-high floor rejects even an eligible probe -> recorded, never lost
    t = _trainer(tmp_path, monkeypatch, exploration_min_probe_quality=0.99)
    d = t._active_learning_admit(_rec(), _est(), _edge(), "edge_too_low")
    assert d["decision"] == "near_miss" and d["reason"] == "below_quality_threshold"
    assert "probe_quality_score" in d
    assert d["shadowed_due_to_quality"] is True
    assert (t._probe_quality_rejected
            and t._probe_quality_rejected[-1]["reason"] == "below_quality_threshold")


def test_quality_floor_zero_opens_and_records(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, exploration_min_probe_quality=0.0)
    d = t._active_learning_admit(_rec(), _est(), _edge(), "edge_too_low")
    assert d["decision"] == "explore" and "probe_quality_score" in d
    assert (t._probe_quality_opened
            and t._probe_quality_opened[-1]["reason"] == "passed_quality_threshold")
    # profitability ranking surfaces the opened probe + reject lists (Fix 4)
    pr = t.profitability_ranking_report()
    assert pr["opened_learning_probes_count"] >= 1
    assert isinstance(pr["top_opened_learning_probes"], list)
    assert "top_rejected_near_misses" in pr


def test_after_cost_buckets_separated(tmp_path, monkeypatch):
    # paper realism report exposes separated after-cost buckets (Fix 2)
    t = _trainer(tmp_path, monkeypatch)
    pr = t.paper_realism_report()
    for k in ("readiness_after_cost_pnl", "exploration_after_cost_pnl",
              "total_after_cost_pnl_all_paper", "after_cost_accounting_bucket_consistent"):
        assert k in pr
    assert pr["after_cost_accounting_bucket_consistent"] is True
    assert pr["readiness_after_cost_pnl"] == pr["readiness_pnl"]


def test_feeds_health_explicit_reasons(tmp_path, monkeypatch):
    # Fix 3: chainlink/btc fast-price feed health with explicit enabled/valid/reason
    t = _trainer(tmp_path, monkeypatch)
    fh = t.status().get("feeds_health", {})
    for k in ("chainlink_enabled", "chainlink_valid", "chainlink_stale_reason",
              "btc_fast_price_enabled", "btc_fast_price_valid", "btc_fast_price_disabled_reason"):
        assert k in fh
    assert fh["read_only"] is True and fh["affects_live_trading"] is False
    # feeds disabled by default -> explicit disabled reasons, not silent
    if not fh["chainlink_valid"]:
        assert fh["chainlink_stale_reason"]
    if not fh["btc_fast_price_valid"]:
        assert fh["btc_fast_price_disabled_reason"]


def test_kill_switch_risk_signals_exclude_exploration(tmp_path, monkeypatch):
    """Bounded-loss exploration probes must NOT count toward the kill-switch's
    drawdown / loss-streak / calibration samples (they are intentional, budget-capped
    learning spend separated from readiness). Otherwise tiny losing probes self-trip the
    kill-switch and disable profit-discovery. A losing READINESS trade still counts."""
    from types import SimpleNamespace
    t = _trainer(tmp_path, monkeypatch)
    # 15 losing EXPLORATION probes (the VPS scenario)
    t.positions = [SimpleNamespace(closed=True, realized_pnl=-0.1, exploration=True,
                                   p_final=0.4, cost=1.0) for _ in range(15)]
    raw = t._monitoring_raw()
    assert raw["loss_streak"] == 0          # exploration excluded
    assert raw["drawdown"] == 0.0
    assert raw["samples"] == 0
    # a genuine READINESS loss DOES count (real risk protection intact)
    t.positions.append(SimpleNamespace(closed=True, realized_pnl=-5.0, exploration=False,
                                       p_final=0.4, cost=1.0))
    raw2 = t._monitoring_raw()
    assert raw2["loss_streak"] == 1
    assert raw2["samples"] == 1
    assert raw2["drawdown"] < 0.0


# --- loss-aware learning throttle (Fix 2) -----------------------------------

def test_throttle_inactive_without_enough_samples(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    # fewer than min_samples losing probes -> throttle stays OFF (no over-reaction)
    t._exploration_outcomes.extend([-0.1] * 3)
    thr = t._learning_probe_throttle()
    assert thr["learning_probe_throttle_active"] is False
    assert thr["learning_probe_quality_threshold"] == thr["learning_probe_quality_base_floor"]
    assert thr["learning_probe_throttle_reason"] == ""


def test_poor_recent_pnl_activates_throttle_and_raises_threshold(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, exploration_min_probe_quality=0.2,
                 learning_throttle_quality_bump=0.3)
    # 12 losing probes: win_rate 0.0 < 0.35 AND after-cost PnL -1.2 < -0.5 -> throttle ON
    t._exploration_outcomes.extend([-0.1] * 12)
    thr = t._learning_probe_throttle()
    assert thr["learning_probe_throttle_active"] is True
    assert thr["learning_probe_recent_win_rate"] == 0.0
    assert thr["learning_probe_recent_after_cost_pnl"] == -1.2
    assert thr["learning_probe_quality_threshold"] == 0.5     # 0.2 base + 0.3 bump
    assert "recent_win_rate" in thr["learning_probe_throttle_reason"]


def test_throttle_shadows_probe_that_would_open_when_healthy(tmp_path, monkeypatch):
    # huge bump makes the effective threshold unreachable while throttled -> SHADOW
    t = _trainer(tmp_path, monkeypatch, learning_throttle_quality_bump=1.0)
    # healthy (no outcomes): the probe OPENS
    d_open = t._active_learning_admit(_rec(), _est(unc=0.9), _edge(net_edge=0.005),
                                      "edge_too_low")
    assert d_open["decision"] == "explore"
    opened_before = t._probes_opened_due_to_quality
    # now poison recent outcomes -> throttle ON -> same probe is SHADOWED, not opened
    t._begin_exploration_phase()
    t._exploration_outcomes.extend([-0.1] * 12)
    d_shadow = t._active_learning_admit(_rec("m1"), _est(unc=0.9), _edge(net_edge=0.005),
                                        "edge_too_low")
    assert d_shadow["decision"] == "near_miss"
    assert d_shadow["reason"] == "below_quality_threshold"
    assert d_shadow["shadowed_due_to_quality"] is True
    assert d_shadow["throttle_active"] is True
    assert t._probes_shadowed_due_to_quality >= 1
    assert t._probes_opened_due_to_quality == opened_before   # no new open
    # labels are still collected (shadow sampling keeps learning)
    assert t.near_miss_log


def test_throttle_reduces_per_tick_frequency(tmp_path, monkeypatch):
    # base cap 2 -> halved to 1 while throttled (bump 0 so quality never interferes)
    t = _trainer(tmp_path, monkeypatch, exploration_max_trades_per_tick=2,
                 exploration_max_per_event=9, exploration_max_per_cluster=9,
                 exploration_max_per_category_per_tick=9,
                 learning_throttle_quality_bump=0.0)
    t._exploration_outcomes.extend([-0.1] * 12)        # throttle ON
    a = t._active_learning_admit(_rec("a", group="event:a", cluster="ca"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    b = t._active_learning_admit(_rec("b", group="event:b", cluster="cb"),
                                 _est(unc=0.9), _edge(net_edge=0.008), "edge_too_low")
    assert a["decision"] == "explore"
    assert b["decision"] == "skip" and b["reason"] == "max_trades_per_tick"


def test_negative_ev_probe_budget_shadows_when_exhausted(tmp_path, monkeypatch):
    # negative-EV probe budget 0 -> a controlled-negative-EV probe is shadowed
    t = _trainer(tmp_path, monkeypatch, exploration_max_negative_ev_probes_per_tick=0,
                 exploration_min_edge=-0.2)
    d = t._active_learning_admit(_rec(), _est(unc=0.9), _edge(net_edge=-0.02),
                                 "edge_too_low")
    assert d["decision"] == "near_miss"
    assert d["reason"] == "negative_ev_probe_budget_exhausted"
    assert d["ev_class"] == "controlled_negative_ev_learning"


def test_active_learning_report_exposes_throttle_metrics(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    rep = t.active_learning_report()
    for key in ("learning_probe_recent_win_rate", "learning_probe_recent_after_cost_pnl",
                "learning_probe_quality_threshold", "learning_probe_throttle_active",
                "learning_probe_throttle_reason", "probes_shadowed_due_to_quality",
                "probes_opened_due_to_quality"):
        assert key in rep


def test_governor_opens_high_quality_shadows_low_quality(tmp_path, monkeypatch):
    # at a modest floor, a high-quality probe OPENS while a low-quality one is SHADOWED
    t = _trainer(tmp_path, monkeypatch, exploration_min_probe_quality=0.7,
                 exploration_max_per_event=9, exploration_max_per_cluster=9,
                 exploration_max_per_category_per_tick=9,
                 exploration_max_trades_per_tick=9)
    hi = t._active_learning_admit(_rec("hi", depth=2000, spread=0.01),
                                  _est(unc=0.9, spread=0.01), _edge(net_edge=0.01),
                                  "edge_too_low")
    lo = t._active_learning_admit(_rec("lo", depth=12, spread=0.075),
                                  _est(unc=0.05, spread=0.075), _edge(net_edge=-0.04),
                                  "edge_too_low")
    assert hi["decision"] == "explore"
    assert lo["decision"] == "near_miss" and lo["shadowed_due_to_quality"] is True
    assert hi["probe_quality_score"] > lo["probe_quality_score"]


def test_exploration_outcomes_recorded_only_for_probes(tmp_path, monkeypatch):
    from engine.training.polymarket_trainer import PaperPosition
    t = _trainer(tmp_path, monkeypatch)
    # a readiness (non-exploration) close must NOT feed the throttle window
    rp = PaperPosition(proposal_id="p", risk_decision_id="rd", order_id="o", fill_id="fr",
                       market_id="r", asset_id="r", group_key="g", category="c",
                       outcome="YES", entry_price=0.4, qty=1.0, p_final=0.5,
                       net_edge=0.0, ambiguity=0.0, evidence=0.0, spread=0.0,
                       liquidity=0.0, open_tick=0, yes_price_entry=0.4,
                       executable_price_entry=0.4, p_market_entry=0.4,
                       exploration=False, mark=0.3)
    t._close(rp, "settled")
    assert len(t._exploration_outcomes) == 0
    ep = PaperPosition(proposal_id="p", risk_decision_id="rd", order_id="o", fill_id="fe",
                       market_id="e", asset_id="e", group_key="g", category="c",
                       outcome="YES", entry_price=0.4, qty=1.0, p_final=0.5,
                       net_edge=0.0, ambiguity=0.0, evidence=0.0, spread=0.0,
                       liquidity=0.0, open_tick=0, yes_price_entry=0.4,
                       executable_price_entry=0.4, p_market_entry=0.4,
                       exploration=True, mark=0.3)
    t._close(ep, "settled")
    assert len(t._exploration_outcomes) == 1
