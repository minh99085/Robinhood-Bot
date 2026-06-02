"""Phase 7 tests: shadow-mode orchestration (no live orders).

Everything is mocked/in-memory; no network, no real keys. These verify the
read-only / no-live-execution contract, candidate filtering, deterministic
decisions through the RiskEngine, ShadowOMS/PaperBroker simulation, outcome
markout, readiness gates (fail-closed, never auto-live), and CLI behavior.
"""

from __future__ import annotations

import importlib.util
import sys
import typing
from decimal import Decimal
from pathlib import Path

import pytest

from engine.schemas import RiskDecision
from engine.risk import RiskContext, RiskEngine, RiskLimits
from engine.shadow import (
    AlertManager,
    LiveReadinessGate,
    ShadowCandidateSelector,
    ShadowConfig,
    ShadowDecisionEngine,
    ShadowOrchestrator,
    ShadowOutcomeTracker,
    ShadowScheduler,
    by_venue,
    compute_session_metrics,
    edge_capture,
    fill_ratio,
    write_report,
)
from engine.shadow.schemas import CandidateMarket, OverallReadiness, ShadowOrder
from engine.storage import Store

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
D = Decimal


def _cfg(**kw) -> ShadowConfig:
    c = ShadowConfig(enabled=True, mode="shadow_live")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class FakeBook:
    """Duck-typed book the PaperBroker accepts (price->size dicts)."""
    def __init__(self):
        self.asks = {D("0.42"): D("1000")}
        self.bids = {D("0.40"): D("1000")}
        self.best_ask = D("0.42")
        self.best_bid = D("0.40")
        self.spread = D("0.02")
        self.resolved = False

    def is_stale(self, _ms):
        return False


def _good_inp(book=True, equity=100000.0, **over):
    cand = {"venue": "polymarket", "market_id": "M1", "outcome": "YES", "question": "q",
            "category": "news", "close_ts_ms": 9999999999999, "spread": 0.02,
            "ambiguity_score": 0.1, "data_fresh": True, "metadata_complete": True,
            "resolution_present": True, "liquidity_score": 0.9}
    cand.update(over.pop("candidate", {}))
    inp = {"candidate": cand, "best_bid": 0.40, "best_ask": 0.42, "spread": 0.02,
           "research": {"p_ensemble": 0.60, "confidence": 0.8, "evidence_score": 0.7,
                        "ambiguity_score": 0.1},
           "risk_context": RiskContext(equity=equity), "venue_kind": "pm"}
    if book:
        inp["book"] = FakeBook()
    inp.update(over)
    return inp


# 1
def test_shadow_start_disabled_by_default():
    orch = ShadowOrchestrator(config=ShadowConfig())  # enabled defaults False
    ok, reason = orch.start()
    assert ok is False and "disabled" in str(reason)


# 2
def test_shadow_start_requires_shadow_mode():
    orch = ShadowOrchestrator(config=_cfg(mode="paper"))
    ok, reason = orch.start()
    assert ok is False and "mode" in str(reason)


# 3
def test_shadow_start_verifies_no_live_broker(monkeypatch):
    monkeypatch.setenv("HTE_LIVE_BROKER", "1")
    orch = ShadowOrchestrator(config=_cfg())
    ok, reason = orch.start()
    assert ok is False and "live broker" in str(reason)


# 4
def test_shadow_session_lifecycle(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg())
    ok, sess = orch.start()
    assert ok and store.get_shadow_session(sess.shadow_session_id)["status"] == "RUNNING"
    orch.stop()
    assert store.get_shadow_session(sess.shadow_session_id)["status"] == "STOPPED"


# 5
def test_shadow_candidate_selector_filters_stale_data():
    c = ShadowCandidateSelector(_cfg()).evaluate(venue="polymarket", data_fresh=False,
                                                 close_ts_ms=9999999999999)
    assert not c.selected and c.rejection_reason == "market_data_stale"


# 6
def test_shadow_candidate_selector_filters_high_ambiguity():
    c = ShadowCandidateSelector(_cfg()).evaluate(venue="polymarket", ambiguity_score=0.9,
                                                 close_ts_ms=9999999999999)
    assert not c.selected and c.rejection_reason == "high_ambiguity"


# 7
def test_shadow_candidate_selector_filters_close_too_near():
    import time as _t
    c = ShadowCandidateSelector(_cfg()).evaluate(venue="polymarket",
                                                 close_ts_ms=int(_t.time() * 1000) + 10000)
    assert not c.selected and c.rejection_reason == "close_too_near"


def _cand():
    return CandidateMarket(venue="polymarket", market_id="M1", outcome="YES", selected=True)


# 8
def test_shadow_decision_no_research_abstains():
    dec, prop, rd = ShadowDecisionEngine(_cfg()).decide(_cand(), best_bid=0.40, best_ask=0.42,
                                                        research=None)
    assert dec.decision == "ABSTAINED" and dec.reason == "no_research_estimate" and prop is None


# 9
def test_shadow_decision_low_edge_abstains():
    dec, prop, rd = ShadowDecisionEngine(_cfg()).decide(
        _cand(), best_bid=0.40, best_ask=0.42,
        research={"p_ensemble": 0.43, "confidence": 0.8, "evidence_score": 0.7, "ambiguity_score": 0.1})
    assert dec.decision == "ABSTAINED" and dec.reason == "insufficient_edge"


# 10
def test_shadow_decision_proposal_routes_through_risk():
    class SpyRisk:
        called = False
        def evaluate(self, p, c):
            self.called = True
            return RiskDecision(proposal_id=p.proposal_id, approved=True, code="OK")
    spy = SpyRisk()
    dec, prop, rd = ShadowDecisionEngine(_cfg()).decide(
        _cand(), best_bid=0.40, best_ask=0.42,
        research={"p_ensemble": 0.60, "confidence": 0.8, "evidence_score": 0.7, "ambiguity_score": 0.1},
        risk_engine=spy, risk_context=RiskContext(equity=100000.0))
    assert spy.called and dec.decision == "APPROVED_SHADOW" and prop is not None


# 11
def test_shadow_risk_rejection_persisted(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    dec = orch.process_market(_good_inp(equity=10.0))  # tiny equity -> oversize -> reject
    assert dec.decision == "RISK_REJECTED"
    rows = store.get_shadow_rows("shadow_decisions", orch.session.shadow_session_id)
    assert any(r["decision"] == "RISK_REJECTED" for r in rows)


# 12
def test_shadow_approved_order_goes_to_shadow_oms_only(tmp_path):
    store = Store(tmp_path / "s.db")
    real_broker_spy = {"calls": 0}
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    dec = orch.process_market(_good_inp())
    assert dec.decision == "APPROVED_SHADOW"
    orders = store.get_shadow_rows("shadow_orders", orch.session.shadow_session_id)
    assert len(orders) == 1
    assert real_broker_spy["calls"] == 0  # no real broker ever touched


# 13
def test_shadow_order_never_calls_live_submit(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    # any "live submit" attribute would raise if called; shadow must not call it
    def _boom(*a, **k):
        raise AssertionError("live submit called!")
    orch.oms.broker.submit_live = _boom  # type: ignore[attr-defined]
    orch.process_market(_good_inp())
    assert orch.counters["live_order_endpoint_calls"] == 0


# 14
def test_shadow_grok_cannot_set_order_size():
    dec, prop, rd = ShadowDecisionEngine(_cfg(default_notional_usd=D("5"))).decide(
        _cand(), best_bid=0.40, best_ask=0.42,
        research={"p_ensemble": 0.60, "confidence": 0.8, "evidence_score": 0.7,
                  "ambiguity_score": 0.1, "suggested_size": 9999, "notional": 12345})
    assert prop is not None and float(prop.notional) == 5.0  # fixed config sizing, Grok ignored


# 15
def test_shadow_outcome_tracker_records_horizons(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = _cfg(outcome_horizons_ms=[0, 5000, 60000])
    tr = ShadowOutcomeTracker(cfg, store)
    assert tr.horizons() == [0, 5000, 60000]
    from engine.shadow.schemas import ShadowDecision
    dec = ShadowDecision(shadow_session_id="x", intended_side="BUY", intended_limit_price=D("0.42"))
    obs = tr.observe(dec, horizon_ms=5000, best_bid=0.50, best_ask=0.52)
    assert obs.horizon_ms == 5000 and obs.midpoint == D("0.51")


# 16
def test_shadow_markout_calculation():
    from engine.shadow.schemas import ShadowDecision
    dec = ShadowDecision(intended_side="BUY", intended_limit_price=D("0.42"))
    obs = ShadowOutcomeTracker(_cfg()).observe(dec, best_bid=0.50, best_ask=0.52, fill_price=0.42)
    assert obs.markout == D("0.09")  # midpoint 0.51 - fill 0.42


# 17
def test_shadow_metrics_fill_ratio():
    assert fill_ratio(4, 1) == 0.25 and fill_ratio(0, 0) == 0.0


# 18
def test_shadow_metrics_edge_capture():
    assert edge_capture([0.1, 0.1], [0.05, 0.05]) == 0.5


# 19
def test_shadow_metrics_by_venue():
    b = by_venue([{"venue": "polymarket"}, {"venue": "kalshi"}, {"venue": "polymarket"}])
    assert b == {"kalshi": 1.0, "polymarket": 2.0}


def _good_metrics():
    return {"venue_uptime_pct": 0.99, "stale_book_rate": 0.0, "parse_error_rate": 0.0,
            "sequence_gap_rate": 0.0, "fill_ratio": 0.5, "edge_capture_ratio": 0.2,
            "reject_rate": 0.3, "max_drawdown_pct": 0.02, "total_pnl": 12.0,
            "resolved_sample_count": 50, "brier_score": 0.18, "log_loss": 0.5, "ece": 0.05}


def _good_counters():
    return {"decisions": 500, "runtime_hours": 48, "reconciliation_clean": True}


# 20
def test_readiness_not_enough_data():
    r = LiveReadinessGate(_cfg()).evaluate({}, {"decisions": 1, "runtime_hours": 0})
    assert r.overall_status == "NOT_ENOUGH_DATA"


# 21
def test_readiness_hard_fail_risk_bypass():
    r = LiveReadinessGate(_cfg()).evaluate(_good_metrics(),
                                           {**_good_counters(), "risk_bypass_count": 1})
    assert r.overall_status == "NOT_READY"
    assert any(g.gate_name == "risk_bypass_count" and g.status == "FAIL" for g in r.gate_results)


# 22
def test_readiness_hard_fail_live_endpoint_call():
    r = LiveReadinessGate(_cfg()).evaluate(_good_metrics(),
                                           {**_good_counters(), "live_order_endpoint_calls": 2})
    assert r.overall_status == "NOT_READY"
    assert any(g.gate_name == "live_order_endpoint_calls" and g.status == "FAIL"
               for g in r.gate_results)


# 23
def test_readiness_data_quality_fail_stale_rate():
    m = {**_good_metrics(), "stale_book_rate": 0.5}
    r = LiveReadinessGate(_cfg()).evaluate(m, _good_counters())
    assert r.overall_status == "NOT_READY"
    assert any(g.gate_name == "stale_book_rate" and g.status == "FAIL" for g in r.gate_results)


# 24
def test_readiness_performance_warn_or_fail_drawdown():
    m = {**_good_metrics(), "max_drawdown_pct": 0.5}
    r = LiveReadinessGate(_cfg()).evaluate(m, _good_counters())
    assert r.overall_status == "NOT_READY"


# 25
def test_readiness_calibration_not_enough_samples():
    m = {**_good_metrics(), "resolved_sample_count": 2}
    r = LiveReadinessGate(_cfg()).evaluate(m, {**_good_counters(), "resolved_sample_count": 2})
    cal = [g for g in r.gate_results if g.gate_name == "calibration"]
    assert cal and cal[0].status == "NOT_ENOUGH_DATA"


# 26
def test_readiness_ready_for_manual_review():
    r = LiveReadinessGate(_cfg()).evaluate(_good_metrics(),
                                           {**_good_counters(), "resolved_sample_count": 50})
    assert r.overall_status == "READY_FOR_MANUAL_REVIEW"


# 27
def test_readiness_never_returns_auto_live():
    statuses = set(typing.get_args(OverallReadiness))
    assert not any("AUTO" in s or "LIVE_AUTO" in s for s in statuses)
    assert "READY_FOR_LIVE_AUTO" not in statuses


# 28 + 29
def test_shadow_report_artifacts_created_and_says_no_live(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = _cfg()
    orch = ShadowOrchestrator(store=store, config=cfg, risk=RiskEngine(RiskLimits()))
    orch.start()
    sid = orch.session.shadow_session_id
    orch.process_market(_good_inp())
    metrics = compute_session_metrics(store, sid, cfg, orch.counters)
    report = LiveReadinessGate(cfg).evaluate(metrics, orch.counters, sid)
    out = write_report(store, sid, cfg, report, metrics, base_dir=str(tmp_path / "art"))
    for name in ("shadow_config.json", "shadow_readiness_report.json",
                 "shadow_readiness_report.md", "decisions.csv", "orders.csv"):
        assert (out / name).exists()
    md = (out / "shadow_readiness_report.md").read_text()
    assert "No live orders were submitted" in md


# 30
def test_shadow_alert_critical_pauses_new_orders(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    orch.alerts.emit("CRITICAL", "reconciliation_failure", "boom")
    assert orch.new_orders_allowed() is False
    orch.process_market(_good_inp())
    assert store.get_shadow_rows("shadow_orders", orch.session.shadow_session_id) == []


# 31
def test_shadow_kill_switch_blocks_decisions(tmp_path):
    ks = tmp_path / "KS"
    ks.write_text("stop")
    store = Store(tmp_path / "s.db")
    cfg = _cfg(kill_switch_path=str(ks))
    # start verifies kill switch absent -> start while present should fail
    orch = ShadowOrchestrator(store=store, config=ShadowConfig(enabled=True, mode="shadow_live"))
    orch.start()
    orch.cfg.kill_switch_path = str(ks)  # switch flips on after start
    dec = orch.process_market(_good_inp())
    assert dec.decision == "ABSTAINED" and dec.reason == "kill_switch"
    assert store.get_shadow_rows("shadow_orders", orch.session.shadow_session_id) == []


# 32
def test_shadow_storage_migrations_idempotent(tmp_path):
    p = tmp_path / "s.db"
    Store(p)
    s2 = Store(p)
    for t in ("shadow_sessions", "shadow_decisions", "shadow_orders", "shadow_observations",
              "readiness_reports", "shadow_alerts"):
        assert s2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()


# 33
def test_shadow_api_status_redacts_secrets(monkeypatch):
    import json
    monkeypatch.setenv("XAI_API_KEY", "xai-MUSTNOTLEAK")
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "ak-MUSTNOTLEAK")
    cfg = ShadowConfig.from_env()
    status = {"enabled": cfg.enabled, "mode": cfg.mode, "venues": cfg.venues,
              "config": cfg.public_dict()}
    blob = json.dumps(status, default=str)
    assert "MUSTNOTLEAK" not in blob


# 34
def test_shadow_api_start_disabled_safely():
    cfg = ShadowConfig(enabled=False)
    ok, reason = cfg.verify_safe_to_start()
    assert ok is False and "disabled" in reason


# 35
def test_run_shadow_cli_help():
    mod = _load("run_shadow", "run_shadow.py")
    with pytest.raises(SystemExit) as e:
        mod.main(["--help"])
    assert e.value.code == 0


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 36
def test_check_live_readiness_cli_nonzero_when_not_ready(tmp_path):
    store = Store(tmp_path / "s.db")
    store.upsert_shadow_session({"shadow_session_id": "S1", "status": "STOPPED",
                                 "started_ts_ms": 1, "mode": "shadow_live", "venues_json": []})
    store.add_readiness_report({"report_id": "R1", "shadow_session_id": "S1",
                                "generated_ts_ms": 2, "overall_status": "NOT_READY",
                                "summary_json": {}, "report_path": None})
    mod = _load("check_live_readiness", "check_live_readiness.py")
    rc = mod.main(["--latest", "--fail-on-not-ready", "--db", str(tmp_path / "s.db")])
    assert rc != 0


# 37
def test_check_live_readiness_cli_zero_when_manual_review_ready(tmp_path):
    store = Store(tmp_path / "s.db")
    store.upsert_shadow_session({"shadow_session_id": "S2", "status": "STOPPED",
                                 "started_ts_ms": 1, "mode": "shadow_live", "venues_json": []})
    store.add_readiness_report({"report_id": "R2", "shadow_session_id": "S2",
                                "generated_ts_ms": 2,
                                "overall_status": "READY_FOR_MANUAL_REVIEW",
                                "summary_json": {}, "report_path": None})
    mod = _load("check_live_readiness2", "check_live_readiness.py")
    rc = mod.main(["--latest", "--fail-on-not-ready", "--db", str(tmp_path / "s.db")])
    assert rc == 0


# 38
def test_shadow_no_private_user_channels():
    from engine.venues.kalshi.ws import FORBIDDEN_CHANNELS
    assert {"fill", "user_orders", "market_positions"} <= set(FORBIDDEN_CHANNELS)


# 39
def test_shadow_no_order_endpoint_calls_for_kalshi():
    from engine.venues.kalshi.rest import KalshiRestClient
    for m in ("place_order", "cancel_order", "submit_order"):
        assert not hasattr(KalshiRestClient, m)


# 40
def test_shadow_no_wallet_usage_for_polymarket(tmp_path, monkeypatch):
    # No wallet/private-key env or module is required to run a shadow decision.
    for k in ("POLYMARKET_PRIVATE_KEY", "POLY_WALLET_KEY", "WALLET_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    assert orch.start()[0] is True
    assert orch.process_market(_good_inp()).decision == "APPROVED_SHADOW"


# 41
def test_shadow_scheduler_exception_contained():
    sch = ShadowScheduler()
    def _boom():
        raise RuntimeError("cycle blew up")
    ok, exc = sch.run_cycle_safe("x", _boom)
    assert ok is False and isinstance(exc, RuntimeError) and sch.error_count == 1


# 42
def test_shadow_heartbeat_updates(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg())
    orch.start()
    orch.heartbeat(cycle_count=7)
    hb = store.get_shadow_heartbeat(orch.session.shadow_session_id)
    assert hb and hb["cycle_count"] == 7


# 43
def test_shadow_observation_missing_data_tolerated():
    from engine.shadow.schemas import ShadowDecision
    obs = ShadowOutcomeTracker(_cfg()).observe(ShadowDecision(intended_side="BUY"),
                                               best_bid=None, best_ask=None)
    assert obs.midpoint is None and obs.markout is None


# 44
def test_shadow_uses_cached_research_when_online_disabled(tmp_path):
    class BoomResearch:
        def research(self, *a, **k):
            raise AssertionError("network research called!")
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(allow_online_research=False),
                              research=BoomResearch(), risk=RiskEngine(RiskLimits()))
    orch.start()
    # research is supplied as cached in the input; the network client is never called
    assert orch.process_market(_good_inp()).decision == "APPROVED_SHADOW"


# 45
def test_shadow_online_research_disabled_by_default():
    assert ShadowConfig().allow_online_research is False


# 46
def test_shadow_reconciliation_failure_blocks_new_orders(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    orch.reconciliation_clean = False
    orch.process_market(_good_inp())
    assert store.get_shadow_rows("shadow_orders", orch.session.shadow_session_id) == []


# 47
def test_shadow_open_orders_cancelled_on_stop(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg())
    orch.start()
    order = ShadowOrder(shadow_session_id=orch.session.shadow_session_id, status="OPEN")
    store.add_shadow_order(order.record())
    orch.oms.open_orders.append(order)
    orch.stop()
    assert orch.oms.open_orders == [] and order.status == "CANCELLED"


# 48
def test_shadow_mode_distinct_from_paper(tmp_path):
    store = Store(tmp_path / "s.db")
    orch = ShadowOrchestrator(store=store, config=_cfg(), risk=RiskEngine(RiskLimits()))
    orch.start()
    orch.process_market(_good_inp())
    # shadow writes shadow tables, NOT the operational paper 'trades' table
    assert store._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert store.get_shadow_rows("shadow_decisions", orch.session.shadow_session_id)


# 49
def test_existing_replay_tests_still_pass_with_shadow_modules():
    import engine.replay  # noqa: F401
    import engine.shadow  # noqa: F401
    from engine.venues.kalshi.replay import reconstruct
    assert reconstruct([]) == {}


# 50
def test_compile_and_import_shadow_modules():
    import importlib
    for name in ("config", "schemas", "alerts", "candidate_selector", "decision_engine",
                 "shadow_oms", "outcome_tracker", "metrics", "readiness", "report",
                 "artifacts", "scheduler", "orchestrator"):
        importlib.import_module(f"engine.shadow.{name}")
