"""ChangeControlWorkflow (Phase 11). Manual change-control record. Approval only
permits drafting a Phase 12 production-canary PLAN — it never enables execution."""

from __future__ import annotations

import time
from typing import Optional

from .jurisdiction import BOT_REVIEWERS
from .schemas import ChangeControlRecord, aggregate_status, make_check

NO_EXECUTION_STATEMENT = ("This change-control record authorizes production-canary DESIGN review "
                          "and drafting a Phase 12 plan ONLY. It does not authorize or enable "
                          "production order submission, cancellation, signing, size increase, or "
                          "autonomous live trading.")


def create_change_control(*, requester_id: str, reviewers: list, review_id: str,
                          risk_summary: str = "", evidence_refs: Optional[list] = None,
                          rollback_plan_ref: Optional[str] = None, expiry_hours: float = 24.0,
                          approve_design_only: bool = False,
                          now_ms=None) -> ChangeControlRecord:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return ChangeControlRecord(
        requester_id=requester_id, reviewers=list(reviewers or []), review_id=review_id,
        intended_scope="production_canary_design_review_only", risk_summary=risk_summary,
        evidence_refs=evidence_refs or [], rollback_plan_ref=rollback_plan_ref,
        no_execution_statement=NO_EXECUTION_STATEMENT,
        approval_status="APPROVED_DESIGN_ONLY" if approve_design_only else "PENDING",
        ts_ms=now, expires_ts_ms=now + int(expiry_hours * 3600_000))


def validate(ctx: dict, cfg, *, now_ms=None):
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cc = ctx.get("change_control")
    checks = []
    if cfg.require_change_control and not cc:
        checks.append(make_check("change_control", "change_control_record_present", "FAIL",
                                 "CRITICAL", "no change-control record"))
        return aggregate_status(checks), checks, None
    if not cc:
        return "NOT_APPLICABLE", checks, None
    checks.append(make_check("change_control", "no_execution_statement_present",
                             "PASS" if (cc.get("no_execution_statement") or "").strip() else "FAIL",
                             "CRITICAL"))
    reviewers = cc.get("reviewers") or []
    human_reviewers = [r for r in reviewers if str(r).lower() not in BOT_REVIEWERS]
    checks.append(make_check("change_control", "enough_human_reviewers",
                             "PASS" if len(human_reviewers) >= cfg.required_human_reviewers
                             else "FAIL", "ERROR", observed=len(human_reviewers),
                             expected=cfg.required_human_reviewers))
    expired = int(cc.get("expires_ts_ms", 0)) and now > int(cc["expires_ts_ms"])
    checks.append(make_check("change_control", "not_expired", "FAIL" if expired else "PASS",
                             "ERROR"))
    checks.append(make_check("change_control", "scope_is_design_review_only",
                             "PASS" if "design" in str(cc.get("intended_scope", "")).lower()
                             else "FAIL", "CRITICAL", observed=cc.get("intended_scope")))
    checks.append(make_check("change_control", "approval_status",
                             "PASS" if cc.get("approval_status") in
                             ("APPROVED_DESIGN_ONLY", "PENDING") else "FAIL", "ERROR",
                             observed=cc.get("approval_status")))
    rec = ChangeControlRecord(**{k: cc.get(k) for k in ChangeControlRecord.model_fields if k in cc})
    return aggregate_status(checks), checks, rec
