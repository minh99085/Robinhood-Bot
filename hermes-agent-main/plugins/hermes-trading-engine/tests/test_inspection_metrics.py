"""Tests for inspection feature extraction, scorecard, and baseline comparison."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspection_metrics as m  # noqa: E402


def _status():
    return {
        "mode": "paper",
        "runtime_seconds": 3600,
        "pnl": {"open_positions": 2, "trades_closed": 40, "equity": 510.0,
                "total_pnl": 10.0, "win_rate": 0.55},
        "scan_metrics": {"scanned": 1000, "kept": 80},
        "safety": {"ok": True, "live_detected": False},
        "monitoring": {"bregman_opportunities": 12, "certified_bregman_profit": 3.2,
                       "brier": 0.21},
        "training_campaign": {"evidence": {"paper_trades": 40, "bregman_candidates": 12,
                                           "bregman_certified": 5,
                                           "after_cost_expectancy": 0.8}},
        "btc_pulse": {"btc_pulse_enabled": True, "btc_pulse_frozen": False,
                      "btc_pulse_oracle_required": True, "btc_pulse_paper_trades": 9,
                      "btc_pulse_after_cost_pnl": 1.5},
        "news": {"news_scanner_enabled": True, "news_provider_mode": "offline_cache",
                 "news_items_fetched": 100, "news_items_used": 30},
        "btc_fast_price": {"enabled": True, "valid": True, "age_seconds": 2.0,
                           "disagreement_vs_chainlink_bps": 5.0},
        "campaign_safety": {"realistic_fill_enabled": True, "clean_label_guard_enabled": True},
    }


def _api():
    return {"chainlink_status": {"available": True,
                                 "btc_usd": {"enabled": True, "valid": True,
                                             "stale": False, "age_seconds": 30, "price": 65000}}}


def test_extract_features_maps_core_fields():
    feats = m.extract_features(_status(), _api(), {"present": True, "passing": True})
    assert feats["paper_training_running"] is True
    assert feats["runtime_minutes"] == 60.0
    assert feats["scanned_markets"] == 1000
    assert feats["equity"] == 510.0
    assert feats["chainlink_enabled"] is True
    assert feats["chainlink_valid"] is True
    assert feats["btc_fast_price_enabled"] is True
    assert feats["btc_pulse_oracle_gate_active"] is True
    assert feats["news_scanner_enabled"] is True
    assert feats["tests_passing"] is True


def test_extract_features_empty_status_is_all_unknown():
    feats = m.extract_features({}, {}, {})
    assert feats["equity"] is None
    assert feats["chainlink_enabled"] in (None, False)
    assert feats["scanned_markets"] is None


def test_scorecard_is_deterministic():
    feats = m.extract_features(_status(), _api(), {"present": True, "passing": True})
    safety = {"status": "OK", "critical": False, "warn": False}
    tests = {"present": True, "passing": True}
    obs = {"artifacts_found": True, "logs_collected": True, "api_ok": True}
    s1 = m.compute_scorecard(feats, safety, tests, True, {"available": False}, obs)
    s2 = m.compute_scorecard(feats, safety, tests, True, {"available": False}, obs)
    assert s1 == s2
    assert 0 <= s1["score"] <= 100
    assert s1["components"]["safety"]["score"] == 25
    assert s1["components"]["tests"]["score"] == 15


def test_scorecard_zero_safety_when_critical():
    feats = m.extract_features(_status(), _api(), {"present": True, "passing": True})
    s = m.compute_scorecard(feats, {"status": "CRITICAL", "critical": True},
                            {"present": True, "passing": True}, True,
                            {"available": False}, {})
    assert s["components"]["safety"]["score"] == 0


def test_compare_baseline_detects_regression():
    base_feats = m.extract_features(_status(), _api(), {})
    cur = dict(base_feats)
    cur["after_cost_pnl"] = base_feats["after_cost_pnl"] - 1.0  # material drop
    cur["equity"] = base_feats["equity"] - 100.0
    comp = m.compare_baseline(cur, {"features": base_feats})
    assert comp["available"] is True
    assert comp["regression"] is True
    assert "after_cost_pnl" in comp["degraded"]


def test_compare_baseline_detects_improvement():
    base_feats = m.extract_features(_status(), _api(), {})
    cur = dict(base_feats)
    cur["after_cost_pnl"] = base_feats["after_cost_pnl"] + 2.0
    cur["equity"] = base_feats["equity"] + 50.0
    comp = m.compare_baseline(cur, {"features": base_feats})
    assert comp["regression"] is False
    assert "after_cost_pnl" in comp["improved"]


def test_compare_baseline_none_is_current_state_only():
    feats = m.extract_features(_status(), _api(), {})
    comp = m.compare_baseline(feats, None)
    assert comp["available"] is False
    assert comp["regression"] is False


def test_tests_passing_regression_flag():
    base = {"features": {"tests_passing": True}}
    comp = m.compare_baseline({"tests_passing": False}, base)
    assert comp["regression"] is True
    assert comp["metrics"]["tests_passing"]["direction"] == "DEGRADED"


def test_detect_missing_features_flags_gaps():
    feats = m.extract_features({}, {}, {"present": False})
    missing = m.detect_missing_features(feats, {}, {"present": False})
    flagged = {x["feature"] for x in missing}
    assert "chainlink" in flagged
    assert "btc_fast_price" in flagged
    assert "tests" in flagged


def test_detect_missing_features_healthy_has_fewer():
    feats = m.extract_features(_status(), _api(), {"present": True, "passing": True})
    missing = m.detect_missing_features(feats, _api(), {"present": True, "passing": True})
    flagged = {x["feature"] for x in missing}
    assert "chainlink" not in flagged
    assert "btc_fast_price" not in flagged


# --------------------------------------------------------------------------- #
# Benchmark layer
# --------------------------------------------------------------------------- #
def test_build_benchmarks_shape_and_counts():
    feats = m.extract_features(_status(), _api(), {"present": True, "passing": True})
    b = m.build_benchmarks(feats)
    assert "benchmarks" in b and "summary" in b
    names = {r["name"] for r in b["benchmarks"]}
    for required in ("after_cost_pnl", "sharpe", "sortino", "calmar", "max_drawdown",
                     "brier", "ece", "bregman_certified_profit",
                     "fill_realism_rejection_rate", "exploration_validation_separated",
                     "paper_attribution_enabled"):
        assert required in names, required
    s = b["summary"]
    assert s["pass"] + s["warn"] + s["fail"] + s["missing"] == len(b["benchmarks"])


def test_benchmark_status_directions():
    # higher-is-better
    assert m._benchmark_status(2.0, "higher", 1.0, 0.0) == "pass"
    assert m._benchmark_status(0.5, "higher", 1.0, 0.0) == "warn"
    assert m._benchmark_status(-1.0, "higher", 1.0, 0.0) == "fail"
    # lower-is-better
    assert m._benchmark_status(0.10, "lower", 0.15, 0.25) == "pass"
    assert m._benchmark_status(0.20, "lower", 0.15, 0.25) == "warn"
    assert m._benchmark_status(0.30, "lower", 0.15, 0.25) == "fail"
    # bool
    assert m._benchmark_status(True, "bool", True, False) == "pass"
    assert m._benchmark_status(False, "bool", True, False) == "fail"
    # missing
    assert m._benchmark_status(None, "higher", 1.0, 0.0) == "missing"


def test_build_benchmarks_flags_failures():
    feats = {"after_cost_pnl": -10.0, "sharpe": -0.5, "max_drawdown": 0.40,
             "ece": 0.20, "fill_realism_enabled": True}
    b = m.build_benchmarks(feats)
    failing = set(b["failing"])
    assert "after_cost_pnl" in failing
    assert "sharpe" in failing
    assert "max_drawdown" in failing
    assert "ece" in failing


def test_fill_realism_rejection_rate_computed():
    st = _status()
    st["pnl"]["fantasy_fill_rejections"] = 3
    st["pnl"]["fill_attempts"] = 12
    feats = m.extract_features(st, _api(), {})
    assert feats["fill_realism_rejection_rate"] == round(3 / 12, 4)


# --------------------------------------------------------------------------- #
# Cross-surface consistency
# --------------------------------------------------------------------------- #
def test_detect_inconsistencies_equity_mismatch():
    feats = m.extract_features(_status(),
                               {"state": {"equity": 100.0}}, {})  # dashboard equity 100
    feats["equity"] = 510.0  # paper equity differs a lot
    incons = m.detect_inconsistencies(feats, _status(), {"state": {"equity": 100.0}})
    checks = {c["check"] for c in incons}
    assert "equity_mismatch" in checks


def test_detect_inconsistencies_none_when_consistent():
    feats = m.extract_features(_status(), {"state": {"equity": 510.0}}, {})
    incons = m.detect_inconsistencies(feats, _status(), {"state": {"equity": 510.0}})
    assert all(c["check"] != "equity_mismatch" for c in incons)


def test_detect_inconsistencies_live_mismatch_is_critical():
    status = {"safety": {"live_detected": True}}
    api = {"state": {"live_detected": False}}
    feats = m.extract_features(status, api, {})
    incons = m.detect_inconsistencies(feats, status, api)
    crit = [c for c in incons if c["check"] == "live_detected_mismatch"]
    assert crit and crit[0]["severity"] == "CRITICAL"


def test_dashboard_equity_extracted():
    feats = m.extract_features(_status(), {"state": {"equity": 777.0}}, {})
    assert feats["dashboard_equity"] == 777.0


# --------------------------------------------------------------------------- #
# Quant responsibilities matrix
# --------------------------------------------------------------------------- #
def test_quant_responsibilities_all_domains():
    qr = m.build_quant_responsibilities(m.extract_features(_status(), _api(), {}))
    for domain in ("data_ingestion", "preprocessing_features", "statistical_modeling",
                   "bregman_signals", "risk_portfolio", "backtest_simulation",
                   "robustness", "clobv2_execution", "monitoring",
                   "compliance_security_ops"):
        assert domain in qr, domain
        assert qr[domain]["owner"]
        assert qr[domain]["coverage"] in ("covered", "gap")
        assert isinstance(qr[domain]["responsibilities"], list)


def test_quant_responsibilities_coverage_gap_on_empty():
    qr = m.build_quant_responsibilities({})
    # With no features, every domain is a coverage gap.
    assert all(v["coverage"] == "gap" for v in qr.values())
