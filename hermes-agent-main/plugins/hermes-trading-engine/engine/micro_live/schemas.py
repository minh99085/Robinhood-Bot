"""Micro-live schemas (Phase 9). Disabled by default; demo-first; one canary."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

MicroLiveEnvironment = Literal["demo", "prod"]
MicroLiveMode = Literal["disabled", "demo_micro", "prod_micro"]

LiveOrderStatus = Literal[
    "CREATED", "PRECHECK_FAILED", "BLOCKED", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED",
    "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCEL_REQUESTED", "CANCELLED", "EXPIRED",
    "UNKNOWN", "RECONCILE_FAILED", "FAILED"]

CheckStatus = Literal["PASS", "FAIL", "WARN"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _nid(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:16]}"


def _s(x) -> Optional[str]:
    return None if x is None else str(x)


def _j(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class MicroLiveLockStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lock_name: str
    passed: bool
    reason: str = ""
    required_value: Optional[str] = None
    observed_value_redacted: Optional[str] = None


class MicroLivePreflightResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    preflight_id: str = Field(default_factory=lambda: _nid("pf"))
    ts_ms: int = Field(default_factory=_now_ms)
    canary_plan_id: Optional[str] = None
    status: CheckStatus = "FAIL"
    lock_results: list[MicroLiveLockStatus] = Field(default_factory=list)
    risk_status: Optional[str] = None
    safety_status: Optional[str] = None
    venue_status: Optional[str] = None
    account_status: Optional[str] = None
    readiness_status: Optional[str] = None
    approval_status: Optional[str] = None
    arming_status: Optional[str] = None
    hard_fail_count: int = 0
    warning_count: int = 0

    def record(self) -> dict:
        return {"preflight_id": self.preflight_id, "ts_ms": self.ts_ms,
                "canary_plan_id": self.canary_plan_id, "status": self.status,
                "risk_status": self.risk_status, "safety_status": self.safety_status,
                "venue_status": self.venue_status, "account_status": self.account_status,
                "readiness_status": self.readiness_status, "approval_status": self.approval_status,
                "arming_status": self.arming_status, "hard_fail_count": self.hard_fail_count,
                "warning_count": self.warning_count,
                "payload_json": _j({"locks": [l.model_dump() for l in self.lock_results]})}


class MicroLiveCanaryPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    canary_plan_id: str = Field(default_factory=lambda: _nid("canary"))
    created_ts_ms: int = Field(default_factory=_now_ms)
    expires_ts_ms: int = 0
    venue: str = "kalshi"
    environment: str = "demo"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    side: str = "BUY"
    order_type: str = "FOK"
    time_in_force: str = "fill_or_kill"
    limit_price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    max_slippage: Optional[Decimal] = None
    max_staleness_ms: int = 750
    source_shadow_session_id: Optional[str] = None
    source_shadow_decision_id: Optional[str] = None
    source_dry_run_intent_id: str = ""
    readiness_report_id: str = ""
    approval_batch_id: Optional[str] = None
    arming_token_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    safety_envelope_decision_id: Optional[str] = None
    expected_payload_hash: str = ""
    status: str = "CREATED"
    reason: Optional[str] = None

    def record(self) -> dict:
        return {"canary_plan_id": self.canary_plan_id, "created_ts_ms": self.created_ts_ms,
                "expires_ts_ms": self.expires_ts_ms, "venue": self.venue,
                "environment": self.environment, "market_id": self.market_id,
                "market_ticker": self.market_ticker, "asset_id": self.asset_id,
                "outcome": self.outcome, "side": self.side, "order_type": self.order_type,
                "time_in_force": self.time_in_force, "limit_price": _s(self.limit_price),
                "quantity": _s(self.quantity), "notional": _s(self.notional),
                "max_slippage": _s(self.max_slippage), "max_staleness_ms": self.max_staleness_ms,
                "source_shadow_session_id": self.source_shadow_session_id,
                "source_shadow_decision_id": self.source_shadow_decision_id,
                "source_dry_run_intent_id": self.source_dry_run_intent_id,
                "readiness_report_id": self.readiness_report_id,
                "approval_batch_id": self.approval_batch_id, "arming_token_id": self.arming_token_id,
                "risk_decision_id": self.risk_decision_id,
                "safety_envelope_decision_id": self.safety_envelope_decision_id,
                "expected_payload_hash": self.expected_payload_hash, "status": self.status,
                "reason": self.reason, "payload_json": _j({})}


class MicroLiveOrderAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")
    live_order_attempt_id: str = Field(default_factory=lambda: _nid("mla"))
    canary_plan_id: str = ""
    ts_ms: int = Field(default_factory=_now_ms)
    venue: str = "kalshi"
    environment: str = "demo"
    client_order_id: str = ""
    exchange_order_id: Optional[str] = None
    status: LiveOrderStatus = "CREATED"
    submit_allowed: bool = False
    submitted: bool = False
    acknowledged: bool = False
    filled_quantity: Decimal = Decimal(0)
    avg_fill_price: Optional[Decimal] = None
    notional_submitted: Decimal = Decimal(0)
    notional_filled: Decimal = Decimal(0)
    fee: Optional[Decimal] = None
    reject_reason: Optional[str] = None
    error_type: Optional[str] = None
    error_message_redacted: Optional[str] = None
    request_payload_hash: Optional[str] = None
    response_payload_hash: Optional[str] = None
    network_call_count: int = 0
    signer_used: bool = False
    risk_decision_id: Optional[str] = None
    safety_envelope_decision_id: Optional[str] = None
    audit_chain_hash: Optional[str] = None

    def record(self) -> dict:
        return {"live_order_attempt_id": self.live_order_attempt_id,
                "canary_plan_id": self.canary_plan_id, "ts_ms": self.ts_ms, "venue": self.venue,
                "environment": self.environment, "client_order_id": self.client_order_id,
                "exchange_order_id": self.exchange_order_id, "status": self.status,
                "submit_allowed": int(self.submit_allowed), "submitted": int(self.submitted),
                "acknowledged": int(self.acknowledged), "filled_quantity": _s(self.filled_quantity),
                "avg_fill_price": _s(self.avg_fill_price),
                "notional_submitted": _s(self.notional_submitted),
                "notional_filled": _s(self.notional_filled), "fee": _s(self.fee),
                "reject_reason": self.reject_reason, "error_type": self.error_type,
                "error_message_redacted": self.error_message_redacted,
                "request_payload_hash": self.request_payload_hash,
                "response_payload_hash": self.response_payload_hash,
                "network_call_count": self.network_call_count, "signer_used": int(self.signer_used),
                "risk_decision_id": self.risk_decision_id,
                "safety_envelope_decision_id": self.safety_envelope_decision_id,
                "audit_chain_hash": self.audit_chain_hash}


class LiveAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    snapshot_id: str = Field(default_factory=lambda: _nid("acct"))
    ts_ms: int = Field(default_factory=_now_ms)
    venue: str = "kalshi"
    environment: str = "demo"
    cash_available: Optional[Decimal] = None
    collateral_available: Optional[Decimal] = None
    positions_value: Optional[Decimal] = None
    open_order_notional: Optional[Decimal] = None
    raw_payload_hash: Optional[str] = None

    def record(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "ts_ms": self.ts_ms, "venue": self.venue,
                "environment": self.environment, "cash_available": _s(self.cash_available),
                "collateral_available": _s(self.collateral_available),
                "positions_value": _s(self.positions_value),
                "open_order_notional": _s(self.open_order_notional),
                "raw_payload_hash": self.raw_payload_hash, "payload_json_redacted": _j({})}


class MicroLiveReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reconciliation_id: str = Field(default_factory=lambda: _nid("recon"))
    ts_ms: int = Field(default_factory=_now_ms)
    live_order_attempt_id: str = ""
    status: CheckStatus = "FAIL"
    exchange_order_status: Optional[str] = None
    local_order_status: str = "UNKNOWN"
    filled_quantity: Decimal = Decimal(0)
    local_filled_quantity: Decimal = Decimal(0)
    fee: Optional[Decimal] = None
    position_delta: Optional[Decimal] = None
    discrepancies: list = Field(default_factory=list)

    def record(self) -> dict:
        return {"reconciliation_id": self.reconciliation_id, "ts_ms": self.ts_ms,
                "live_order_attempt_id": self.live_order_attempt_id, "status": self.status,
                "exchange_order_status": self.exchange_order_status,
                "local_order_status": self.local_order_status,
                "filled_quantity": _s(self.filled_quantity),
                "local_filled_quantity": _s(self.local_filled_quantity), "fee": _s(self.fee),
                "position_delta": _s(self.position_delta),
                "discrepancies_json": _j(self.discrepancies)}


class EmergencyCancelResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cancel_id: str = Field(default_factory=lambda: _nid("cancel"))
    ts_ms: int = Field(default_factory=_now_ms)
    venue: str = "kalshi"
    environment: str = "demo"
    requested_by: str = ""
    reason: str = ""
    client_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    cancel_all: bool = False
    sent: bool = False
    success: bool = False
    response_hash: Optional[str] = None
    error_message_redacted: Optional[str] = None

    def record(self) -> dict:
        return {"cancel_id": self.cancel_id, "ts_ms": self.ts_ms, "venue": self.venue,
                "environment": self.environment, "requested_by": self.requested_by,
                "reason": self.reason, "client_order_id": self.client_order_id,
                "exchange_order_id": self.exchange_order_id, "cancel_all": int(self.cancel_all),
                "sent": int(self.sent), "success": int(self.success),
                "response_hash": self.response_hash,
                "error_message_redacted": self.error_message_redacted, "payload_json": _j({})}


class VenueLiveOrderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: str
    environment: str = "demo"
    payload_redacted: dict = Field(default_factory=dict)
    payload_hash: str = ""
    unsigned_payload_hash: Optional[str] = None
    signed_payload_hash: Optional[str] = None
    order_type: str = "FOK"
    time_in_force: str = "fill_or_kill"
    price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    post_only: bool = False
    reduce_only: Optional[bool] = None
    cancel_on_pause: Optional[bool] = True
