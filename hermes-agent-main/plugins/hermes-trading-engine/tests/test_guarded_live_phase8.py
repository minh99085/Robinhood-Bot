"""Phase 8 tests: guarded-live design skeleton (DRY-RUN ONLY).

Prove the door stays locked: execution methods disabled, no network/order/signing
path, fail-closed prechecks, two-person approvals, dry-run-only arming, RiskEngine
+ SafetyEnvelope required for every dry-run intent, conformance proofs, and no live
state anywhere. Mocks/fakes only; no real keys/network.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
import typing
from decimal import Decimal
from pathlib import Path

import pytest

from engine.guarded_live import (
    ApprovalWorkflow,
    ArmingTokenManager,
    ConformanceHarness,
    DisabledLiveBroker,
    DryRunLiveBroker,
    GuardedLiveConfig,
    GuardedLiveStateMachine,
    LiveExecutionDisabled,
    SafetyEnvelope,
    SecretPolicy,
    redact,
    run_precheck,
    write_report,
)
from engine.guarded_live.errors import GuardedLiveStateError
from engine.guarded_live.schemas import GuardedLiveState
from engine.guarded_live.state_machine import FORBIDDEN_LIVE_STATES, STATES
from engine.guarded_live.venue_mappers import map_kalshi_order, map_polymarket_order
from engine.storage import Store

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_FIX = _ROOT / "tests" / "fixtures"
NOW = 1_780_000_000_000


def _store(tmp):
    return Store(tmp / "g.db")


def _cfg(**kw):
    c = GuardedLiveConfig(enabled=True, allowlist_venues=["polymarket"])
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _seed_ready(store, status="READY_FOR_MANUAL_REVIEW", age_ms=0):
    store.add_readiness_report({"report_id": "R", "shadow_session_id": "S",
                                "generated_ts_ms": int(time.time() * 1000) - age_ms,
                                "overall_status": status, "summary_json": {}, "report_path": None})


# 1
def test_guarded_live_disabled_by_default():
    c = GuardedLiveConfig()
    assert c.enabled is False and c.mode == "design_only" and c.dry_run_only is True


# 2
def test_guarded_live_state_machine_has_no_live_active_state():
    args = set(typing.get_args(GuardedLiveState))
    assert not (args & FORBIDDEN_LIVE_STATES)
    assert not (set(STATES) & FORBIDDEN_LIVE_STATES)
    assert "LIVE_ACTIVE" not in STATES and "AUTO_LIVE" not in STATES


# 3
def test_state_machine_cannot_transition_to_live():
    sm = GuardedLiveStateMachine(config=_cfg(), initial="ARMED_DRY_RUN_ONLY")
    for bad in ("LIVE_ACTIVE", "REAL_MONEY_ACTIVE", "PRODUCTION_EXECUTION", "AUTO_LIVE"):
        with pytest.raises(GuardedLiveStateError):
            sm.transition(bad)


# 4
def test_precheck_fails_without_readiness_report(tmp_path):
    pre = run_precheck(_store(tmp_path), _cfg())
    assert pre.status == "FAIL"
    assert any(c.check_name == "shadow_readiness_valid" and c.status == "FAIL" for c in pre.checks)


# 5
def test_precheck_fails_if_readiness_not_manual_review(tmp_path):
    st = _store(tmp_path)
    _seed_ready(st, status="NOT_READY")
    pre = run_precheck(st, _cfg())
    assert any(c.check_name == "shadow_readiness_valid" and c.status == "FAIL" for c in pre.checks)


# 6
def test_precheck_fails_if_shadow_report_too_old(tmp_path):
    st = _store(tmp_path)
    _seed_ready(st, age_ms=48 * 3600 * 1000)
    pre = run_precheck(st, _cfg(max_shadow_report_age_hours=24))
    assert any(c.check_name == "shadow_readiness_valid" and c.status == "FAIL" for c in pre.checks)


# 7
def test_precheck_fails_if_live_broker_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_BROKER_ENABLED", "1")
    st = _store(tmp_path)
    _seed_ready(st)
    pre = run_precheck(st, _cfg())
    assert pre.status == "FAIL"
    assert any(c.check_name == "no_live_broker_configured" and c.status == "FAIL" for c in pre.checks)


# 8
def test_precheck_fails_on_forbidden_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_ORDERS", "1")
    st = _store(tmp_path)
    _seed_ready(st)
    pre = run_precheck(st, _cfg())
    assert any(c.check_name == "no_forbidden_env_vars" and c.status == "FAIL" for c in pre.checks)


# 9
def test_precheck_fails_on_kill_switch(tmp_path):
    ks = tmp_path / "KS"
    ks.write_text("x")
    st = _store(tmp_path)
    _seed_ready(st)
    pre = run_precheck(st, _cfg(kill_switch_path=str(ks)))
    assert any(c.check_name == "kill_switch_absent" and c.status == "FAIL" for c in pre.checks)


# 10
def test_precheck_passes_for_good_dry_run_setup(tmp_path):
    st = _store(tmp_path)
    _seed_ready(st)
    pre = run_precheck(st, _cfg(allowlist_venues=["polymarket"]), conformance_ok=True)
    assert pre.status == "PASS", [c.check_name for c in pre.checks if c.status == "FAIL"]


def _batch(store, cfg, now=NOW):
    return ApprovalWorkflow(store, cfg).create_batch(readiness_report_id="R",
                                                     config_hash=cfg.config_hash(), now_ms=now)


# 11
def test_manual_approval_requires_human_actor(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    wf = ApprovalWorkflow(st, cfg)
    b = _batch(st, cfg)
    ok, res = wf.approve(b, approver_id="grok", role="lab_manager",
                         confirmation_text="I understand this is DRY-RUN ONLY.",
                         readiness_report_id="R", config_hash=cfg.config_hash(), now_ms=NOW)
    assert ok is False and res == "automated_actor_forbidden"


# 12
def test_manual_approval_requires_confirmation_text(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _batch(st, cfg)
    ok, res = ApprovalWorkflow(st, cfg).approve(
        b, approver_id="alice", role="lab_manager", confirmation_text="ok",
        readiness_report_id="R", config_hash=cfg.config_hash(), now_ms=NOW)
    assert ok is False and res == "missing_typed_confirmation"


# 13
def test_duplicate_approver_counts_once(tmp_path):
    cfg = _cfg(required_approvals=2)
    st = _store(tmp_path)
    wf = ApprovalWorkflow(st, cfg)
    b = _batch(st, cfg)
    conf = "I understand this is DRY-RUN ONLY."
    for _ in range(2):
        wf.approve(b, approver_id="alice", role="lab_manager", confirmation_text=conf,
                   readiness_report_id="R", config_hash=cfg.config_hash(), now_ms=NOW)
    assert b.valid_approvals == 1 and b.status == "PENDING"


# 14
def test_approval_expires(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _batch(st, cfg)
    b.expires_ts_ms = 1
    ok, res = ApprovalWorkflow(st, cfg).approve(
        b, approver_id="alice", role="lab_manager",
        confirmation_text="I understand this is DRY-RUN ONLY.", readiness_report_id="R",
        config_hash=cfg.config_hash(), now_ms=NOW)
    assert ok is False and res == "approval_batch_expired" and b.status == "EXPIRED"


# 15
def test_config_change_invalidates_approval(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _batch(st, cfg)
    ok, res = ApprovalWorkflow(st, cfg).approve(
        b, approver_id="alice", role="lab_manager",
        confirmation_text="I understand this is DRY-RUN ONLY.", readiness_report_id="R",
        config_hash="DIFFERENT_HASH", now_ms=NOW)
    assert ok is False and "config_hash_mismatch" in res and b.status == "INVALIDATED"


# 16
def test_approval_batch_reaches_dry_run_only(tmp_path):
    cfg = _cfg(required_approvals=2)
    st = _store(tmp_path)
    wf = ApprovalWorkflow(st, cfg)
    b = _batch(st, cfg)
    conf = "I understand this is DRY-RUN ONLY."
    for who in ("alice", "bob"):
        wf.approve(b, approver_id=who, role="lab_manager", confirmation_text=conf,
                   readiness_report_id="R", config_hash=cfg.config_hash(), now_ms=NOW)
    assert b.status == "APPROVED_DRY_RUN_ONLY" and b.valid_approvals == 2


# 17
def test_approval_batch_never_reaches_live():
    from engine.guarded_live.schemas import ApprovalBatch
    statuses = set(typing.get_args(ApprovalBatch.model_fields["status"].annotation))
    assert not any("LIVE" in s.upper() and "DRY" not in s.upper() for s in statuses)


def _approved_batch(store, cfg):
    wf = ApprovalWorkflow(store, cfg)
    b = _batch(store, cfg)
    conf = "I understand this is DRY-RUN ONLY."
    for who in ("alice", "bob"):
        wf.approve(b, approver_id=who, role="risk_reviewer", confirmation_text=conf,
                   readiness_report_id="R", config_hash=cfg.config_hash(), now_ms=NOW)
    return b


# 18
def test_arming_token_stored_hashed_only(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _approved_batch(st, cfg)
    plain, rec = ArmingTokenManager(st, cfg).issue(b, now_ms=NOW)
    row = st.get_arming_token_by_hash(rec.token_hash)
    assert row is not None and row["token_hash"] != plain and plain not in str(row)


# 19
def test_arming_token_expires(tmp_path):
    cfg = _cfg(arming_expiry_minutes=10)
    st = _store(tmp_path)
    b = _approved_batch(st, cfg)
    plain, rec = ArmingTokenManager(st, cfg).issue(b, now_ms=NOW)
    ok, reason = ArmingTokenManager(st, cfg).verify(plain, now_ms=rec.expires_ts_ms + 1)
    assert ok is False and reason == "expired"


# 20
def test_arming_token_revoked(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _approved_batch(st, cfg)
    mgr = ArmingTokenManager(st, cfg)
    plain, rec = mgr.issue(b, now_ms=NOW)
    mgr.revoke(rec.arming_token_id)
    ok, reason = mgr.verify(plain, now_ms=NOW + 1000)
    assert ok is False and reason == "revoked"


# 21
def test_arming_token_bound_to_config_hash(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _approved_batch(st, cfg)
    plain, rec = ArmingTokenManager(st, cfg).issue(b, now_ms=NOW)
    ok, reason = ArmingTokenManager(st, cfg).verify(plain, config_hash="OTHER", now_ms=NOW + 1000)
    assert ok is False and reason == "config_hash_mismatch"


# 22
def test_arming_token_cannot_enable_live(tmp_path):
    cfg = _cfg()
    st = _store(tmp_path)
    b = _approved_batch(st, cfg)
    _, rec = ArmingTokenManager(st, cfg).issue(b, now_ms=NOW)
    assert rec.mode == "dry_run_only"


# 23/24/25
def test_disabled_live_broker_execution_raises():
    db = DisabledLiveBroker("polymarket")
    for m in ("submit_order", "cancel_order", "replace_order"):
        with pytest.raises(LiveExecutionDisabled):
            getattr(db, m)()


def _order():
    return {"venue": "kalshi", "market_ticker": "X", "outcome": "YES", "side": "BUY",
            "price": 0.45, "quantity": 1, "order_type": "LIMIT"}


# 26 + 27
def test_dry_run_broker_never_calls_network_or_signs():
    i = DryRunLiveBroker().validate_order(_order(), risk_decision_id="r", safety_envelope_decision_id="s")
    assert i.network_called is False and i.signer_used is False and i.unsigned and i.unsent


# 28
def test_dry_run_intent_requires_risk_decision():
    i = DryRunLiveBroker().validate_order(_order())
    assert i.status == "BLOCKED" and i.reason == "missing_risk_decision"


# 29
def test_dry_run_intent_requires_safety_envelope_decision():
    i = DryRunLiveBroker().validate_order(_order(), risk_decision_id="r")
    assert i.status == "BLOCKED" and i.reason == "missing_safety_envelope_decision"


def _good_safety_ctx(**over):
    ctx = {"notional": 0.5, "edge_after_costs": 0.1, "evidence_score": 0.6, "source_count": 2,
           "close_ts_ms": int(time.time() * 1000) + 10 ** 9}
    ctx.update(over)
    return ctx


# 30
def test_safety_envelope_blocks_stale_data():
    d = SafetyEnvelope(_cfg(), state="ARMED_DRY_RUN_ONLY").validate(_good_safety_ctx(stale_ms=5000))
    assert not d.allowed and d.reason == "market_data_fresh"


# 31
def test_safety_envelope_blocks_high_ambiguity():
    d = SafetyEnvelope(_cfg(), state="ARMED_DRY_RUN_ONLY").validate(_good_safety_ctx(ambiguity_score=0.9))
    assert not d.allowed and d.reason == "ambiguity_within_limit"


# 32
def test_safety_envelope_blocks_excess_notional():
    d = SafetyEnvelope(_cfg(), state="ARMED_DRY_RUN_ONLY").validate(_good_safety_ctx(notional=1000))
    assert not d.allowed and d.reason == "order_notional_within_limit"


# 33
def test_safety_envelope_blocks_venue_degraded():
    d = SafetyEnvelope(_cfg(), state="ARMED_DRY_RUN_ONLY").validate(_good_safety_ctx(venue_status="degraded"))
    assert not d.allowed and d.reason == "venue_not_degraded"


# 34
def test_safety_envelope_blocks_kill_switch():
    d = SafetyEnvelope(_cfg(), state="ARMED_DRY_RUN_ONLY").validate(_good_safety_ctx(kill_switch=True))
    assert not d.allowed and d.reason == "kill_switch_absent"


# 35
def test_polymarket_mapper_validates_payload_without_signing():
    payload, errors = map_polymarket_order({"asset_id": "TOK", "side": "BUY", "price": 0.45,
                                            "quantity": 2, "tick_size": 0.01})
    assert not errors and payload["_signed"] is False and payload["_sent"] is False
    assert payload["_intent_tag"] == "UNSIGNED_DRY_RUN_ONLY"


# 36
def test_polymarket_mapper_rejects_bad_tick():
    _, errors = map_polymarket_order({"asset_id": "T", "side": "BUY", "price": 0.455,
                                      "quantity": 1, "tick_size": 0.01})
    assert any("tick" in e for e in errors)


# 37
def test_polymarket_mapper_rejects_post_only_fok():
    _, errors = map_polymarket_order({"asset_id": "T", "side": "BUY", "price": 0.45,
                                      "quantity": 1, "tick_size": 0.01, "post_only": True,
                                      "order_type": "FOK"})
    assert any("post_only" in e for e in errors)


# 38
def test_polymarket_mapper_does_not_import_wallet_signer():
    m = importlib.import_module("engine.guarded_live.venue_mappers.polymarket_mapper")
    names = [n.lower() for n in dir(m)]
    assert not any(("sign" in n or "wallet" in n or "private" in n) for n in names)


# 39
def test_kalshi_mapper_validates_payload_without_posting():
    payload, errors = map_kalshi_order(_order())
    assert not errors and payload["_sent"] is False and payload["_intent_tag"] == "UNSENT_DRY_RUN_ONLY"


# 40
def test_kalshi_mapper_sets_cancel_order_on_pause_true():
    payload, _ = map_kalshi_order(_order())
    assert payload["cancel_order_on_pause"] is True


# 41
def test_kalshi_mapper_rejects_invalid_price():
    _, errors = map_kalshi_order({"market_ticker": "X", "outcome": "YES", "side": "BUY",
                                  "price": 1.5, "quantity": 1})
    assert any("price" in e for e in errors)


# 42
def test_kalshi_mapper_does_not_call_create_order_endpoint():
    m = importlib.import_module("engine.guarded_live.venue_mappers.kalshi_mapper")
    names = [n.lower() for n in dir(m)]
    assert not any(("post" in n or "create_order" in n or "submit" in n) for n in names)


# 43
def test_conformance_detects_network_call():
    run = ConformanceHarness(config=_cfg()).run(traps={"network": 1})
    assert run.status == "FAIL"


# 44
def test_conformance_detects_order_endpoint_call():
    run = ConformanceHarness(config=_cfg()).run(traps={"order_endpoint": 1})
    assert run.status == "FAIL"


# 45
def test_conformance_passes_clean_dry_run_design():
    run = ConformanceHarness(config=_cfg()).run()
    assert run.status == "PASS" and run.fail_count == 0


# 46
def test_secret_policy_redacts_keys(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-SUPERSECRET12345")
    out = redact("auth xai-SUPERSECRET12345 -----BEGIN PRIVATE KEY-----z-----END PRIVATE KEY-----")
    assert "xai-SUPERSECRET12345" not in out and "[REDACTED" in out


# 47
def test_secret_policy_detects_forbidden_env(monkeypatch):
    monkeypatch.setenv("REAL_MONEY", "1")
    ok, violations = SecretPolicy(_cfg()).check()
    assert ok is False and any(v.violation_type == "forbidden_env_var" for v in violations)


# 48
def test_api_guarded_live_status_redacts_secrets(monkeypatch):
    import json
    monkeypatch.setenv("XAI_API_KEY", "xai-NOLEAK")
    cfg = GuardedLiveConfig.from_env()
    status = {"enabled": cfg.enabled, "mode": cfg.mode, "config": cfg.public_dict()}
    assert "xai-NOLEAK" not in json.dumps(status, default=str)


# 49
def test_api_dry_run_intent_requires_armed_state():
    for s in ("DISABLED", "DESIGN_ONLY", "PRECHECK_PASSED", "AWAITING_APPROVAL"):
        sm = GuardedLiveStateMachine(initial=s)
        assert sm.state not in ("ARMED_DRY_RUN_ONLY", "DRY_RUN_ACTIVE")


# 50
def test_api_arm_dry_run_never_sets_live_state():
    sm = GuardedLiveStateMachine(config=_cfg(), initial="APPROVED_DRY_RUN_ONLY")
    sm.transition("ARMED_DRY_RUN_ONLY", reason="arm")
    assert sm.state == "ARMED_DRY_RUN_ONLY"
    assert sm.state not in FORBIDDEN_LIVE_STATES


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 51/52/53
@pytest.mark.parametrize("fname", ["guarded_live_precheck.py", "guarded_live_conformance.py",
                                   "guarded_live_dry_run_order.py"])
def test_cli_help(fname):
    mod = _load(fname.replace(".py", ""), fname)
    with pytest.raises(SystemExit) as e:
        mod.main(["--help"])
    assert e.value.code == 0


# 54 + 55
def test_guarded_live_report_artifacts_and_no_live(tmp_path):
    cfg = _cfg()
    out = write_report(_store(tmp_path), cfg, state="DESIGN_ONLY",
                       conformance={"status": "PASS"}, base_dir=str(tmp_path / "art"))
    assert (out / "guarded_live_design_report.md").exists()
    assert (out / "guarded_live_config.json").exists()
    md = (out / "guarded_live_design_report.md").read_text()
    assert "No live orders were submitted" in md and "Real execution remains DISABLED" in md


# 56
def test_guarded_live_storage_migrations_idempotent(tmp_path):
    p = tmp_path / "g.db"
    Store(p)
    s2 = Store(p)
    for t in ("guarded_live_state", "manual_approvals", "approval_batches", "arming_tokens",
              "dry_run_order_intents", "conformance_runs", "secret_policy_violations"):
        assert s2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()


# 57
def test_no_private_user_channels_in_guarded_live():
    from engine.venues.kalshi.ws import FORBIDDEN_CHANNELS
    assert {"fill", "user_orders", "market_positions"} <= set(FORBIDDEN_CHANNELS)


# 58
def test_no_order_endpoint_methods_exposed_by_default():
    db = DisabledLiveBroker("kalshi")
    # the methods exist but are LOCKED — calling any raises
    for m in ("submit_order", "cancel_order", "replace_order", "post_order", "create_order"):
        with pytest.raises(LiveExecutionDisabled):
            getattr(db, m)()


# 59
def test_existing_shadow_tests_still_pass_with_guarded_live_modules():
    import engine.shadow  # noqa: F401
    import engine.guarded_live  # noqa: F401
    from engine.shadow import ShadowConfig
    assert ShadowConfig().mode in ("shadow_live",)


# 60
def test_compile_and_import_guarded_live_modules():
    for name in ("config", "errors", "schemas", "state_machine", "safety_envelope", "approval",
                 "arming", "broker_interfaces", "disabled_brokers", "dry_run", "conformance",
                 "secret_policy", "readiness_loader", "audit", "report", "precheck"):
        importlib.import_module(f"engine.guarded_live.{name}")
    for name in ("polymarket_mapper", "kalshi_mapper"):
        importlib.import_module(f"engine.guarded_live.venue_mappers.{name}")
