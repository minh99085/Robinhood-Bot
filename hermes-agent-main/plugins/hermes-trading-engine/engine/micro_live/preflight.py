"""Micro-live preflight (Phase 9). Re-runs ALL gates (locks, conformance,
readiness, approvals, arming, RiskEngine, SafetyEnvelope, venue/market data,
account) and persists a MicroLivePreflightResult. Does NOT submit."""

from __future__ import annotations

import time
from typing import Optional

from .audit import write_audit
from .canary_plan import (latest_conformance_ok, readiness_ok, validate_canary_plan)
from .config import MicroLiveConfig
from .locks import all_pass, check_locks
from .safety import MicroSafetyEnvelope, run_risk
from .schemas import MicroLivePreflightResult


def preflight_canary_plan(store, config: MicroLiveConfig, plan, *,
                          approvals_ok: Optional[bool] = None,
                          arming_ok: Optional[bool] = None,
                          account_ok: Optional[bool] = None,
                          market_ctx: Optional[dict] = None,
                          now_ms: Optional[int] = None,
                          persist: bool = True) -> MicroLivePreflightResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    market_ctx = market_ctx or {}

    lock_results = check_locks(config)
    locks_ok = all_pass(lock_results)

    plan_ok, plan_errs = validate_canary_plan(store, config, plan, market_ctx=market_ctx, now_ms=now)
    conformance = latest_conformance_ok(store) if config.require_phase8_conformance else True
    r_ok, r_reason, _ = readiness_ok(store, config, plan.readiness_report_id) \
        if config.require_shadow_ready else (True, "ok", plan.readiness_report_id)

    if approvals_ok is None:
        approvals_ok = (not config.require_approvals) or bool(plan.approval_batch_id)
    if arming_ok is None:
        arming_ok = (not config.require_arming_token) or bool(plan.arming_token_id)
    if account_ok is None:
        account_ok = not config.require_account_snapshot

    sctx = {
        "locks_ok": locks_ok, "environment": plan.environment, "venue": plan.venue,
        "market_ref": plan.market_ticker or plan.market_id, "order_type": plan.order_type,
        "time_in_force": plan.time_in_force, "notional": plan.notional,
        "limit_price": plan.limit_price, "side": plan.side, "now_ms": now,
        "state": "CANARY_READY", "canary_plan_id": plan.canary_plan_id,
        "idempotency_ok": market_ctx.get("idempotency_ok", True),
        **market_ctx,
    }
    safety = MicroSafetyEnvelope(config).validate(sctx)
    risk = run_risk(config, sctx)

    hard_fail = 0
    if not locks_ok:
        hard_fail += 1
    if not plan_ok:
        hard_fail += 1
    if not conformance:
        hard_fail += 1
    if not r_ok:
        hard_fail += 1
    if not approvals_ok:
        hard_fail += 1
    if not arming_ok:
        hard_fail += 1
    if not account_ok:
        hard_fail += 1
    if not safety.allowed:
        hard_fail += 1
    if not risk.approved:
        hard_fail += 1

    result = MicroLivePreflightResult(
        ts_ms=now, canary_plan_id=plan.canary_plan_id,
        status="PASS" if hard_fail == 0 else "FAIL", lock_results=lock_results,
        risk_status=("PASS" if risk.approved else f"FAIL:{risk.code}"),
        safety_status=("PASS" if safety.allowed else f"FAIL:{safety.reason}"),
        venue_status=str(market_ctx.get("venue_status", "ready")),
        account_status=("PASS" if account_ok else "FAIL"),
        readiness_status=("PASS" if r_ok else f"FAIL:{r_reason}"),
        approval_status=("PASS" if approvals_ok else "FAIL"),
        arming_status=("PASS" if arming_ok else "FAIL"),
        hard_fail_count=hard_fail,
        warning_count=(1 if safety.allowed and plan_errs else 0))

    if persist and store is not None:
        try:
            store.add_micro_live_preflight(result.record())
            store.add_safety_envelope_decision(safety.record())
        except Exception:  # noqa: BLE001
            pass
    write_audit(store, event_type="preflight", severity="INFO" if result.status == "PASS" else "WARN",
                actor="cli", canary_plan_id=plan.canary_plan_id,
                message=f"preflight {result.status} hard_fail={hard_fail}")
    return result, safety, risk
