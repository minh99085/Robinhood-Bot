"""Post-canary schemas (Phase 10). Analysis + veto only. Decimal for money."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PostCanaryStatus = Literal["CREATED", "RUNNING", "CLEAN", "CLEAN_BUT_NOT_ENOUGH_DATA",
                           "WARN_REQUIRES_REVIEW", "FAIL", "UNKNOWN_BLOCKING", "ERROR"]

PostCanaryRecommendation = Literal[
    "STOP", "FIX_AND_REPEAT_SHADOW", "REPEAT_DEMO_CANARY_SAME_SIZE",
    "MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN", "MANUAL_REVIEW_FOR_NEXT_PHASE"]

# Recommendations that must NEVER be produced by Phase 10.
FORBIDDEN_RECOMMENDATIONS = {
    "AUTO_SCALE", "INCREASE_SIZE", "ENABLE_AUTONOMOUS_LIVE", "ENABLE_PRODUCTION",
    "READY_FOR_PRODUCTION", "READY_FOR_LIVE_LOOP", "ENABLE_LIVE", "READY_FOR_AUTONOMOUS_TRADING"}

CheckStatus = Literal["PASS", "WARN", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
Severity = Literal["INFO", "WARN", "ERROR", "CRITICAL"]


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


def make_check(name, status="PASS", severity="INFO", reason="", observed=None, expected=None,
               threshold=None, details=None) -> "AuditCheckResult":
    return AuditCheckResult(check_name=name, status=status, severity=severity, reason=reason,
                            observed_value=_s(observed), expected_value=_s(expected),
                            threshold=_s(threshold), details=details)


_RANK = {"PASS": 0, "NOT_APPLICABLE": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 3}


def aggregate_status(checks) -> str:
    """Worst-of: UNKNOWN > FAIL > WARN > PASS."""
    worst = "PASS"
    for c in checks:
        if _RANK.get(c.status, 0) > _RANK.get(worst, 0):
            worst = c.status
    return worst


class PostCanaryAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    analysis_id: Optional[str] = None
    live_order_attempt_id: str = ""
    canary_plan_id: Optional[str] = None
    refresh_readonly_exchange_state: bool = False
    include_market_data_window: bool = True
    include_shadow_context: bool = True
    include_research_context: bool = True
    generated_by: str = "cli"
    notes: Optional[str] = None


class AuditCheckResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    check_name: str
    status: CheckStatus = "PASS"
    severity: Severity = "INFO"
    reason: str = ""
    observed_value: Optional[str] = None
    expected_value: Optional[str] = None
    threshold: Optional[str] = None
    details: Optional[dict] = None


def _checks_json(checks) -> str:
    return _j([c.model_dump() for c in checks])


class _WithChecks(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: CheckStatus = "PASS"
    checks: list[AuditCheckResult] = Field(default_factory=list)


class ReconciliationAuditResult(_WithChecks):
    exchange_status: Optional[str] = None
    local_status: Optional[str] = None
    filled_quantity: Optional[Decimal] = None
    local_filled_quantity: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    local_fee: Optional[Decimal] = None
    position_delta: Optional[Decimal] = None
    local_position_delta: Optional[Decimal] = None
    discrepancies: list = Field(default_factory=list)

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status,
                "exchange_status": self.exchange_status, "local_status": self.local_status,
                "filled_quantity": _s(self.filled_quantity),
                "local_filled_quantity": _s(self.local_filled_quantity), "fee": _s(self.fee),
                "local_fee": _s(self.local_fee), "position_delta": _s(self.position_delta),
                "local_position_delta": _s(self.local_position_delta),
                "discrepancies_json": _j(self.discrepancies)}


class ExecutionQualityResult(_WithChecks):
    intended_price: Optional[Decimal] = None
    submitted_price: Optional[Decimal] = None
    avg_fill_price: Optional[Decimal] = None
    intended_quantity: Optional[Decimal] = None
    filled_quantity: Optional[Decimal] = None
    intended_notional: Optional[Decimal] = None
    filled_notional: Optional[Decimal] = None
    slippage_bps: Optional[Decimal] = None
    payload_drift_detected: bool = False
    unexpected_partial_fill: bool = False
    unexpected_resting_order: bool = False
    details: Optional[dict] = None

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status,
                "intended_price": _s(self.intended_price), "submitted_price": _s(self.submitted_price),
                "avg_fill_price": _s(self.avg_fill_price),
                "intended_quantity": _s(self.intended_quantity),
                "filled_quantity": _s(self.filled_quantity),
                "intended_notional": _s(self.intended_notional),
                "filled_notional": _s(self.filled_notional), "slippage_bps": _s(self.slippage_bps),
                "payload_drift_detected": int(self.payload_drift_detected),
                "unexpected_partial_fill": int(self.unexpected_partial_fill),
                "unexpected_resting_order": int(self.unexpected_resting_order),
                "details_json": _j(self.details or {})}


class MarkoutObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    horizon_ms: int
    observed_ts_ms: Optional[int] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    midpoint: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    last_trade_price: Optional[Decimal] = None
    markout_vs_mid: Optional[Decimal] = None
    markout_vs_touch: Optional[Decimal] = None
    adverse_selection: Optional[Decimal] = None
    data_missing: bool = False
    details: Optional[dict] = None

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "horizon_ms": self.horizon_ms,
                "observed_ts_ms": self.observed_ts_ms, "best_bid": _s(self.best_bid),
                "best_ask": _s(self.best_ask), "midpoint": _s(self.midpoint),
                "spread": _s(self.spread), "last_trade_price": _s(self.last_trade_price),
                "markout_vs_mid": _s(self.markout_vs_mid),
                "markout_vs_touch": _s(self.markout_vs_touch),
                "adverse_selection": _s(self.adverse_selection),
                "data_missing": int(self.data_missing), "details_json": _j(self.details or {})}


class MarkoutAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: CheckStatus = "PASS"
    observations: list[MarkoutObservation] = Field(default_factory=list)
    worst_markout_bps: Optional[Decimal] = None
    best_markout_bps: Optional[Decimal] = None
    markout_5s_bps: Optional[Decimal] = None
    markout_60s_bps: Optional[Decimal] = None
    markout_5m_bps: Optional[Decimal] = None
    edge_capture_ratio: Optional[Decimal] = None
    adverse_selection_detected: bool = False
    details: Optional[dict] = None


class MarketDataAuditResult(_WithChecks):
    bbo_age_ms: Optional[int] = None
    orderbook_age_ms: Optional[int] = None
    spread: Optional[Decimal] = None
    depth_at_limit: Optional[Decimal] = None
    sequence_gap_detected: bool = False
    tick_dirty: bool = False
    venue_status: Optional[str] = None
    market_status: Optional[str] = None

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status, "bbo_age_ms": self.bbo_age_ms,
                "orderbook_age_ms": self.orderbook_age_ms, "spread": _s(self.spread),
                "depth_at_limit": _s(self.depth_at_limit),
                "sequence_gap_detected": int(self.sequence_gap_detected),
                "tick_dirty": int(self.tick_dirty), "venue_status": self.venue_status,
                "market_status": self.market_status, "details_json": _checks_json(self.checks)}


class ResearchAuditResult(_WithChecks):
    estimate_id: Optional[str] = None
    p_ensemble: Optional[Decimal] = None
    confidence: Optional[Decimal] = None
    evidence_score: Optional[Decimal] = None
    source_count: Optional[int] = None
    ambiguity_score: Optional[Decimal] = None
    stale: bool = False
    no_trade_reason: Optional[str] = None

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status, "estimate_id": self.estimate_id,
                "p_ensemble": _s(self.p_ensemble), "confidence": _s(self.confidence),
                "evidence_score": _s(self.evidence_score), "source_count": self.source_count,
                "ambiguity_score": _s(self.ambiguity_score), "stale": int(self.stale),
                "no_trade_reason": self.no_trade_reason, "details_json": _checks_json(self.checks)}


class RiskAuditResult(_WithChecks):
    risk_decision_id: Optional[str] = None
    safety_envelope_decision_id: Optional[str] = None
    risk_approved: bool = False
    safety_allowed: bool = False
    bypass_detected: bool = False
    limit_breach_detected: bool = False

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status,
                "risk_decision_id": self.risk_decision_id,
                "safety_envelope_decision_id": self.safety_envelope_decision_id,
                "risk_approved": int(self.risk_approved), "safety_allowed": int(self.safety_allowed),
                "bypass_detected": int(self.bypass_detected),
                "limit_breach_detected": int(self.limit_breach_detected),
                "details_json": _checks_json(self.checks)}


class ChainAuditResult(_WithChecks):
    missing_links: list = Field(default_factory=list)
    audit_chain_hash_valid: Optional[bool] = None
    trace: Optional[dict] = None

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status,
                "missing_links_json": _j(self.missing_links),
                "audit_chain_hash_valid": (None if self.audit_chain_hash_valid is None
                                           else int(self.audit_chain_hash_valid)),
                "trace_json": _j(self.trace or {})}


class SecretAuditResult(_WithChecks):
    secret_leak_count: int = 0
    redaction_count: int = 0
    violations: list = Field(default_factory=list)

    def record(self, analysis_id) -> dict:
        return {"analysis_id": analysis_id, "status": self.status,
                "secret_leak_count": self.secret_leak_count,
                "redaction_count": self.redaction_count, "violations_json": _j(self.violations)}


class PostCanaryAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    analysis_id: str = Field(default_factory=lambda: _nid("pca"))
    live_order_attempt_id: str = ""
    canary_plan_id: Optional[str] = None
    ts_ms: int = Field(default_factory=_now_ms)
    status: PostCanaryStatus = "CREATED"
    recommendation: PostCanaryRecommendation = "STOP"
    hard_fail_count: int = 0
    warning_count: int = 0
    unknown_blocking_count: int = 0
    clean_for_repeat_demo_same_size: bool = False
    eligible_for_production_design_review: bool = False
    eligible_for_size_increase: bool = False        # always False
    eligible_for_autonomous_live: bool = False      # always False
    reconciliation: Optional[ReconciliationAuditResult] = None
    execution_quality: Optional[ExecutionQualityResult] = None
    markout: Optional[MarkoutAnalysisResult] = None
    market_data: Optional[MarketDataAuditResult] = None
    research: Optional[ResearchAuditResult] = None
    risk: Optional[RiskAuditResult] = None
    chain: Optional[ChainAuditResult] = None
    secrets: Optional[SecretAuditResult] = None
    summary: str = ""
    blocking_reasons: list = Field(default_factory=list)
    next_required_actions: list = Field(default_factory=list)

    def record(self, report_path: Optional[str] = None) -> dict:
        return {"analysis_id": self.analysis_id, "live_order_attempt_id": self.live_order_attempt_id,
                "canary_plan_id": self.canary_plan_id, "ts_ms": self.ts_ms, "status": self.status,
                "recommendation": self.recommendation, "hard_fail_count": self.hard_fail_count,
                "warning_count": self.warning_count,
                "unknown_blocking_count": self.unknown_blocking_count,
                "clean_for_repeat_demo_same_size": int(self.clean_for_repeat_demo_same_size),
                "eligible_for_production_design_review":
                    int(self.eligible_for_production_design_review),
                "eligible_for_size_increase": 0, "eligible_for_autonomous_live": 0,
                "summary_json": _j({"summary": self.summary}),
                "blocking_reasons_json": _j(self.blocking_reasons),
                "next_required_actions_json": _j(self.next_required_actions),
                "report_path": report_path}

    def all_checks(self):
        out = []
        for cat in ("reconciliation", "execution_quality", "market_data", "research", "risk",
                    "chain", "secrets"):
            sub = getattr(self, cat, None)
            if sub and getattr(sub, "checks", None):
                for c in sub.checks:
                    out.append((cat, c))
        return out


class CanaryEligibilitySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    venue: str = "kalshi"
    environment: str = "demo"
    total_canaries: int = 0
    clean_canaries: int = 0
    failed_canaries: int = 0
    unresolved_canaries: int = 0
    emergency_cancel_count: int = 0
    clean_demo_canary_streak: int = 0
    last_clean_canary_ts_ms: Optional[int] = None
    renewed_shadow_hours_after_last_canary: Optional[Decimal] = None
    renewed_shadow_decisions_after_last_canary: Optional[int] = None
    eligible_repeat_demo_same_size: bool = False
    eligible_production_design_review: bool = False
    eligible_size_increase: bool = False  # always False
    reason: str = ""

    def record(self) -> dict:
        return {"venue": self.venue, "environment": self.environment,
                "ts_ms": _now_ms(), "total_canaries": self.total_canaries,
                "clean_canaries": self.clean_canaries, "failed_canaries": self.failed_canaries,
                "unresolved_canaries": self.unresolved_canaries,
                "emergency_cancel_count": self.emergency_cancel_count,
                "clean_demo_canary_streak": self.clean_demo_canary_streak,
                "last_clean_canary_ts_ms": self.last_clean_canary_ts_ms,
                "renewed_shadow_hours_after_last_canary":
                    _s(self.renewed_shadow_hours_after_last_canary),
                "renewed_shadow_decisions_after_last_canary":
                    self.renewed_shadow_decisions_after_last_canary,
                "eligible_repeat_demo_same_size": int(self.eligible_repeat_demo_same_size),
                "eligible_production_design_review": int(self.eligible_production_design_review),
                "eligible_size_increase": 0, "reason": self.reason}
