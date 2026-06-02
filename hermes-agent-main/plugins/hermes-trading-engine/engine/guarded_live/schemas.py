"""Guarded-live schemas (Phase 8). Design/dry-run only; no live execution."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GuardedLiveMode = Literal["disabled", "design_only", "dry_run_only"]

# States. There is intentionally NO LIVE_ACTIVE / REAL_MONEY_ACTIVE /
# PRODUCTION_EXECUTION / AUTO_LIVE / READY_FOR_AUTO_LIVE state.
GuardedLiveState = Literal[
    "DISABLED", "DESIGN_ONLY", "PRECHECK_FAILED", "PRECHECK_PASSED", "AWAITING_APPROVAL",
    "APPROVED_DRY_RUN_ONLY", "ARMED_DRY_RUN_ONLY", "DRY_RUN_ACTIVE", "PAUSED",
    "KILL_SWITCHED", "EXPIRED", "STOPPED", "FAILED"]

CheckStatus = Literal["PASS", "FAIL", "WARN"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _nid(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:16]}"


def _s(x) -> Optional[str]:
    return None if x is None else str(x)


def _json(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class PrecheckResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    check_name: str
    status: CheckStatus
    reason: str = ""
    observed_value: Optional[str] = None
    threshold: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class GuardedLivePrecheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    precheck_id: str = Field(default_factory=lambda: _nid("pre"))
    ts_ms: int = Field(default_factory=_now_ms)
    config_hash: str = ""
    readiness_report_id: Optional[str] = None
    status: CheckStatus = "FAIL"
    checks: list[PrecheckResult] = Field(default_factory=list)
    hard_fail_count: int = 0
    warning_count: int = 0

    def record(self) -> dict:
        return {"precheck_id": self.precheck_id, "ts_ms": self.ts_ms,
                "config_hash": self.config_hash, "readiness_report_id": self.readiness_report_id,
                "status": self.status, "hard_fail_count": self.hard_fail_count,
                "warning_count": self.warning_count,
                "payload_json": _json({"n": len(self.checks)})}


class ManualApproval(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_id: str = Field(default_factory=lambda: _nid("appr"))
    approval_batch_id: str = ""
    ts_ms: int = Field(default_factory=_now_ms)
    approver_id: str = ""
    role: str = ""
    readiness_report_id: str = ""
    config_hash: str = ""
    risk_limits_hash: str = ""
    approval_reason: str = ""
    confirmation_text: str = ""
    expires_ts_ms: int = 0
    revoked_ts_ms: Optional[int] = None
    status: Literal["ACTIVE", "EXPIRED", "REVOKED", "INVALIDATED"] = "ACTIVE"

    def record(self) -> dict:
        return {"approval_id": self.approval_id, "approval_batch_id": self.approval_batch_id,
                "ts_ms": self.ts_ms, "approver_id": self.approver_id, "role": self.role,
                "readiness_report_id": self.readiness_report_id, "config_hash": self.config_hash,
                "risk_limits_hash": self.risk_limits_hash, "approval_reason": self.approval_reason,
                "confirmation_text": self.confirmation_text, "expires_ts_ms": self.expires_ts_ms,
                "revoked_ts_ms": self.revoked_ts_ms, "status": self.status}


class ApprovalBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_batch_id: str = Field(default_factory=lambda: _nid("batch"))
    readiness_report_id: str = ""
    config_hash: str = ""
    required_approvals: int = 2
    valid_approvals: int = 0
    status: Literal["PENDING", "APPROVED_DRY_RUN_ONLY", "EXPIRED", "REVOKED",
                    "INVALIDATED"] = "PENDING"
    created_ts_ms: int = Field(default_factory=_now_ms)
    expires_ts_ms: int = 0

    def record(self) -> dict:
        return {"approval_batch_id": self.approval_batch_id,
                "readiness_report_id": self.readiness_report_id, "config_hash": self.config_hash,
                "required_approvals": self.required_approvals, "valid_approvals": self.valid_approvals,
                "status": self.status, "created_ts_ms": self.created_ts_ms,
                "expires_ts_ms": self.expires_ts_ms}


class ArmingTokenRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    arming_token_id: str = Field(default_factory=lambda: _nid("arm"))
    token_hash: str = ""
    approval_batch_id: str = ""
    readiness_report_id: str = ""
    config_hash: str = ""
    mode: str = "dry_run_only"
    created_ts_ms: int = Field(default_factory=_now_ms)
    expires_ts_ms: int = 0
    used_ts_ms: Optional[int] = None
    revoked_ts_ms: Optional[int] = None
    status: Literal["ACTIVE", "USED", "EXPIRED", "REVOKED", "INVALIDATED"] = "ACTIVE"

    def record(self) -> dict:
        return {"arming_token_id": self.arming_token_id, "token_hash": self.token_hash,
                "approval_batch_id": self.approval_batch_id,
                "readiness_report_id": self.readiness_report_id, "config_hash": self.config_hash,
                "mode": self.mode, "created_ts_ms": self.created_ts_ms,
                "expires_ts_ms": self.expires_ts_ms, "used_ts_ms": self.used_ts_ms,
                "revoked_ts_ms": self.revoked_ts_ms, "status": self.status}


class DryRunOrderIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    dry_run_intent_id: str = Field(default_factory=lambda: _nid("dry"))
    ts_ms: int = Field(default_factory=_now_ms)
    venue: str = "polymarket"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    side: str = "BUY"
    order_type: str = "LIMIT"
    limit_price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    internal_order_request: dict = Field(default_factory=dict)
    venue_payload: dict = Field(default_factory=dict)
    unsigned: bool = True
    unsent: bool = True
    signer_used: bool = False   # MUST remain False
    network_called: bool = False  # MUST remain False
    risk_decision_id: Optional[str] = None
    safety_envelope_decision_id: Optional[str] = None
    oms_order_id: Optional[str] = None
    status: Literal["VALIDATED", "REJECTED", "BLOCKED", "ERROR"] = "BLOCKED"
    reason: Optional[str] = None

    def record(self) -> dict:
        return {"dry_run_intent_id": self.dry_run_intent_id, "ts_ms": self.ts_ms,
                "venue": self.venue, "market_id": self.market_id,
                "market_ticker": self.market_ticker, "asset_id": self.asset_id,
                "outcome": self.outcome, "side": self.side, "order_type": self.order_type,
                "limit_price": _s(self.limit_price), "quantity": _s(self.quantity),
                "notional": _s(self.notional),
                "internal_order_request_json": _json(self.internal_order_request),
                "venue_payload_json": _json(self.venue_payload), "unsigned": int(self.unsigned),
                "unsent": int(self.unsent), "signer_used": int(self.signer_used),
                "network_called": int(self.network_called),
                "risk_decision_id": self.risk_decision_id,
                "safety_envelope_decision_id": self.safety_envelope_decision_id,
                "oms_order_id": self.oms_order_id, "status": self.status, "reason": self.reason}


class SafetyEnvelopeDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id: str = Field(default_factory=lambda: _nid("safe"))
    ts_ms: int = Field(default_factory=_now_ms)
    allowed: bool = False
    mode: str = "design_only"
    state: str = "DISABLED"
    reason: str = ""
    checks: dict[str, Any] = Field(default_factory=dict)
    config_hash: str = ""
    proposal_id: Optional[str] = None
    client_order_id: Optional[str] = None

    def record(self) -> dict:
        return {"decision_id": self.decision_id, "ts_ms": self.ts_ms, "allowed": int(self.allowed),
                "mode": self.mode, "state": self.state, "reason": self.reason,
                "checks_json": _json(self.checks), "config_hash": self.config_hash,
                "proposal_id": self.proposal_id, "client_order_id": self.client_order_id}


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    check_id: str = Field(default_factory=lambda: _nid("cfck"))
    conformance_run_id: str = ""
    check_name: str = ""
    status: CheckStatus = "PASS"
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    def record(self) -> dict:
        return {"check_id": self.check_id, "conformance_run_id": self.conformance_run_id,
                "check_name": self.check_name, "status": self.status, "reason": self.reason,
                "details_json": _json(self.details)}


class ConformanceRun(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conformance_run_id: str = Field(default_factory=lambda: _nid("conf"))
    started_ts_ms: int = Field(default_factory=_now_ms)
    finished_ts_ms: Optional[int] = None
    status: Literal["PASS", "FAIL", "ERROR"] = "ERROR"
    config_hash: str = ""
    test_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    warning_count: int = 0
    report_path: Optional[str] = None
    checks: list[ConformanceCheck] = Field(default_factory=list)

    def record(self) -> dict:
        return {"conformance_run_id": self.conformance_run_id, "started_ts_ms": self.started_ts_ms,
                "finished_ts_ms": self.finished_ts_ms, "status": self.status,
                "config_hash": self.config_hash, "test_count": self.test_count,
                "pass_count": self.pass_count, "fail_count": self.fail_count,
                "warning_count": self.warning_count, "report_path": self.report_path}


class SecretPolicyViolation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    violation_id: str = Field(default_factory=lambda: _nid("viol"))
    ts_ms: int = Field(default_factory=_now_ms)
    severity: str = "ERROR"
    location: str = ""
    violation_type: str = ""
    redacted_value: Optional[str] = None
    reason: str = ""

    def record(self) -> dict:
        return {"violation_id": self.violation_id, "ts_ms": self.ts_ms, "severity": self.severity,
                "location": self.location, "violation_type": self.violation_type,
                "redacted_value": self.redacted_value, "reason": self.reason,
                "payload_json": _json({})}
