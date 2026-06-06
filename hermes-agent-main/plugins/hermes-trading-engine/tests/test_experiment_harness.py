"""Pass-9: paper-training experiment harness (profiles, safety, comparison, edge
score, bottleneck diagnosis). Orchestration/diagnostics only — PAPER ONLY; no
strategy/threshold/realism changes."""

from __future__ import annotations

from engine.markets import universe_manager as um
from engine.training import experiments as X

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0

_REQUIRED_KEYS = {
    "run_id", "created_at", "profiles", "strategy_bucket_comparison", "bregman_comparison",
    "directional_comparison", "active_learning_comparison", "paper_realism_comparison",
    "profitability_comparison", "correlation_comparison", "readiness_comparison",
    "rejection_waterfall_comparison", "edge_scoreboard", "recommended_next_pass",
}


def _bregman_catalog(asks=(0.28, 0.30, 0.30), group="elect"):
    cat = []
    for i, a in enumerate(asks):
        raw = market(i, bid=round(a - 0.02, 4), ask=a, liq=20_000, depth=2000,
                     category="crypto", group=group, now=_NOW)
        raw["negRiskComplete"] = True
        cat.append(raw)
    return cat


# --- profiles + safety ------------------------------------------------------

def test_profile_loader_returns_required_profiles():
    profiles = X.load_profiles()
    for name in ("strict_full_system", "bregman_only", "directional_only",
                 "bregman_shadow_diagnostics", "correlation_shadow_ablation",
                 "profitability_shadow_ablation"):
        assert name in profiles


def test_strict_full_system_passes_safety():
    ok, errors = X.validate_profile_safety(X.load_profiles()["strict_full_system"])
    assert ok and errors == []


def test_unsafe_profile_fails_safety():
    unsafe = {"config": {"allow_pm_reference_price_fills": True}}
    ok, errors = X.validate_profile_safety(unsafe)
    assert not ok and any("allow_pm_reference_price_fills" in e for e in errors)
    unsafe2 = {"config": {"reject_on_stale_book": False}}
    assert not X.validate_profile_safety(unsafe2)[0]


def test_profile_config_refuses_unsafe():
    import pytest
    with pytest.raises(ValueError):
        X.profile_config("evil", {"config": {"allow_offline_stub_trading": True}})


# --- run + comparison -------------------------------------------------------

def _run(tmp_path, monkeypatch, names, catalog=None, ticks=1):
    clean_live_env(monkeypatch, tmp_path)
    profiles = X.load_profiles()
    summaries = {}
    for i, n in enumerate(names):
        ddir = tmp_path / f"d{i}"
        summaries[n] = X.run_profile(n, profiles[n], catalog=catalog, ticks=ticks,
                                     data_dir=ddir, now=_NOW)
    return summaries


def test_comparison_has_all_top_level_keys(tmp_path, monkeypatch):
    summaries = _run(tmp_path, monkeypatch, ["strict_full_system", "bregman_only"],
                     catalog=_bregman_catalog())
    comp = X.build_comparison("exp-test", summaries)
    assert _REQUIRED_KEYS <= set(comp.keys())
    assert comp["paper_only"] is True


def test_comparison_report_has_all_sections(tmp_path, monkeypatch):
    summaries = _run(tmp_path, monkeypatch, ["strict_full_system"], catalog=_bregman_catalog())
    md = X.comparison_to_markdown(X.build_comparison("exp-test", summaries))
    assert X.validate_comparison_report(md) == []


def test_bregman_only_opens_bundle_directional_shadow(tmp_path, monkeypatch):
    summaries = _run(tmp_path, monkeypatch, ["bregman_only"], catalog=_bregman_catalog())
    s = summaries["bregman_only"]
    assert s["bregman_funnel"]["bundles_opened"] >= 1
    # directional execution disabled -> no directional realistic trades
    assert s["run"]["directional_trades_opened"] == 0


# --- leaderboard: shadow never outranks realistic ---------------------------

def test_leaderboard_realistic_outranks_shadow():
    summary = {
        "readiness": {"readiness_trade_count": 1, "readiness_pnl": 0.0,
                      "bregman_realistic_pnl": 0.0, "directional_realistic_pnl": 1.0},
        "paper_realism": {"shadow_theoretical_pnl": 999.0, "shadow_trade_count": 50},
        "profitability_ranking": {"directional_after_cost_positive": 1},
        "active_learning": {}, "bregman_funnel": {"certified_opportunities": 0},
        "trade_ledger_summary": {"bregman_legs": 0, "directional_trades": 1},
    }
    lb = X.strategy_bucket_leaderboard(summary)
    realistic_rank = next(b["rank"] for b in lb if b["bucket"] == "directional_realistic_exploit")
    shadow_rank = next(b["rank"] for b in lb if b["bucket"] == "shadow_theoretical")
    assert realistic_rank < shadow_rank   # realistic ranks above huge shadow PnL


# --- edge score: penalize shadow/unrealistic --------------------------------

def test_edge_score_penalizes_shadow_only():
    shadow_only = {"readiness": {"readiness_trade_count": 0, "readiness_pnl": 0.0},
                   "paper_realism": {"shadow_theoretical_pnl": 50.0,
                                     "reference_fill_theoretical_pnl": 0.0},
                   "profitability_ranking": {}, "rejection_waterfall": {},
                   "correlation_risk": {}, "bregman_funnel": {}}
    real = {"readiness": {"readiness_trade_count": 12, "readiness_pnl": 0.5},
            "paper_realism": {"shadow_theoretical_pnl": 0.0,
                              "reference_fill_theoretical_pnl": 0.0},
            "profitability_ranking": {"avg_after_cost_roi_executed": 0.02},
            "rejection_waterfall": {}, "correlation_risk": {},
            "bregman_funnel": {"bundles_opened": 2}}
    es_shadow = X.edge_score(shadow_only)
    es_real = X.edge_score(real)
    assert es_real["edge_score"] > es_shadow["edge_score"]
    assert es_shadow["confidence_level"] == "low"
    assert es_real["confidence_level"] == "high"


def test_edge_score_penalizes_unrealistic_fill():
    s = {"readiness": {"readiness_trade_count": 5, "readiness_pnl": 0.2},
         "paper_realism": {"reference_fill_theoretical_pnl": 1.0,
                           "reference_price_fills_allowed_for_exploit": True},
         "profitability_ranking": {}, "rejection_waterfall": {}, "correlation_risk": {},
         "bregman_funnel": {}}
    es = X.edge_score(s)
    assert es["edge_score_components"]["unrealistic_fill_penalty"] < 0


# --- bottleneck classifiers -------------------------------------------------

def test_bregman_bottleneck_classes():
    assert X.bregman_bottleneck({"raw_groups_discovered": 0}) == "no_groups_found"
    assert X.bregman_bottleneck({"raw_groups_discovered": 5,
                                 "certified_opportunities": 0}) == "groups_found_not_certified"
    assert X.bregman_bottleneck({"raw_groups_discovered": 5, "certified_opportunities": 3,
                                 "bundles_opened": 0,
                                 "rejected_by_reason": {}}) == "certified_not_executable"
    assert X.bregman_bottleneck({"raw_groups_discovered": 5, "certified_opportunities": 3,
                                 "bundles_opened": 0,
                                 "rejected_by_reason": {"max_bundles_per_tick": 2}}) \
        == "executable_blocked_by_risk"
    assert X.bregman_bottleneck({"raw_groups_discovered": 5, "certified_opportunities": 3,
                                 "bundles_opened": 2}) == "opened_and_promising"


def test_directional_bottleneck_negative_after_cost():
    s = {"profitability_ranking": {"candidates_annotated": 20,
                                   "directional_after_cost_positive": 0,
                                   "candidates_rejected_negative_after_cost": 15},
         "strategy_priority": {}, "readiness": {}}
    assert X.directional_bottleneck(s) == "model_edge_not_after_cost_positive"


def test_active_learning_bottleneck_classes():
    assert X.active_learning_bottleneck({"active_learning_enabled": False}) == "disabled"
    assert X.active_learning_bottleneck({"active_learning_enabled": True,
                                         "active_learning_candidates_considered": 0}) \
        == "no_eligible_candidates"
    assert X.active_learning_bottleneck({"active_learning_enabled": True,
                                         "active_learning_candidates_considered": 5,
                                         "active_learning_candidates_selected": 2,
                                         "completed_feedback_count": 0}) \
        == "selected_pending_feedback"


# --- manifest + console + zero-trade ----------------------------------------

def test_run_manifest_has_no_secrets():
    comp = X.build_comparison("exp-x", {})
    m = X.run_manifest("exp-x", ["strict_full_system"], comp, command="cmd")
    assert m["paper_only"] is True and m["live_trading_disabled"] is True
    assert not any(k for k in m if k.lower() in ("api_key", "secret", "token"))


def test_console_summary_renders_zero_trade(tmp_path, monkeypatch):
    summaries = _run(tmp_path, monkeypatch, ["strict_full_system"], catalog=[])
    comp = X.build_comparison("exp-zero", summaries)
    line = X.console_summary(comp)
    assert "Run ID: exp-zero" in line and "Unrealistic fills counted as real: 0" in line


def test_recommended_next_pass_deterministic():
    comp = X.build_comparison("exp-r", {})
    assert "focus" in comp["recommended_next_pass"]
    assert "message" in comp["recommended_next_pass"]
