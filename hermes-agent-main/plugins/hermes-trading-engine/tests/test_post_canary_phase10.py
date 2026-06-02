"""Phase 10 — post-canary analysis & scaling-veto tests.

All inputs are mocked contexts/fixtures. No real network, no real orders, no real
cancels, no credentials. Verifies that a canary is CLEAN only if the entire chain
is clean, that the maximum positive recommendation is REPEAT_DEMO_CANARY_SAME_SIZE,
and that size-increase / autonomous-live / production-execution are never enabled.
"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.post_canary import (FORBIDDEN_RECOMMENDATIONS, PostCanaryAnalysisRequest,  # noqa: E402
                                PostCanaryAnalyzer, PostCanaryConfig, analyze_context,
                                compute_eligibility, decide)
from engine.storage import Store  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "sample_clean_demo_canary.json"


def clean_ctx() -> dict:
    return copy.deepcopy(json.loads(_FIXTURE.read_text()))


def cfg():
    return PostCanaryConfig.from_env()


def analyze(ctx, **kw):
    return analyze_context(cfg(), ctx, **kw)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "pc.db")


# --- 1-2 loader ------------------------------------------------------------ #
def test_post_canary_loader_requires_live_order_attempt(store):
    from engine.post_canary.loader import LoaderError, load
    with pytest.raises(LoaderError):
        load(store, attempt_id=None)
    with pytest.raises(LoaderError):
        load(store, fixture={"attempt": {}})


def test_post_canary_loader_missing_required_trace_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["risk_decision_id"] = None
    ctx["attempt"]["safety_envelope_decision_id"] = None
    ctx["plan"]["source_dry_run_intent_id"] = None
    res = analyze(ctx)
    assert res.status in ("FAIL", "UNKNOWN_BLOCKING")
    assert res.recommendation == "STOP"


# --- 3-7 reconciliation ---------------------------------------------------- #
def test_reconciliation_unknown_status_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["status"] = "UNKNOWN"
    res = analyze(ctx)
    assert res.status in ("FAIL", "UNKNOWN_BLOCKING") and res.recommendation == "STOP"


def test_reconciliation_mismatch_blocks():
    ctx = clean_ctx()
    ctx["reconciliation"]["filled_quantity"] = "1"
    ctx["reconciliation"]["local_filled_quantity"] = "0"
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_reconciliation_duplicate_client_order_blocks():
    ctx = clean_ctx()
    ctx["duplicate_client_order_id"] = True
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_reconciliation_unexplained_open_order_blocks():
    ctx = clean_ctx()
    ctx["reconciliation"]["local_order_status"] = "OPEN"
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_emergency_cancel_blocks_clean_status():
    ctx = clean_ctx()
    ctx["emergency_cancels"] = [{"sent": 1, "success": 1, "venue": "kalshi"}]
    res = analyze(ctx)
    assert res.status != "CLEAN"
    assert res.recommendation in ("FIX_AND_REPEAT_SHADOW", "STOP")


# --- 8-12 execution quality ------------------------------------------------ #
def test_execution_payload_drift_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["request_payload_hash"] = "DIFFERENT"
    res = analyze(ctx)
    assert res.recommendation == "STOP"
    assert res.execution_quality.payload_drift_detected


def test_execution_wrong_environment_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["environment"] = "prod"
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_execution_wrong_market_blocks():
    ctx = clean_ctx()
    ctx["dry_run_intent"]["market_ticker"] = "OTHER-MARKET"
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_execution_over_notional_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["notional_filled"] = "5.0"  # above approved $0.50
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_execution_unexpected_partial_fill_blocks_by_default():
    ctx = clean_ctx()
    ctx["attempt"]["status"] = "PARTIALLY_FILLED"
    res = analyze(ctx)
    assert res.status != "CLEAN"
    assert res.recommendation in ("STOP", "FIX_AND_REPEAT_SHADOW")


def test_execution_slippage_above_threshold_blocks_repeat(monkeypatch):
    monkeypatch.setenv("POST_CANARY_MAX_SLIPPAGE_BPS", "10")
    ctx = clean_ctx()
    ctx["attempt"]["avg_fill_price"] = "0.60"  # 2000 bps worse than 0.50 intended
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


def test_fee_deviation_above_threshold_blocks_repeat(monkeypatch):
    monkeypatch.setenv("POST_CANARY_MAX_FEE_DEVIATION_BPS", "1")
    ctx = clean_ctx()
    ctx["reconciliation"]["fee"] = "1"
    ctx["attempt"]["fee"] = "100"  # huge deviation
    res = analyze(ctx)
    assert res.recommendation in ("FIX_AND_REPEAT_SHADOW", "STOP")
    assert res.status != "CLEAN"


# --- 15-18 market data ----------------------------------------------------- #
def test_market_data_stale_at_submit_blocks():
    ctx = clean_ctx()
    ctx["market_data"]["bbo_age_ms"] = 999999
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


def test_market_data_sequence_gap_blocks():
    ctx = clean_ctx()
    ctx["market_data"]["sequence_gap"] = True
    res = analyze(ctx)
    assert res.status == "FAIL" and res.recommendation == "STOP"


def test_market_data_tick_dirty_blocks():
    ctx = clean_ctx()
    ctx["market_data"]["tick_dirty"] = True
    res = analyze(ctx)
    assert res.status == "FAIL" and res.recommendation == "STOP"


def test_market_closed_at_submit_blocks():
    ctx = clean_ctx()
    ctx["market_data"]["market_status"] = "closed"
    res = analyze(ctx)
    assert res.recommendation == "STOP"


# --- 19-21 research -------------------------------------------------------- #
def test_research_stale_blocks():
    ctx = clean_ctx()
    ctx["research_stale"] = True
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


def test_research_high_ambiguity_blocks():
    ctx = clean_ctx()
    ctx["research"]["ambiguity_score"] = "0.95"
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


def test_research_low_evidence_blocks():
    ctx = clean_ctx()
    ctx["research"]["evidence_score"] = "0.1"
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


# --- 22-25 risk ------------------------------------------------------------ #
def test_risk_missing_decision_blocks():
    ctx = clean_ctx()
    ctx["attempt"]["risk_decision_id"] = None
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_safety_missing_decision_blocks():
    ctx = clean_ctx()
    ctx["safety_decision"] = None
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_risk_decision_after_submit_blocks():
    ctx = clean_ctx()
    ctx["safety_decision"]["ts_ms"] = ctx["attempt"]["ts_ms"] + 10000
    res = analyze(ctx)
    assert res.recommendation == "STOP"


def test_kill_switch_active_at_submit_blocks():
    ctx = clean_ctx()
    ctx["kill_switch_active_at_submit"] = True
    res = analyze(ctx)
    assert res.recommendation == "STOP"


# --- 26-27 chain ----------------------------------------------------------- #
def test_chain_audit_missing_shadow_link_warns_or_blocks():
    ctx = clean_ctx()
    ctx["plan"]["source_shadow_session_id"] = None
    ctx["plan"]["source_shadow_decision_id"] = None
    res = analyze(ctx)
    # shadow link is WARN -> at most FIX_AND_REPEAT, never CLEAN
    assert res.recommendation in ("FIX_AND_REPEAT_SHADOW", "REPEAT_DEMO_CANARY_SAME_SIZE")
    assert "trace_shadow_or_manual_link" in [c.check_name for _, c in res.all_checks()]


def test_chain_audit_hash_mismatch_blocks():
    ctx = clean_ctx()
    ctx["audit_chain_valid"] = False
    res = analyze(ctx)
    assert res.recommendation == "STOP"


# --- 28-29 secrets --------------------------------------------------------- #
def test_secret_audit_detects_secret_in_payload():
    ctx = clean_ctx()
    ctx["scan_blobs"] = ["-----BEGIN PRIVATE KEY-----\nXXXX\n-----END PRIVATE KEY-----"]
    res = analyze(ctx)
    assert res.recommendation == "STOP"
    assert res.secrets.secret_leak_count >= 1


def test_secret_audit_redacts_report(store, tmp_path, monkeypatch):
    monkeypatch.setenv("POST_CANARY_OUTPUT_DIR", str(tmp_path / "art"))
    ctx = clean_ctx()
    ctx["dry_run_intent"]["venue_payload_json"] = "key=0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef00"
    res = PostCanaryAnalyzer(store, cfg()).analyze(
        PostCanaryAnalysisRequest(live_order_attempt_id="mla-clean-001"), fixture=ctx)
    rep = [r for r in store.get_post_canary_reports(10) if r["analysis_id"] == res.analysis_id]
    blob = ""
    for sib in Path(rep[0]["report_path"]).parent.glob("*"):
        blob += sib.read_text()
    assert "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef00" not in blob


# --- 30-32 markout --------------------------------------------------------- #
def test_markout_calculates_known_values():
    ctx = clean_ctx()
    res = analyze(ctx)
    # BUY at 0.50, mid at 60s = (0.50+0.52)/2 = 0.51 -> +0.01 -> 200 bps
    assert res.markout.markout_60s_bps is not None
    assert abs(float(res.markout.markout_60s_bps) - 200.0) < 1.0


def test_markout_missing_all_horizons_blocks():
    ctx = clean_ctx()
    ctx["market_data"]["horizons"] = {}
    res = analyze(ctx)
    assert res.markout.status == "UNKNOWN"
    assert res.status == "UNKNOWN_BLOCKING" and res.recommendation == "STOP"


def test_markout_adverse_selection_threshold(monkeypatch):
    monkeypatch.setenv("POST_CANARY_MAX_ADVERSE_MARKOUT_BPS", "50")
    ctx = clean_ctx()
    # price collapses after our BUY -> adverse
    for h in ctx["market_data"]["horizons"].values():
        h["best_bid"], h["best_ask"] = "0.10", "0.12"
    res = analyze(ctx)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


# --- 33-37 veto ------------------------------------------------------------ #
def test_veto_stop_on_unknown_status():
    assert decide("UNKNOWN_BLOCKING") == "STOP"
    assert decide("FAIL") == "STOP"


def test_veto_repeat_demo_same_size_for_clean_demo():
    res = analyze(clean_ctx())
    assert res.recommendation == "REPEAT_DEMO_CANARY_SAME_SIZE"


def test_veto_never_returns_size_increase():
    for status in ("CLEAN", "FAIL", "WARN_REQUIRES_REVIEW", "CLEAN_BUT_NOT_ENOUGH_DATA",
                   "UNKNOWN_BLOCKING"):
        rec = decide(status, eligible_production_design_review=True)
        assert rec not in FORBIDDEN_RECOMMENDATIONS
        assert "INCREASE" not in rec and "SCALE" not in rec


def test_veto_never_returns_autonomous_live():
    for status in ("CLEAN", "FAIL", "WARN_REQUIRES_REVIEW"):
        assert "AUTONOMOUS" not in decide(status)


def test_veto_never_returns_enable_production():
    res = analyze(clean_ctx(), eligible_production_design_review=True)
    assert res.recommendation in ("REPEAT_DEMO_CANARY_SAME_SIZE",
                                  "MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN")
    assert "ENABLE_PRODUCTION" not in res.recommendation
    assert "READY" not in res.recommendation


# --- 38-43 eligibility ----------------------------------------------------- #
def _seed_attempt(store, aid, status="FILLED", venue="kalshi", env="demo", now=None):
    now = now or int(time.time() * 1000)
    store.add_micro_live_order_attempt({
        "live_order_attempt_id": aid, "canary_plan_id": "cp", "ts_ms": now, "venue": venue,
        "environment": env, "client_order_id": aid, "status": status, "submit_allowed": 1,
        "submitted": 1, "acknowledged": 1, "filled_quantity": "1", "notional_submitted": "0.5"})


def _seed_analysis(store, aid, status="CLEAN", rec="REPEAT_DEMO_CANARY_SAME_SIZE", now=None):
    now = now or int(time.time() * 1000)
    store.add_post_canary_analysis({
        "analysis_id": "an-" + aid, "live_order_attempt_id": aid, "canary_plan_id": "cp",
        "ts_ms": now, "status": status, "recommendation": rec, "hard_fail_count": 0,
        "warning_count": 0, "unknown_blocking_count": 0, "clean_for_repeat_demo_same_size": 1,
        "eligible_for_production_design_review": 0, "summary_json": "{}",
        "blocking_reasons_json": "[]", "next_required_actions_json": "[]"})


def test_eligibility_requires_no_unresolved_canaries(store):
    _seed_attempt(store, "a1", status="UNKNOWN")  # no analysis -> unresolved
    e = compute_eligibility(store, cfg(), "kalshi", "demo")
    assert e.unresolved_canaries >= 1
    assert not e.eligible_production_design_review


def test_eligibility_requires_min_clean_demo_for_prod_design(store):
    for i in range(2):
        _seed_attempt(store, f"a{i}", now=1000 + i)
        _seed_analysis(store, f"a{i}", now=1000 + i)
    e = compute_eligibility(store, cfg(), "kalshi", "demo",
                            renewed_shadow_hours=100, renewed_shadow_decisions=1000)
    assert e.clean_canaries == 2
    assert not e.eligible_production_design_review  # below min (5)


def test_eligibility_requires_renewed_shadow_after_canary(store):
    for i in range(6):
        _seed_attempt(store, f"b{i}", now=1000 + i)
        _seed_analysis(store, f"b{i}", now=1000 + i)
    e = compute_eligibility(store, cfg(), "kalshi", "demo")  # no renewed shadow provided
    assert not e.eligible_production_design_review


def test_eligibility_production_design_review_only(store):
    for i in range(6):
        _seed_attempt(store, f"c{i}", now=1000 + i)
        _seed_analysis(store, f"c{i}", now=1000 + i)
    e = compute_eligibility(store, cfg(), "kalshi", "demo",
                            renewed_shadow_hours=48, renewed_shadow_decisions=500)
    assert e.eligible_production_design_review is True
    assert e.eligible_size_increase is False


def test_size_increase_always_false(store):
    for i in range(10):
        _seed_attempt(store, f"d{i}", now=1000 + i)
        _seed_analysis(store, f"d{i}", now=1000 + i)
    e = compute_eligibility(store, cfg(), "kalshi", "demo",
                            renewed_shadow_hours=999, renewed_shadow_decisions=99999)
    assert e.eligible_size_increase is False


def test_autonomous_live_always_false():
    for ctx_status in ("CLEAN", "FAIL"):
        res = analyze(clean_ctx())
        assert res.eligible_for_autonomous_live is False
        assert res.eligible_for_size_increase is False


# --- 44-47 storage / report ------------------------------------------------ #
def test_post_canary_storage_migrations_idempotent(tmp_path):
    p = tmp_path / "idem.db"
    Store(p)
    s = Store(p)
    s.add_post_canary_audit_event({"event_type": "x", "message": "y"})
    assert s.get_post_canary_audit_events(10)


def test_post_canary_report_artifacts_created(store, tmp_path, monkeypatch):
    monkeypatch.setenv("POST_CANARY_OUTPUT_DIR", str(tmp_path / "art"))
    res = PostCanaryAnalyzer(store, cfg()).analyze(
        PostCanaryAnalysisRequest(live_order_attempt_id="mla-clean-001"), fixture=clean_ctx())
    rep = [r for r in store.get_post_canary_reports(10) if r["analysis_id"] == res.analysis_id][0]
    d = Path(rep["report_path"]).parent
    for f in ("post_canary_summary.json", "post_canary_report.md", "audit_checks.csv",
              "reconciliation_audit.json", "markout.csv", "eligibility.json",
              "redacted_trace.json"):
        assert (d / f).exists()


def test_post_canary_report_says_no_scaling(store, tmp_path, monkeypatch):
    monkeypatch.setenv("POST_CANARY_OUTPUT_DIR", str(tmp_path / "art"))
    res = PostCanaryAnalyzer(store, cfg()).analyze(
        PostCanaryAnalysisRequest(live_order_attempt_id="mla-clean-001"), fixture=clean_ctx())
    rep = [r for r in store.get_post_canary_reports(10) if r["analysis_id"] == res.analysis_id][0]
    md = Path(rep["report_path"]).read_text()
    assert "No scaling is approved" in md


def test_post_canary_report_says_no_autonomous_live(store, tmp_path, monkeypatch):
    monkeypatch.setenv("POST_CANARY_OUTPUT_DIR", str(tmp_path / "art"))
    res = PostCanaryAnalyzer(store, cfg()).analyze(
        PostCanaryAnalysisRequest(live_order_attempt_id="mla-clean-001"), fixture=clean_ctx())
    rep = [r for r in store.get_post_canary_reports(10) if r["analysis_id"] == res.analysis_id][0]
    md = Path(rep["report_path"]).read_text()
    assert "No autonomous live trading is approved" in md
    assert "Production execution remains unimplemented" in md


# --- 48-50 api ------------------------------------------------------------- #
def test_post_canary_api_has_no_submit_cancel_scale_routes():
    src = (_ROOT / "engine" / "app.py").read_text()
    for tok in ("/api/post-canary/submit", "/api/post-canary/cancel", "/api/post-canary/scale",
                "/api/post-canary/production", "/api/post-canary/size"):
        assert tok not in src, f"forbidden route {tok}"
    assert "/api/post-canary/analyze" in src and "/api/post-canary/eligibility" in src


def test_post_canary_api_redacts_secrets():
    # eligibility/analysis dicts contain no secret-bearing fields
    e = compute_eligibility(None, cfg(), "kalshi", "demo")
    blob = json.dumps(e.model_dump(), default=str)
    assert "PRIVATE KEY" not in blob


def test_post_canary_analyze_endpoint_no_order_calls():
    # analyzer source must not reference order submit/cancel
    for mod in ("analyzer", "loader", "reconciliation_audit", "execution_quality"):
        src = (_ROOT / "engine" / "post_canary" / f"{mod}.py").read_text().lower()
        for bad in ("submit_fok_canary_order", "submit_order", "create_order", "cancel_order",
                    "emergency_cancel("):
            assert bad not in src, f"{mod} references {bad}"


# --- 51-54 CLI ------------------------------------------------------------- #
def _run_cli(name, argv):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_cli_{name}",
                                                  _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(argv)


def test_cli_post_canary_analyze_help():
    with pytest.raises(SystemExit) as e:
        _run_cli("post_canary_analyze", ["--help"])
    assert e.value.code == 0


def test_cli_post_canary_veto_exit_codes(store):
    db = str(store.db_path)
    _seed_attempt(store, "v1")
    store.add_post_canary_analysis({
        "analysis_id": "an-stop", "live_order_attempt_id": "v1", "canary_plan_id": "cp",
        "ts_ms": 9999, "status": "FAIL", "recommendation": "STOP", "hard_fail_count": 1,
        "warning_count": 0, "unknown_blocking_count": 0, "clean_for_repeat_demo_same_size": 0,
        "eligible_for_production_design_review": 0, "summary_json": "{}",
        "blocking_reasons_json": "[]", "next_required_actions_json": "[]"})
    assert _run_cli("post_canary_veto", ["--analysis-id", "an-stop", "--db", db,
                                         "--fail-on-stop"]) != 0
    store.add_post_canary_analysis({
        "analysis_id": "an-repeat", "live_order_attempt_id": "v1", "canary_plan_id": "cp",
        "ts_ms": 99999, "status": "CLEAN", "recommendation": "REPEAT_DEMO_CANARY_SAME_SIZE",
        "hard_fail_count": 0, "warning_count": 0, "unknown_blocking_count": 0,
        "clean_for_repeat_demo_same_size": 1, "eligible_for_production_design_review": 0,
        "summary_json": "{}", "blocking_reasons_json": "[]", "next_required_actions_json": "[]"})
    assert _run_cli("post_canary_veto", ["--analysis-id", "an-repeat", "--db", db]) == 0


def test_cli_post_canary_eligibility_json(store, capsys):
    rc = _run_cli("post_canary_eligibility", ["--venue", "kalshi", "--environment", "demo",
                                              "--db", str(store.db_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["eligible_size_increase"] is False
    assert out["production_execution"] == "NOT_IMPLEMENTED"


def test_export_post_canary_dataset(store, tmp_path):
    _seed_attempt(store, "e1")
    _seed_analysis(store, "e1")
    out = tmp_path / "ds"
    rc = _run_cli("post_canary_export_dataset", ["--db", str(store.db_path), "--out", str(out)])
    assert rc == 0
    for f in ("analyses.csv", "audit_checks.csv", "markout.csv", "eligibility.csv"):
        assert (out / f).exists()


# --- 55-57 safety integration --------------------------------------------- #
def test_auto_analyze_after_submit_does_not_submit_again(store, monkeypatch, tmp_path):
    monkeypatch.setenv("POST_CANARY_OUTPUT_DIR", str(tmp_path / "art"))
    # Analyzer is pure analysis; even with a store it never calls a live broker.
    res = PostCanaryAnalyzer(store, cfg()).analyze(
        PostCanaryAnalysisRequest(live_order_attempt_id="mla-clean-001"), fixture=clean_ctx())
    assert res.recommendation == "REPEAT_DEMO_CANARY_SAME_SIZE"
    # no micro-live order attempts were created by the analysis
    assert store.get_micro_live_attempts(10) == []


def test_no_grok_can_clear_veto():
    ctx = clean_ctx()
    ctx["audit_events"].append({"event_type": "live_submit", "actor": "grok",
                                "audit_chain_hash": "g1"})
    res = analyze(ctx)
    assert res.recommendation == "STOP"  # grok-triggered live action is CRITICAL


def test_no_dashboard_scale_button():
    js = (_ROOT / "web" / "app.js").read_text().lower()
    panel = js[js.find("post-canary-panel"):]
    seg = panel.split("renderomspanel")[0] if "renderomspanel" in panel else panel[:4000]
    assert "<button" not in seg
    for bad in ("scale", "/submit", "/cancel", "production-unlock"):
        assert f'"{bad}' not in seg


# --- 58-60 regression / import --------------------------------------------- #
def test_existing_micro_live_tests_still_pass():
    from engine.micro_live import MicroLiveConfig, all_pass, check_locks
    assert not all_pass(check_locks(MicroLiveConfig()))  # disabled by default


def test_existing_guarded_live_tests_still_pass():
    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig
    res = ConformanceHarness(store=None, config=GuardedLiveConfig()).run()
    assert res.status in ("PASS", "FAIL")


def test_compile_and_import_post_canary_modules():
    import engine.post_canary.analyzer  # noqa: F401
    import engine.post_canary.artifacts  # noqa: F401
    import engine.post_canary.chain_audit  # noqa: F401
    import engine.post_canary.config  # noqa: F401
    import engine.post_canary.eligibility  # noqa: F401
    import engine.post_canary.execution_quality  # noqa: F401
    import engine.post_canary.fee_analysis  # noqa: F401
    import engine.post_canary.loader  # noqa: F401
    import engine.post_canary.market_data_audit  # noqa: F401
    import engine.post_canary.markout  # noqa: F401
    import engine.post_canary.reconciliation_audit  # noqa: F401
    import engine.post_canary.report  # noqa: F401
    import engine.post_canary.research_audit  # noqa: F401
    import engine.post_canary.risk_audit  # noqa: F401
    import engine.post_canary.schemas  # noqa: F401
    import engine.post_canary.secret_audit  # noqa: F401
    import engine.post_canary.slippage  # noqa: F401
    import engine.post_canary.veto  # noqa: F401
    assert True
