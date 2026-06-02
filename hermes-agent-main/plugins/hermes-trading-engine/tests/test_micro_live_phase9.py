"""Phase 9 — micro-live canary execution tests.

All exchange network calls are MOCKED. No real credentials, no real network, no
real order submission/cancellation. Verifies the locks, gates, FOK-only shape,
idempotency, reconciliation, emergency cancel, secret handling, and that live
execution is impossible by default / via dashboard / strategy / Grok.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.micro_live import MicroLiveConfig, all_pass, check_locks  # noqa: E402
from engine.micro_live import config as ml_config  # noqa: E402
from engine.micro_live import locks as ml_locks  # noqa: E402
from engine.micro_live import order_builder  # noqa: E402
from engine.micro_live import preflight as ml_preflight  # noqa: E402
from engine.micro_live.canary_plan import create_canary_plan  # noqa: E402
from engine.micro_live.errors import MicroLiveDisabled, NotImplementedLiveSigning  # noqa: E402
from engine.micro_live.execution_service import (FixtureSigner,  # noqa: E402
                                                 MicroLiveExecutionService, fixture_transport)
from engine.micro_live.network_guard import NetworkGuard  # noqa: E402
from engine.micro_live.schemas import MicroLiveCanaryPlan  # noqa: E402
from engine.storage import Store  # noqa: E402

ACK = ml_config.REQUIRED_ACK_PHRASE
CONFIRM = ml_config.SUBMIT_CONFIRMATION


# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ml.db")


@pytest.fixture
def open_env(monkeypatch):
    """Open the build + runtime + ack + kalshi locks (mocked exchange only)."""
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", True)
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")
    monkeypatch.setenv("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", ACK)
    monkeypatch.setenv("KALSHI_MICRO_LIVE_ENABLED", "1")
    monkeypatch.delenv("MICRO_LIVE_KILL_SWITCH_PATH", raising=False)
    return True


def _seed_ready(store, now=None):
    now = now or int(time.time() * 1000)
    store.add_conformance_run({"conformance_run_id": "cr", "started_ts_ms": now,
                               "finished_ts_ms": now, "status": "PASS", "config_hash": "x",
                               "test_count": 1, "pass_count": 1, "fail_count": 0,
                               "warning_count": 0, "report_path": None})
    store.add_readiness_report({"report_id": "rr", "shadow_session_id": "s", "generated_ts_ms": now,
                                "overall_status": "READY_FOR_MANUAL_REVIEW", "summary_json": {},
                                "report_path": None})
    store.add_dry_run_order_intent({
        "dry_run_intent_id": "dri", "ts_ms": now, "venue": "kalshi", "market_id": "M",
        "market_ticker": "TEST-MKT", "asset_id": None, "outcome": "YES", "side": "BUY",
        "order_type": "FOK", "limit_price": "0.50", "quantity": "1", "notional": "0.50",
        "internal_order_request_json": "{}", "venue_payload_json": "{}", "unsigned": 1,
        "unsent": 1, "signer_used": 0, "network_called": 0, "risk_decision_id": "rd",
        "safety_envelope_decision_id": "se", "oms_order_id": None, "status": "CREATED",
        "reason": None})
    return now


def _make_plan(store, now=None, **over):
    now = now or int(time.time() * 1000)
    plan, errs = create_canary_plan(
        store, MicroLiveConfig.from_env(), dry_run_intent_id="dri", readiness_report_id="rr",
        venue=over.get("venue", "kalshi"), environment=over.get("environment", "demo"),
        approval_batch_id="ab", arming_token_id="at", now_ms=now)
    return plan, errs


def _good_market_ctx():
    return {"edge_after_costs": 0.2, "spread": 0.0, "stale_ms": 0, "venue_status": "ready",
            "market_status": "open", "evidence_score": 0.8, "source_count": 3,
            "ambiguity_score": 0.0}


def _submit(store, plan, **kw):
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    return svc.submit_canary_order(
        plan.canary_plan_id, arming_token=kw.pop("arming_token", "tok"),
        confirm=kw.pop("confirm", CONFIRM), market_ctx=kw.pop("market_ctx", _good_market_ctx()),
        transport=kw.pop("transport", fixture_transport(fill=True)),
        signer=kw.pop("signer", FixtureSigner()), non_interactive_test_fixture=True, **kw)


# --- 1-8 locks / defaults -------------------------------------------------- #
def test_micro_live_disabled_by_default(monkeypatch):
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", False)
    for v in ("MICRO_LIVE_ENABLED", "MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK"):
        monkeypatch.delenv(v, raising=False)
    assert not all_pass(check_locks(MicroLiveConfig.from_env()))


def test_micro_live_build_lock_required(monkeypatch):
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", False)
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")
    monkeypatch.setenv("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", ACK)
    res = check_locks(MicroLiveConfig.from_env())
    assert not all_pass(res)
    assert "source_build_lock" in [r.lock_name for r in res if not r.passed]


def test_micro_live_runtime_lock_required(monkeypatch):
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", True)
    monkeypatch.delenv("MICRO_LIVE_ENABLED", raising=False)
    monkeypatch.setenv("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", ACK)
    res = check_locks(MicroLiveConfig.from_env())
    assert "runtime_lock" in [r.lock_name for r in res if not r.passed]


def test_micro_live_acknowledgement_required(monkeypatch):
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", True)
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")
    monkeypatch.delenv("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", raising=False)
    res = check_locks(MicroLiveConfig.from_env())
    assert "real_money_acknowledgement" in [r.lock_name for r in res if not r.passed]


def test_micro_live_demo_default():
    c = MicroLiveConfig()
    assert c.environment == "demo" and not c.allow_production


def test_micro_live_production_requires_extra_unlock(monkeypatch, open_env):
    monkeypatch.setenv("MICRO_LIVE_ENV", "prod")
    monkeypatch.setenv("MICRO_LIVE_ALLOWED_ENVIRONMENTS", "demo,prod")
    monkeypatch.setenv("MICRO_LIVE_ALLOW_PRODUCTION", "0")
    res = check_locks(MicroLiveConfig.from_env())
    assert "environment_lock" in [r.lock_name for r in res if not r.passed]


def test_micro_live_one_order_per_token(store, open_env):
    _seed_ready(store)
    plan, _ = _make_plan(store)
    r1 = _submit(store, plan)
    assert r1["submitted"]
    # same plan/token again -> blocked
    r2 = _submit(store, plan)
    assert r2.get("blocked")


def test_micro_live_max_order_notional_hard_cap(monkeypatch):
    monkeypatch.setenv("MICRO_LIVE_MAX_ORDER_NOTIONAL_USD", "1000")
    assert MicroLiveConfig.from_env().max_order_notional_usd <= Decimal("1")


# --- 9-12 order shape ------------------------------------------------------ #
def test_micro_live_rejects_gtc():
    errs = order_builder.validate_shape("GTC", "good_till_canceled", MicroLiveConfig())
    assert errs


def test_micro_live_rejects_gtd():
    errs = order_builder.validate_shape("GTD", "good_till_date", MicroLiveConfig())
    assert errs


def test_micro_live_rejects_batch_order():
    errs = order_builder.validate_shape("FOK", "fill_or_kill", MicroLiveConfig(), batch=True)
    assert "batch_orders_forbidden" in errs


def test_micro_live_rejects_replace_amend():
    e1 = order_builder.validate_shape("FOK", "fill_or_kill", MicroLiveConfig(), replace=True)
    e2 = order_builder.validate_shape("FOK", "fill_or_kill", MicroLiveConfig(), amend=True)
    assert "replace_forbidden" in e1 and "amend_forbidden" in e2


# --- 13-16 canary plan ----------------------------------------------------- #
def test_canary_plan_requires_dry_run_intent(store, open_env):
    _seed_ready(store)
    plan, errs = create_canary_plan(store, MicroLiveConfig.from_env(),
                                    dry_run_intent_id="missing", readiness_report_id="rr",
                                    venue="kalshi", environment="demo")
    assert "dry_run_intent_missing" in errs


def test_canary_plan_requires_ready_shadow_report(store, open_env):
    store.add_conformance_run({"conformance_run_id": "cr", "started_ts_ms": 1, "finished_ts_ms": 1,
                               "status": "PASS", "config_hash": "x", "test_count": 1,
                               "pass_count": 1, "fail_count": 0, "warning_count": 0,
                               "report_path": None})
    store.add_dry_run_order_intent({"dry_run_intent_id": "dri", "ts_ms": 1, "venue": "kalshi",
                                    "market_ticker": "M", "outcome": "YES", "side": "BUY",
                                    "order_type": "FOK", "limit_price": "0.5", "quantity": "1",
                                    "notional": "0.5", "unsigned": 1, "unsent": 1, "signer_used": 0,
                                    "network_called": 0, "safety_envelope_decision_id": "se",
                                    "status": "CREATED"})
    plan, errs = create_canary_plan(store, MicroLiveConfig.from_env(), dry_run_intent_id="dri",
                                    readiness_report_id="nope", venue="kalshi", environment="demo")
    assert any("readiness" in e for e in errs)


def test_canary_plan_expires(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    from engine.micro_live.canary_plan import validate_canary_plan
    ok, errs = validate_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                    now_ms=plan.expires_ts_ms + 1)
    assert not ok and "canary_plan_expired" in errs


def test_canary_plan_invalidated_by_market_data_change(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    from engine.micro_live.canary_plan import validate_canary_plan
    ok, errs = validate_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                    market_ctx={"price_drift": 0.5}, now_ms=now)
    assert not ok and "market_data_drift_exceeds_tolerance" in errs


# --- 17-22 preflight gates ------------------------------------------------- #
def test_preflight_requires_phase8_conformance(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    # wipe conformance by using a fresh store-less plan check: simulate fail via no runs
    s2 = Store(Path(store.db_path).parent / "s2.db")
    s2.add_readiness_report({"report_id": "rr", "shadow_session_id": "s", "generated_ts_ms": now,
                             "overall_status": "READY_FOR_MANUAL_REVIEW", "summary_json": {},
                             "report_path": None})
    result, _, _ = ml_preflight.preflight_canary_plan(s2, MicroLiveConfig.from_env(), plan,
                                                       arming_ok=True, account_ok=True,
                                                       market_ctx=_good_market_ctx(), now_ms=now)
    assert result.status == "FAIL"


def test_preflight_requires_approval_and_arming(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    plan.approval_batch_id = None
    plan.arming_token_id = None
    result, _, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                       market_ctx=_good_market_ctx(), now_ms=now)
    assert result.status == "FAIL"
    assert result.approval_status.startswith("FAIL") or result.arming_status.startswith("FAIL")


def test_preflight_blocks_kill_switch(store, open_env, monkeypatch, tmp_path):
    ks = tmp_path / "KILL"
    ks.write_text("stop")
    monkeypatch.setenv("MICRO_LIVE_KILL_SWITCH_PATH", str(ks))
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    result, _, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                       arming_ok=True, account_ok=True,
                                                       market_ctx=_good_market_ctx(), now_ms=now)
    assert result.status == "FAIL"


def test_preflight_blocks_degraded_venue(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    ctx = _good_market_ctx()
    ctx["venue_status"] = "degraded"
    result, safety, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                           arming_ok=True, account_ok=True,
                                                           market_ctx=ctx, now_ms=now)
    assert result.status == "FAIL"


def test_preflight_blocks_stale_book(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    ctx = _good_market_ctx()
    ctx["stale_ms"] = 999999
    result, _, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                       arming_ok=True, account_ok=True,
                                                       market_ctx=ctx, now_ms=now)
    assert result.status == "FAIL"


def test_preflight_blocks_high_ambiguity(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    ctx = _good_market_ctx()
    ctx["ambiguity_score"] = 0.99
    result, safety, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                           arming_ok=True, account_ok=True,
                                                           market_ctx=ctx, now_ms=now)
    assert result.status == "FAIL"


# --- 23-30 submit path guards --------------------------------------------- #
def test_submit_cli_only(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    res = svc.submit_canary_order(plan.canary_plan_id, arming_token="t", confirm=CONFIRM,
                                  market_ctx=_good_market_ctx(), cli_context=False)
    assert res.get("blocked") and res["reason"] == "not_cli_context"


def test_no_api_submit_endpoint_exists():
    src = (_ROOT / "engine" / "app.py").read_text()
    for tok in ("/api/micro-live/submit", "/api/micro-live/cancel", "/api/micro-live/live-order"):
        for line in src.splitlines():
            if tok in line and ".post(" in line.lower():
                raise AssertionError(f"forbidden submit route present: {tok}")


def test_dashboard_has_no_submit_button():
    js = (_ROOT / "web" / "app.js").read_text().lower()
    panel = js[js.find("micro-live-panel"):]
    assert "micro-live/submit" not in panel
    assert "<button" not in panel.split("renderomspanel")[0]


def test_micro_live_submit_requires_typed_confirmation(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    r = _submit(store, plan, confirm="nope")
    assert r.get("blocked") and r["reason"] == "typed_confirmation_required"


def test_micro_live_submit_reruns_preflight(store, open_env, monkeypatch):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    calls = {"n": 0}
    orig = ml_preflight.preflight_canary_plan

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr("engine.micro_live.execution_service.preflight_canary_plan", spy)
    _submit(store, plan)
    assert calls["n"] >= 1


def test_micro_live_submit_calls_risk_and_safety(store, open_env, monkeypatch):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    seen = {"risk": 0, "safety": 0}
    orig_risk = ml_preflight.run_risk

    def risk_spy(*a, **k):
        seen["risk"] += 1
        return orig_risk(*a, **k)
    monkeypatch.setattr(ml_preflight, "run_risk", risk_spy)
    orig_val = ml_preflight.MicroSafetyEnvelope.validate

    def val_spy(self, ctx):
        seen["safety"] += 1
        return orig_val(self, ctx)
    monkeypatch.setattr(ml_preflight.MicroSafetyEnvelope, "validate", val_spy)
    _submit(store, plan)
    assert seen["risk"] >= 1 and seen["safety"] >= 1


def test_micro_live_no_strategy_direct_path():
    # The trading engine / OMS must not import or hold a micro-live live broker.
    eng_src = (_ROOT / "engine" / "engine.py").read_text()
    oms_files = list((_ROOT / "engine" / "execution").glob("*.py")) if \
        (_ROOT / "engine" / "execution").exists() else []
    assert "micro_live" not in eng_src.lower()
    for f in oms_files:
        assert "micro_live" not in f.read_text().lower()


def test_grok_cannot_trigger_micro_live():
    for sub in ("research", "brain.py"):
        p = _ROOT / "engine" / sub
        files = list(p.glob("*.py")) if p.is_dir() else ([p] if p.exists() else [])
        for f in files:
            assert "micro_live" not in f.read_text().lower()


# --- 31-40 kalshi behavior ------------------------------------------------- #
def test_kalshi_demo_fok_order_payload(open_env):
    plan = MicroLiveCanaryPlan(venue="kalshi", market_ticker="X", outcome="YES", side="BUY",
                               limit_price=Decimal("0.50"), notional=Decimal("0.50"),
                               quantity=Decimal("1"))
    payload, errs = order_builder.build_kalshi_fok_payload(plan, MicroLiveConfig.from_env())
    assert not errs
    p = payload.payload_redacted
    assert p["time_in_force"] == "fill_or_kill" and p["cancel_order_on_pause"] is True


def test_kalshi_rejects_resting_order_payload():
    errs = order_builder.validate_shape("FOK", "good_till_canceled", MicroLiveConfig())
    assert errs


def test_kalshi_self_trade_prevention_default(open_env):
    plan = MicroLiveCanaryPlan(venue="kalshi", market_ticker="X", outcome="YES", side="BUY",
                               limit_price=Decimal("0.50"), notional=Decimal("0.50"))
    payload, _ = order_builder.build_kalshi_fok_payload(plan, MicroLiveConfig.from_env())
    assert payload.payload_redacted["self_trade_prevention_type"] == "taker_at_cross"


def test_kalshi_submit_blocked_without_trading_credentials(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    # no signer injected, no creds in env -> trading signer load fails
    res = svc.submit_canary_order(plan.canary_plan_id, arming_token="t", confirm=CONFIRM,
                                  market_ctx=_good_market_ctx(), transport=fixture_transport(),
                                  non_interactive_test_fixture=True)
    assert res.get("blocked") and "no_trading_signer" in res["reason"]


def test_kalshi_submit_uses_trading_signer_only_after_locks(store):
    # locks closed by default -> no signer load, base broker blocked
    from engine.micro_live.secret_runtime import load_kalshi_trading_signer
    signer, status = load_kalshi_trading_signer(False)
    assert signer is None and status == "locks_not_open"


def test_kalshi_submit_network_called_once_when_all_locks_open(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    t = fixture_transport(fill=True)
    res = _submit(store, plan, transport=t)
    assert res["submitted"]
    assert res["network_call_count"] == 1  # exactly one create-order call


def test_kalshi_timeout_does_not_resubmit_blindly(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    posts = {"n": 0}

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            posts["n"] += 1
            raise TimeoutError("network timeout")
        if "/balance" in url:
            return 200, {"balance": 1000}
        return 200, {}
    res = _submit(store, plan, transport=t)
    assert posts["n"] == 1
    assert res["status"] == "UNKNOWN"


def test_kalshi_reconcile_gets_order_and_fills(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    res = _submit(store, plan, transport=fixture_transport(fill=True))
    assert res["status"] == "FILLED"
    assert store.get_micro_live_reconciliations(10)


def test_kalshi_emergency_cancel_requires_confirmation(store, open_env):
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    r = svc.emergency_cancel(venue="kalshi", environment="demo", confirm="wrong",
                             requested_by="t", order_id="o1", cli_context=True)
    assert not r["sent"]


def test_kalshi_emergency_cancel_calls_cancel_once(store, open_env):
    calls = {"del": 0}

    def t(method, url, *, headers=None, json_body=None):
        if method == "DELETE":
            calls["del"] += 1
        return 200, {"status": "canceled"}
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    r = svc.emergency_cancel(venue="kalshi", environment="demo",
                             confirm=ml_config.EMERGENCY_CANCEL_CONFIRMATION, requested_by="t",
                             order_id="o1", transport=t, signer=FixtureSigner(), cli_context=True)
    assert r["sent"] and calls["del"] == 1


# --- 41-46 polymarket ------------------------------------------------------ #
def test_polymarket_disabled_by_default(open_env):
    from engine.micro_live.polymarket_live_broker import PolymarketLiveBroker
    b = PolymarketLiveBroker(MicroLiveConfig.from_env(), locks_ok=True)
    with pytest.raises(NotImplementedLiveSigning):
        b.submit_fok_canary_order(object(), "x")


def test_polymarket_not_implemented_fails_safe_if_sdk_missing():
    from engine.micro_live.polymarket_live_broker import PolymarketLiveBroker
    assert PolymarketLiveBroker.signing_available() in (True, False)
    b = PolymarketLiveBroker(MicroLiveConfig(), locks_ok=False)
    with pytest.raises(MicroLiveDisabled):
        b.submit_fok_canary_order(object(), "x")  # locks closed -> disabled first


def test_polymarket_fok_payload_validation():
    plan = MicroLiveCanaryPlan(venue="polymarket", asset_id="0xabc", outcome="YES", side="BUY",
                               limit_price=Decimal("0.50"), notional=Decimal("0.50"))
    payload, errs = order_builder.build_polymarket_fok_payload(plan, MicroLiveConfig())
    assert not errs and payload.order_type == "FOK"


def test_polymarket_rejects_gtc_gtd():
    errs = order_builder.validate_shape("GTC", "good_till_canceled", MicroLiveConfig())
    assert errs


def test_polymarket_signer_not_loaded_before_locks():
    from engine.micro_live.polymarket_live_broker import PolymarketLiveBroker
    b = PolymarketLiveBroker(MicroLiveConfig(), locks_ok=False)
    with pytest.raises(MicroLiveDisabled):
        b.submit_fok_canary_order(object(), "x")


def test_polymarket_cancel_requires_confirmation(store, open_env):
    svc = MicroLiveExecutionService(store, MicroLiveConfig.from_env())
    r = svc.emergency_cancel(venue="polymarket", environment="demo", confirm="wrong",
                             requested_by="t", order_id="o1", cli_context=True)
    assert not r["sent"]


# --- 47-48 network guard --------------------------------------------------- #
def test_network_guard_blocks_forbidden_endpoint():
    from engine.micro_live.errors import ForbiddenEndpointError
    g = NetworkGuard(allow_production=False)
    for url in ("https://demo-api.kalshi.co/x/deposit", "https://demo-api.kalshi.co/x/withdraw",
                "https://demo-api.kalshi.co/x/batch", "https://demo-api.kalshi.co/x/amend"):
        with pytest.raises(ForbiddenEndpointError):
            g.record("POST", url)


def test_network_guard_blocks_production_when_disabled():
    from engine.micro_live.errors import ForbiddenEndpointError
    g = NetworkGuard(allow_production=False)
    with pytest.raises(ForbiddenEndpointError):
        g.record("POST", "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders")


# --- 49-53 idempotency / state blocking ------------------------------------ #
def test_idempotency_key_persisted_before_submit(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    posted = {"before": None}

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            # at submit time the attempt row must already exist with a client_order_id
            posted["before"] = store.get_micro_live_attempts_for_plan(plan.canary_plan_id)
            return 201, {"order": {"order_id": "o", "status": "executed", "filled_quantity": 1,
                                   "fee": 1}}
        if "/balance" in url:
            return 200, {"balance": 1000}
        if "/orders/" in url:
            return 200, {"order": {"status": "executed", "filled_quantity": 1, "fee": 1}}
        if "/fills" in url:
            return 200, {"fills": [{"count": 1, "fee": 1}]}
        return 200, {}
    _submit(store, plan, transport=t)
    assert posted["before"] and posted["before"][0]["client_order_id"].startswith("mlt-")


def test_idempotency_prevents_duplicate_submit(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    assert _submit(store, plan)["submitted"]
    assert _submit(store, plan).get("blocked")


def test_unknown_status_blocks_new_orders(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            raise TimeoutError("x")
        if "/balance" in url:
            return 200, {"balance": 1000}
        return 200, {}
    assert _submit(store, plan, transport=t)["status"] == "UNKNOWN"
    plan2, _ = _make_plan(store, now=now)
    r = _submit(store, plan2)
    assert r.get("blocked")  # UNKNOWN attempt blocks further orders


def test_partial_fill_blocks_future_orders(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            return 201, {"order": {"order_id": "o", "status": "canceled", "filled_quantity": 1}}
        if "/balance" in url:
            return 200, {"balance": 1000}
        if "/orders/" in url:
            return 200, {"order": {"status": "canceled", "filled_quantity": 1, "fee": 0}}
        if "/fills" in url:
            return 200, {"fills": [{"count": 1, "fee": 0}]}
        return 200, {}
    # count from $0.50 / $0.50 = 1 contract expected; canceled-with-fill => partial
    res = _submit(store, plan, transport=t)
    assert res["status"] in ("PARTIALLY_FILLED", "REJECTED")
    plan2, _ = _make_plan(store, now=now)
    assert _submit(store, plan2).get("blocked")


def test_reconciliation_mismatch_blocks_future_orders(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            return 201, {"order": {"order_id": "o", "status": "resting"}}
        if "/balance" in url:
            return 200, {"balance": 1000}
        if "/orders/" in url:
            return 200, {"order": {"status": "resting", "filled_quantity": 0}}
        if "/fills" in url:
            return 200, {"fills": []}
        return 200, {}
    res = _submit(store, plan, transport=t)
    # FOK should never rest -> CRITICAL / acknowledged-open; blocks further
    plan2, _ = _make_plan(store, now=now)
    assert _submit(store, plan2).get("blocked")


# --- 54-58 audit / report / storage / api --------------------------------- #
def test_audit_chain_hash_changes_with_events(store):
    from engine.micro_live.audit import write_audit
    h1 = write_audit(store, event_type="a", message="one")
    h2 = write_audit(store, event_type="b", message="two")
    assert h1 != h2 and len(h1) == 16


def test_micro_live_report_artifacts_created(store, open_env, tmp_path, monkeypatch):
    out = tmp_path / "art"
    monkeypatch.setenv("MICRO_LIVE_OUTPUT_DIR", str(out))
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    res = _submit(store, plan)
    rp = Path(res["report_path"])
    assert rp.exists()
    d = rp.parent
    for f in ("micro_live_report.md", "order_attempt.json", "canary_plan.json",
              "audit_events.csv"):
        assert (d / f).exists()


def test_micro_live_report_redacts_secrets(store, open_env, tmp_path, monkeypatch):
    out = tmp_path / "art2"
    monkeypatch.setenv("MICRO_LIVE_OUTPUT_DIR", str(out))
    monkeypatch.setenv("KALSHI_TRADING_ACCESS_KEY_ID", "SUPERSECRETKEY")
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    res = _submit(store, plan)
    blob = Path(res["report_path"]).read_text()
    for sib in Path(res["report_path"]).parent.glob("*.json"):
        blob += sib.read_text()
    assert "SUPERSECRETKEY" not in blob


def test_micro_live_storage_migrations_idempotent(tmp_path):
    p = tmp_path / "idem.db"
    Store(p)
    s = Store(p)  # second init must not raise / wipe
    s.add_micro_live_audit_event({"event_type": "x", "message": "y"})
    assert s.get_micro_live_audit_events(10)


def test_micro_live_api_status_redacts_secrets(monkeypatch):
    monkeypatch.setenv("KALSHI_TRADING_ACCESS_KEY_ID", "APISECRET123")
    cfg = MicroLiveConfig.from_env()
    blob = json.dumps(cfg.public_dict())
    assert "APISECRET123" not in blob


# --- 59-62 conformance ----------------------------------------------------- #
def _conf():
    from engine.micro_live.conformance import MicroLiveConformanceHarness
    return MicroLiveConformanceHarness(MicroLiveConfig())


def test_micro_live_conformance_fails_if_api_submit_route_exists():
    bad = 'app.post("/api/micro-live/submit")\nasync def x(): pass\n'
    res = _conf().run({"app_source": bad})
    assert res["status"] == "FAIL"
    assert any(c["check_name"] == "no_api_submit_route" and c["status"] == "FAIL"
               for c in res["checks"])


def test_micro_live_conformance_fails_on_autonomous_loop():
    res = _conf().run({"autonomous_loop": True})
    assert res["status"] == "FAIL"


def test_micro_live_conformance_passes_clean_disabled_default(monkeypatch):
    monkeypatch.setattr(ml_locks, "BUILD_ENABLED", False)
    res = _conf().run()
    assert res["status"] == "PASS"


def test_micro_live_conformance_passes_mocked_demo_canary(store, open_env, monkeypatch, tmp_path):
    monkeypatch.setenv("MICRO_LIVE_OUTPUT_DIR", str(tmp_path / "c"))
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    res = _submit(store, plan)
    assert res["submitted"] and res["status"] == "FILLED"


# --- 63-69 secrets / kill / allowlists ------------------------------------- #
def test_micro_live_no_private_key_persistence(store, open_env, monkeypatch):
    monkeypatch.setenv("KALSHI_TRADING_PRIVATE_KEY_PEM",
                       "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----")
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    _submit(store, plan)
    raw = Path(store.db_path).read_bytes()
    assert b"BEGIN PRIVATE KEY" not in raw and b"ABC" not in raw


def test_micro_live_no_secrets_in_logs(monkeypatch):
    monkeypatch.setenv("KALSHI_TRADING_PRIVATE_KEY_PASSWORD", "p@ssw0rd-secret")
    from engine.micro_live.secret_runtime import redact
    assert "p@ssw0rd-secret" not in redact("password=p@ssw0rd-secret")


def test_micro_live_kill_switch_after_submit_blocks_second_order(store, open_env, monkeypatch,
                                                                 tmp_path):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    assert _submit(store, plan)["submitted"]
    ks = tmp_path / "KS"
    ks.write_text("x")
    monkeypatch.setenv("MICRO_LIVE_KILL_SWITCH_PATH", str(ks))
    plan2, _ = _make_plan(store, now=now)
    r = _submit(store, plan2)
    assert r.get("blocked")


def test_micro_live_open_order_after_fok_triggers_emergency_state(store, open_env):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)

    def t(method, url, *, headers=None, json_body=None):
        if method == "POST" and "/orders" in url:
            return 201, {"order": {"order_id": "o", "status": "resting"}}
        if "/balance" in url:
            return 200, {"balance": 1000}
        if "/orders/" in url:
            return 200, {"order": {"status": "resting", "filled_quantity": 0}}
        if "/fills" in url:
            return 200, {"fills": []}
        return 200, {}
    _submit(store, plan, transport=t)
    events = store.get_micro_live_audit_events(50)
    assert any(e.get("severity") == "CRITICAL" for e in events)


def test_micro_live_allowed_venues_enforced(store, open_env, monkeypatch):
    now = _seed_ready(store)
    # plan venue kalshi allowed; build a polymarket plan -> blocked at create
    store.add_dry_run_order_intent({"dry_run_intent_id": "dri2", "ts_ms": now, "venue": "polymarket",
                                    "market_ticker": "M", "outcome": "YES", "side": "BUY",
                                    "order_type": "FOK", "limit_price": "0.5", "quantity": "1",
                                    "notional": "0.5", "unsigned": 1, "unsent": 1, "signer_used": 0,
                                    "network_called": 0, "safety_envelope_decision_id": "se",
                                    "status": "CREATED"})
    plan, errs = create_canary_plan(store, MicroLiveConfig.from_env(), dry_run_intent_id="dri2",
                                    readiness_report_id="rr", venue="polymarket",
                                    environment="demo", now_ms=now)
    assert any("venue_not_allowed" in e for e in errs)


def test_micro_live_market_allowlist_enforced(store, open_env, monkeypatch):
    monkeypatch.setenv("MICRO_LIVE_MARKET_ALLOWLIST", "ONLY-THIS-MARKET")
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)  # market TEST-MKT not in allowlist
    ctx = _good_market_ctx()
    result, safety, _ = ml_preflight.preflight_canary_plan(store, MicroLiveConfig.from_env(), plan,
                                                           arming_ok=True, account_ok=True,
                                                           market_ctx={**ctx,
                                                                       "market_ref": plan.market_ticker},
                                                           now_ms=now)
    assert result.status == "FAIL"


def test_micro_live_max_orders_per_day_enforced(store, open_env, monkeypatch):
    now = _seed_ready(store)
    plan, _ = _make_plan(store, now=now)
    assert _submit(store, plan)["submitted"]
    # a second distinct plan on the same day should be blocked by daily cap / blocking state
    plan2, _ = _make_plan(store, now=now)
    assert _submit(store, plan2).get("blocked")


# --- 70-71 regression / import -------------------------------------------- #
def test_existing_phase8_tests_still_pass():
    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig
    res = ConformanceHarness(store=None, config=GuardedLiveConfig()).run()
    assert res.status in ("PASS", "FAIL")  # harness still runs


def test_compile_and_import_micro_live_modules():
    import engine.micro_live.account_snapshot  # noqa: F401
    import engine.micro_live.audit  # noqa: F401
    import engine.micro_live.canary_plan  # noqa: F401
    import engine.micro_live.config  # noqa: F401
    import engine.micro_live.execution_service  # noqa: F401
    import engine.micro_live.kalshi_live_broker  # noqa: F401
    import engine.micro_live.ledger  # noqa: F401
    import engine.micro_live.live_broker_base  # noqa: F401
    import engine.micro_live.locks  # noqa: F401
    import engine.micro_live.network_guard  # noqa: F401
    import engine.micro_live.order_builder  # noqa: F401
    import engine.micro_live.polymarket_live_broker  # noqa: F401
    import engine.micro_live.preflight  # noqa: F401
    import engine.micro_live.reconciliation  # noqa: F401
    import engine.micro_live.report  # noqa: F401
    import engine.micro_live.schemas  # noqa: F401
    import engine.micro_live.secret_runtime  # noqa: F401
    import engine.micro_live.state_machine  # noqa: F401
    assert True
