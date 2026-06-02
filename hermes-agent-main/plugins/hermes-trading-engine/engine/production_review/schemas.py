"""Production-review schemas (Phase 11). Design review only; never execution."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ProductionReviewStatus = Literal["CREATED", "RUNNING", "PASS_DESIGN_REVIEW_ONLY",
                                 "WARN_REQUIRES_REVIEW", "FAIL", "BLOCKED", "ERROR"]

ProductionReviewRecommendation = Literal[
    "NOT_READY", "FIX_AND_REPEAT_SHADOW", "FIX_AND_REPEAT_DEMO_CANARIES",
    "READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW",
    "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"]

# Recommendations that must NEVER be returned by Phase 11.
FORBIDDEN_PRODUCTION_RECOMMENDATIONS = {
    "READY_FOR_PRODUCTION_EXECUTION", "ENABLE_PRODUCTION", "AUTO_PRODUCTION",
    "INCREASE_SIZE", "ENABLE_AUTONOMOUS_LIVE"}

CheckStatus = Literal["PASS", "WARN", "FAIL", "BLOCKED", "NOT_APPLICABLE"]
Severity = Literal["INFO", "WARN", "ERROR", "CRITICAL"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _nid(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:16]}"


def _j(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class ProductionReviewCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    check_id: str = Field(default_factory=lambda: _nid("chk"))
    category: str = ""
    check_name: str = ""
    status: CheckStatus = "PASS"
    severity: Severity = "INFO"
    reason: str = ""
    observed_value: Optional[str] = None
    expected_value: Optional[str] = None
    evidence_ref: Optional[str] = None
    details: Optional[dict] = None


def make_check(category, name, status="PASS", severity="INFO", reason="", observed=None,
               expected=None, evidence_ref=None, details=None) -> ProductionReviewCheck:
    return ProductionReviewCheck(
        category=category, check_name=name, status=status, severity=severity, reason=reason,
        observed_value=(None if observed is None else str(observed)),
        expected_value=(None if expected is None else str(expected)), evidence_ref=evidence_ref,
        details=details)


_RANK = {"PASS": 0, "NOT_APPLICABLE": 0, "WARN": 1, "FAIL": 2, "BLOCKED": 3}


def aggregate_status(checks) -> str:
    worst = "PASS"
    for c in checks:
        if _RANK.get(c.status, 0) > _RANK.get(worst, 0):
            worst = c.status
    return worst


class _WithChecks(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: CheckStatus = "PASS"
    checks: list[ProductionReviewCheck] = Field(default_factory=list)


class ProductionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_id: Optional[str] = None
    generated_by: str = "cli"
    include_readonly_account_snapshot: bool = False
    include_mock_production_conformance: bool = True
    include_artifacts: bool = True
    notes: Optional[str] = None


class ProductionEvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str = Field(default_factory=lambda: _nid("ev"))
    ts_ms: int = Field(default_factory=_now_ms)
    latest_shadow_report_id: Optional[str] = None
    latest_post_canary_analysis_id: Optional[str] = None
    clean_demo_canary_count: int = 0
    unresolved_canary_count: int = 0
    failed_canary_count: int = 0
    renewed_shadow_hours: Optional[float] = None
    renewed_shadow_decisions: Optional[int] = None
    guarded_live_conformance_status: Optional[str] = None
    micro_live_conformance_status: Optional[str] = None
    post_canary_eligibility_status: Optional[str] = None
    missing_evidence: list = Field(default_factory=list)
    stale_evidence: list = Field(default_factory=list)

    def record(self, review_id) -> dict:
        return {"evidence_id": self.evidence_id, "review_id": review_id, "ts_ms": self.ts_ms,
                "latest_shadow_report_id": self.latest_shadow_report_id,
                "latest_post_canary_analysis_id": self.latest_post_canary_analysis_id,
                "clean_demo_canary_count": self.clean_demo_canary_count,
                "unresolved_canary_count": self.unresolved_canary_count,
                "failed_canary_count": self.failed_canary_count,
                "renewed_shadow_hours": (None if self.renewed_shadow_hours is None
                                         else str(self.renewed_shadow_hours)),
                "renewed_shadow_decisions": self.renewed_shadow_decisions,
                "guarded_live_conformance_status": self.guarded_live_conformance_status,
                "micro_live_conformance_status": self.micro_live_conformance_status,
                "post_canary_eligibility_status": self.post_canary_eligibility_status,
                "missing_evidence_json": _j(self.missing_evidence),
                "stale_evidence_json": _j(self.stale_evidence)}


class AccountReadinessResult(_WithChecks):
    venue_accounts_reviewed: list = Field(default_factory=list)
    production_account_attested: bool = False
    read_only_snapshot_used: bool = False
    funding_or_collateral_attested: bool = False
    restrictions_attested_clear: bool = False
    no_funds_moved: bool = True  # always true in Phase 11

    def record(self, review_id) -> dict:
        return {"review_id": review_id, "status": self.status,
                "venue_accounts_reviewed_json": _j(self.venue_accounts_reviewed),
                "production_account_attested": int(self.production_account_attested),
                "read_only_snapshot_used": int(self.read_only_snapshot_used),
                "funding_or_collateral_attested": int(self.funding_or_collateral_attested),
                "restrictions_attested_clear": int(self.restrictions_attested_clear),
                "no_funds_moved": 1}


class VenuePermissionResult(_WithChecks):
    venue: str = "kalshi"
    environment_separation_passed: bool = False
    read_only_key_separated: Optional[bool] = None
    trading_key_custody_plan_present: Optional[bool] = None
    private_user_channels_disabled: bool = True
    order_endpoints_blocked: bool = True
    forbidden_flows_blocked: bool = True

    def record(self, review_id) -> dict:
        def _ni(v):
            return None if v is None else int(v)
        return {"review_id": review_id, "venue": self.venue, "status": self.status,
                "environment_separation_passed": int(self.environment_separation_passed),
                "read_only_key_separated": _ni(self.read_only_key_separated),
                "trading_key_custody_plan_present": _ni(self.trading_key_custody_plan_present),
                "private_user_channels_disabled": int(self.private_user_channels_disabled),
                "order_endpoints_blocked": int(self.order_endpoints_blocked),
                "forbidden_flows_blocked": int(self.forbidden_flows_blocked),
                "details_json": _j([c.model_dump() for c in self.checks])}


class JurisdictionEligibilityAttestation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    attestation_id: str = Field(default_factory=lambda: _nid("attest"))
    ts_ms: int = Field(default_factory=_now_ms)
    reviewer_id: str = ""
    venue: str = "kalshi"
    account_identifier_redacted: Optional[str] = None
    jurisdiction_reviewed: bool = False
    eligibility_confirmed_by_operator: bool = False
    venue_terms_reviewed: bool = False
    prohibited_market_categories_reviewed: bool = False
    tax_reporting_out_of_scope_acknowledged: bool = False
    legal_advice_not_provided_acknowledged: bool = False
    confirmation_text: str = ""
    expires_ts_ms: int = 0
    status: str = "ACTIVE"

    def record(self) -> dict:
        return {"attestation_id": self.attestation_id, "ts_ms": self.ts_ms,
                "reviewer_id": self.reviewer_id, "venue": self.venue,
                "account_identifier_redacted": self.account_identifier_redacted,
                "jurisdiction_reviewed": int(self.jurisdiction_reviewed),
                "eligibility_confirmed_by_operator": int(self.eligibility_confirmed_by_operator),
                "venue_terms_reviewed": int(self.venue_terms_reviewed),
                "prohibited_market_categories_reviewed": int(self.prohibited_market_categories_reviewed),
                "tax_reporting_out_of_scope_acknowledged": int(self.tax_reporting_out_of_scope_acknowledged),
                "legal_advice_not_provided_acknowledged": int(self.legal_advice_not_provided_acknowledged),
                "confirmation_text": self.confirmation_text, "expires_ts_ms": self.expires_ts_ms,
                "revoked_ts_ms": None, "status": self.status}


class EndpointSeparationResult(_WithChecks):
    api_submit_routes_found: int = 0
    dashboard_submit_controls_found: int = 0
    strategy_production_paths_found: int = 0
    grok_production_paths_found: int = 0
    production_order_endpoint_reachable: bool = False
    read_only_endpoints_isolated: bool = True

    def record(self, review_id) -> dict:
        return {"review_id": review_id, "status": self.status,
                "api_submit_routes_found": self.api_submit_routes_found,
                "dashboard_submit_controls_found": self.dashboard_submit_controls_found,
                "strategy_production_paths_found": self.strategy_production_paths_found,
                "grok_production_paths_found": self.grok_production_paths_found,
                "production_order_endpoint_reachable": int(self.production_order_endpoint_reachable),
                "read_only_endpoints_isolated": int(self.read_only_endpoints_isolated),
                "details_json": _j([c.model_dump() for c in self.checks])}


class CredentialCustodyResult(_WithChecks):
    raw_secret_findings: int = 0
    redaction_findings: int = 0
    production_signer_loaded: bool = False
    wallet_private_key_loaded: bool = False
    db_secret_findings: int = 0
    artifact_secret_findings: int = 0
    custody_plan_present: bool = False
    rotation_plan_present: bool = False
    revocation_plan_present: bool = False

    def record(self, review_id) -> dict:
        return {"review_id": review_id, "status": self.status,
                "raw_secret_findings": self.raw_secret_findings,
                "redaction_findings": self.redaction_findings,
                "production_signer_loaded": int(self.production_signer_loaded),
                "wallet_private_key_loaded": int(self.wallet_private_key_loaded),
                "db_secret_findings": self.db_secret_findings,
                "artifact_secret_findings": self.artifact_secret_findings,
                "custody_plan_present": int(self.custody_plan_present),
                "rotation_plan_present": int(self.rotation_plan_present),
                "revocation_plan_present": int(self.revocation_plan_present),
                "details_json": _j([c.model_dump() for c in self.checks])}


class ProductionConformanceRun(_WithChecks):
    conformance_run_id: str = Field(default_factory=lambda: _nid("pconf"))
    ts_ms: int = Field(default_factory=_now_ms)
    mock_only: bool = True
    real_network_calls: int = 0
    production_order_calls: int = 0
    production_cancel_calls: int = 0
    production_signer_calls: int = 0
    report_path: Optional[str] = None

    def record(self, review_id=None) -> dict:
        return {"conformance_run_id": self.conformance_run_id, "review_id": review_id,
                "ts_ms": self.ts_ms, "status": self.status, "mock_only": int(self.mock_only),
                "real_network_calls": self.real_network_calls,
                "production_order_calls": self.production_order_calls,
                "production_cancel_calls": self.production_cancel_calls,
                "production_signer_calls": self.production_signer_calls,
                "report_path": self.report_path,
                "summary_json": _j([c.model_dump() for c in self.checks])}


class OperationalReadinessResult(_WithChecks):
    runbook_present: bool = False
    monitoring_plan_present: bool = False
    incident_response_present: bool = False
    rollback_plan_present: bool = False
    emergency_contact_placeholder_present: bool = False
    manual_exchange_ui_checklist_present: bool = False

    def record(self, review_id) -> dict:
        return {"review_id": review_id, "status": self.status,
                "runbook_present": int(self.runbook_present),
                "monitoring_plan_present": int(self.monitoring_plan_present),
                "incident_response_present": int(self.incident_response_present),
                "rollback_plan_present": int(self.rollback_plan_present),
                "emergency_contact_placeholder_present": int(self.emergency_contact_placeholder_present),
                "manual_exchange_ui_checklist_present": int(self.manual_exchange_ui_checklist_present),
                "details_json": _j([c.model_dump() for c in self.checks])}


class ChangeControlRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    change_id: str = Field(default_factory=lambda: _nid("cc"))
    ts_ms: int = Field(default_factory=_now_ms)
    requester_id: str = ""
    reviewers: list = Field(default_factory=list)
    review_id: str = ""
    intended_scope: str = "production_canary_design_review_only"
    risk_summary: str = ""
    evidence_refs: list = Field(default_factory=list)
    rollback_plan_ref: Optional[str] = None
    no_execution_statement: str = ""
    approval_status: str = "PENDING"
    expires_ts_ms: int = 0

    def record(self) -> dict:
        return {"change_id": self.change_id, "ts_ms": self.ts_ms, "requester_id": self.requester_id,
                "reviewers_json": _j(self.reviewers), "review_id": self.review_id,
                "intended_scope": self.intended_scope, "risk_summary": self.risk_summary,
                "evidence_refs_json": _j(self.evidence_refs),
                "rollback_plan_ref": self.rollback_plan_ref,
                "no_execution_statement": self.no_execution_statement,
                "approval_status": self.approval_status, "expires_ts_ms": self.expires_ts_ms}


class HumanChecklistResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    checklist_id: str = Field(default_factory=lambda: _nid("hc"))
    ts_ms: int = Field(default_factory=_now_ms)
    reviewer_id: str = ""
    review_id: str = ""
    checklist_items: list[ProductionReviewCheck] = Field(default_factory=list)
    all_required_items_passed: bool = False
    confirmation_text: str = ""
    status: str = "INCOMPLETE"

    def record(self) -> dict:
        return {"checklist_id": self.checklist_id, "ts_ms": self.ts_ms,
                "reviewer_id": self.reviewer_id, "review_id": self.review_id,
                "all_required_items_passed": int(self.all_required_items_passed),
                "confirmation_text": self.confirmation_text, "status": self.status,
                "items_json": _j([c.model_dump() for c in self.checklist_items])}


class ProductionReviewResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_id: str = Field(default_factory=lambda: _nid("prev"))
    ts_ms: int = Field(default_factory=_now_ms)
    status: ProductionReviewStatus = "CREATED"
    recommendation: ProductionReviewRecommendation = "NOT_READY"
    hard_fail_count: int = 0
    warning_count: int = 0
    blocked_count: int = 0
    evidence_summary: Optional[ProductionEvidenceSummary] = None
    account_readiness: Optional[AccountReadinessResult] = None
    venue_permissions: list[VenuePermissionResult] = Field(default_factory=list)
    jurisdiction_attestations: list[JurisdictionEligibilityAttestation] = Field(default_factory=list)
    endpoint_separation: Optional[EndpointSeparationResult] = None
    credential_custody: Optional[CredentialCustodyResult] = None
    production_conformance: Optional[ProductionConformanceRun] = None
    operational_readiness: Optional[OperationalReadinessResult] = None
    change_control: Optional[ChangeControlRecord] = None
    human_checklist: Optional[HumanChecklistResult] = None
    blocking_reasons: list = Field(default_factory=list)
    next_required_actions: list = Field(default_factory=list)
    eligible_to_draft_phase12_plan: bool = False
    eligible_for_production_execution: bool = False  # always False
    eligible_for_size_increase: bool = False         # always False
    eligible_for_autonomous_live: bool = False       # always False
    summary: str = ""

    def all_checks(self):
        out = []
        for cat in ("account_readiness", "endpoint_separation", "credential_custody",
                    "production_conformance", "operational_readiness"):
            sub = getattr(self, cat, None)
            if sub and getattr(sub, "checks", None):
                for c in sub.checks:
                    out.append((cat, c))
        for vp in self.venue_permissions:
            for c in vp.checks:
                out.append(("venue_permissions", c))
        return out

    def record(self, report_path=None) -> dict:
        return {"review_id": self.review_id, "ts_ms": self.ts_ms, "status": self.status,
                "recommendation": self.recommendation, "generated_by": "review",
                "hard_fail_count": self.hard_fail_count, "warning_count": self.warning_count,
                "blocked_count": self.blocked_count,
                "eligible_to_draft_phase12_plan": int(self.eligible_to_draft_phase12_plan),
                "eligible_for_production_execution": 0, "eligible_for_size_increase": 0,
                "eligible_for_autonomous_live": 0,
                "blocking_reasons_json": _j(self.blocking_reasons),
                "next_required_actions_json": _j(self.next_required_actions),
                "summary_json": _j({"summary": self.summary}), "report_path": report_path}
