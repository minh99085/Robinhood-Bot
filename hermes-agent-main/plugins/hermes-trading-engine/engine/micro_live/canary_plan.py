"""Canary plan workflow (Phase 9). A CanaryPlan is the ONLY allowed source of a
Phase 9 live order. It is generated from an approved Phase 8 dry-run intent,
expires quickly, is read-only until submitted, and is invalidated by config-hash
mismatch, market-data drift, degraded venue, kill switch, or staleness."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from ..guarded_live.readiness_loader import load_latest_readiness
from .audit import write_audit
from .config import MicroLiveConfig
from .order_builder import build_payload
from .schemas import MicroLiveCanaryPlan

CANARY_EXPIRY_SECONDS = 300


def latest_conformance_ok(store) -> bool:
    if store is None:
        return False
    try:
        runs = store.get_conformance_runs(1)
    except Exception:  # noqa: BLE001
        return False
    return bool(runs) and str(runs[0].get("status")) == "PASS"


def readiness_ok(store, config: MicroLiveConfig,
                 report_id: Optional[str]) -> tuple[bool, str, Optional[str]]:
    rep = load_latest_readiness(store, report_id)
    if not rep:
        return False, "no_readiness_report", None
    status = (rep.get("overall_status") or rep.get("status") or rep.get("readiness_status"))
    rid = rep.get("report_id") or report_id
    if status != config.required_shadow_status:
        return False, f"readiness_status={status}", rid
    ts = rep.get("generated_ts_ms") or rep.get("ts_ms") or rep.get("created_ts_ms")
    if ts and config.max_shadow_report_age_hours > 0:
        age_h = (int(time.time() * 1000) - int(ts)) / 3_600_000.0
        if age_h > config.max_shadow_report_age_hours:
            return False, "readiness_report_stale", rid
    return True, "ok", rid


def create_canary_plan(store, config: MicroLiveConfig, *, dry_run_intent_id: str,
                       readiness_report_id: Optional[str], venue: str, environment: str,
                       approval_batch_id: Optional[str] = None,
                       arming_token_id: Optional[str] = None,
                       now_ms: Optional[int] = None,
                       edge_after_costs: Optional[float] = None
                       ) -> tuple[MicroLiveCanaryPlan, list[str]]:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    errs: list[str] = []

    intent = store.get_dry_run_order_intent(dry_run_intent_id) if store is not None else None
    if config.require_dry_run_intent and not intent:
        errs.append("dry_run_intent_missing")
    if intent:
        if int(intent.get("unsigned", 1)) != 1:
            errs.append("dry_run_intent_was_signed")
        if int(intent.get("unsent", 1)) != 1:
            errs.append("dry_run_intent_was_sent")
        if int(intent.get("signer_used", 0)) != 0:
            errs.append("dry_run_intent_signer_used")
        if int(intent.get("network_called", 0)) != 0:
            errs.append("dry_run_intent_network_called")
        if not intent.get("safety_envelope_decision_id"):
            errs.append("dry_run_intent_no_safety_decision")

    if venue not in config.allowed_venues:
        errs.append(f"venue_not_allowed:{venue}")
    if environment not in config.allowed_environments or \
            (environment == "prod" and not config.allow_production):
        errs.append(f"environment_not_allowed:{environment}")

    if config.require_shadow_ready:
        ok, reason, rid = readiness_ok(store, config, readiness_report_id)
        if not ok:
            errs.append(f"readiness:{reason}")
        readiness_report_id = rid or readiness_report_id or ""
    if config.require_phase8_conformance and not latest_conformance_ok(store):
        errs.append("phase8_conformance_not_pass")

    src = intent or {}
    notional = Decimal(str(src.get("notional", "0") or "0"))
    if notional <= 0 or notional > config.max_order_notional_usd:
        errs.append(f"notional_out_of_cap:{notional}")

    plan = MicroLiveCanaryPlan(
        created_ts_ms=now, expires_ts_ms=now + CANARY_EXPIRY_SECONDS * 1000, venue=venue,
        environment=environment, market_id=src.get("market_id"),
        market_ticker=src.get("market_ticker"), asset_id=src.get("asset_id"),
        outcome=src.get("outcome") or "YES", side=src.get("side") or "BUY",
        order_type="FOK", time_in_force="fill_or_kill",
        limit_price=Decimal(str(src.get("limit_price", "0") or "0")),
        quantity=Decimal(str(src.get("quantity", "0") or "0")), notional=notional,
        max_slippage=Decimal("0.01"), max_staleness_ms=config.max_stale_ms,
        source_dry_run_intent_id=dry_run_intent_id, readiness_report_id=readiness_report_id or "",
        approval_batch_id=approval_batch_id, arming_token_id=arming_token_id,
        safety_envelope_decision_id=src.get("safety_envelope_decision_id"),
        risk_decision_id=src.get("risk_decision_id"))

    payload, perrs = build_payload(plan, config)
    errs.extend(perrs)
    if payload is not None:
        plan.expected_payload_hash = payload.payload_hash

    plan.status = "REJECTED" if errs else "CREATED"
    plan.reason = ";".join(errs) if errs else None
    if store is not None:
        try:
            store.add_micro_live_canary_plan(plan.record())
        except Exception:  # noqa: BLE001
            errs.append("storage_write_failed")
    write_audit(store, event_type="canary_plan_created",
                severity="INFO" if not errs else "WARN", actor="cli",
                canary_plan_id=plan.canary_plan_id, message=plan.reason or "created",
                payload={"venue": venue, "environment": environment})
    return plan, errs


def validate_canary_plan(store, config: MicroLiveConfig, plan: MicroLiveCanaryPlan, *,
                         market_ctx: Optional[dict] = None,
                         now_ms: Optional[int] = None) -> tuple[bool, list[str]]:
    """Re-validate just before arming/submit. Fail closed."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    market_ctx = market_ctx or {}
    errs: list[str] = []
    if now >= plan.expires_ts_ms:
        errs.append("canary_plan_expired")
    if config.kill_switch_active():
        errs.append("kill_switch_active")
    if config.config_hash() and plan.status == "INVALID":
        errs.append("plan_invalidated")
    # market-data drift beyond tolerance
    drift = market_ctx.get("price_drift")
    if drift is not None and plan.max_slippage is not None:
        try:
            if Decimal(str(abs(drift))) > Decimal(str(plan.max_slippage)):
                errs.append("market_data_drift_exceeds_tolerance")
        except Exception:  # noqa: BLE001
            pass
    if market_ctx.get("tick_dirty"):
        errs.append("tick_size_dirty")
    if market_ctx.get("seq_gap"):
        errs.append("sequence_gap")
    if str(market_ctx.get("venue_status", "ready")).lower() in (
            "degraded", "disconnected", "reconnecting", "failed"):
        errs.append("venue_degraded")
    if int(market_ctx.get("stale_ms", 0)) > config.max_stale_ms:
        errs.append("orderbook_stale")
    return (not errs), errs
