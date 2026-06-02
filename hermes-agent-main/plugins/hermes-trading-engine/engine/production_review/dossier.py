"""Production-review dossier orchestrator (Phase 11). Runs all sub-reviews,
applies the veto gate, and persists results + artifacts. NEVER executes,
submits, cancels, signs, scales, or moves funds."""

from __future__ import annotations

from typing import Optional

from . import (account_readiness, audit, change_control, credential_custody, endpoint_separation,
               evidence_loader, human_checklist, jurisdiction, operational_readiness,
               production_conformance, venue_permissions, veto)
from .config import ProductionReviewConfig
from .schemas import ProductionReviewRequest, ProductionReviewResult


def run_review(store, config: Optional[ProductionReviewConfig] = None, *,
               request: Optional[ProductionReviewRequest] = None,
               fixture: Optional[dict] = None, write_report: bool = True) -> ProductionReviewResult:
    cfg = config or ProductionReviewConfig.from_env()
    request = request or ProductionReviewRequest()
    ctx = evidence_loader.load(store, cfg, fixture=fixture)
    ev = ctx.get("evidence_summary")

    res = ProductionReviewResult(status="RUNNING", evidence_summary=ev)
    res.account_readiness = account_readiness.run(ctx, cfg)
    res.venue_permissions = venue_permissions.run(ctx, cfg)
    jur_status, jur_checks, jur_valid = jurisdiction.validate(ctx, cfg)
    res.jurisdiction_attestations = jur_valid
    res.endpoint_separation = endpoint_separation.run(ctx, cfg)
    res.credential_custody = credential_custody.run(ctx, cfg)
    if request.include_mock_production_conformance:
        res.production_conformance = production_conformance.run(cfg, ctx=ctx)
    res.operational_readiness = operational_readiness.run(ctx, cfg)
    cc_status, cc_checks, cc_rec = change_control.validate(ctx, cfg)
    res.change_control = cc_rec
    hc_status, hc_checks, hc_rec = human_checklist.validate(ctx, cfg)
    res.human_checklist = hc_rec

    extra_checks = list(jur_checks) + list(cc_checks) + list(hc_checks)
    all_checks = [c for _, c in res.all_checks()] + extra_checks

    critical_fail = any(c.status in ("FAIL", "BLOCKED") and c.severity == "CRITICAL"
                        for c in all_checks)
    hard_fail = sum(1 for c in all_checks if c.status == "FAIL")
    warnings = sum(1 for c in all_checks if c.status == "WARN")
    blocked = sum(1 for c in all_checks if c.status == "BLOCKED")

    # evidence flags
    demo_insufficient = (
        ev.clean_demo_canary_count < cfg.min_clean_demo_canaries
        or (cfg.require_all_canaries_clean and ev.failed_canary_count > 0)
        or (cfg.require_no_unresolved_canaries and ev.unresolved_canary_count > 0))
    shadow_insufficient = (
        ev.renewed_shadow_hours is None
        or float(ev.renewed_shadow_hours) < cfg.min_renewed_shadow_hours
        or ev.renewed_shadow_decisions is None
        or int(ev.renewed_shadow_decisions) < cfg.min_renewed_shadow_decisions)

    conf_ok = (res.production_conformance is None
               or res.production_conformance.status == "PASS")
    env_attempt = ProductionReviewConfig.production_enable_attempt_detected()
    other_blocking = (
        not conf_ok
        or (cfg.require_phase8_conformance and ev.guarded_live_conformance_status != "PASS")
        or (cfg.require_phase9_conformance and ev.micro_live_conformance_status != "PASS")
        or (cfg.require_phase10_eligibility and ev.post_canary_eligibility_status != "eligible")
        or res.account_readiness.status in ("FAIL", "BLOCKED")
        or any(v.status in ("FAIL", "BLOCKED") for v in res.venue_permissions)
        or jur_status in ("FAIL", "BLOCKED")
        or res.endpoint_separation.status in ("FAIL", "BLOCKED")
        or res.credential_custody.status in ("FAIL", "BLOCKED")
        or res.operational_readiness.status in ("FAIL", "BLOCKED")
        or cc_status in ("FAIL", "BLOCKED")
        or bool(getattr(ev, "stale_evidence", []))
        or bool(getattr(ev, "missing_evidence", []))
        or bool(env_attempt))

    all_clean = not (critical_fail or demo_insufficient or shadow_insufficient or other_blocking)
    cc_approved = bool(cc_rec and cc_rec.approval_status == "APPROVED_DESIGN_ONLY"
                       and cc_status not in ("FAIL", "BLOCKED"))
    hc_passed = bool(hc_rec and hc_status == "PASS")

    rec = veto.decide(critical_fail=critical_fail, shadow_insufficient=shadow_insufficient,
                      demo_insufficient=demo_insufficient, other_blocking=other_blocking,
                      all_clean=all_clean, change_control_approved=cc_approved,
                      human_checklist_passed=hc_passed)
    res.recommendation = veto.assert_safe(rec)

    if res.recommendation in ("READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW",
                              "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"):
        res.status = "PASS_DESIGN_REVIEW_ONLY"
    elif critical_fail:
        res.status = "FAIL"
    elif blocked:
        res.status = "BLOCKED"
    else:
        res.status = "WARN_REQUIRES_REVIEW"

    res.hard_fail_count = hard_fail
    res.warning_count = warnings
    res.blocked_count = blocked
    res.eligible_to_draft_phase12_plan = (res.recommendation ==
                                          "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN")
    res.eligible_for_production_execution = False
    res.eligible_for_size_increase = False
    res.eligible_for_autonomous_live = False

    reasons = []
    if env_attempt:
        reasons.append("env attempt to enable production execution: " + ",".join(env_attempt))
    if critical_fail:
        reasons += [f"{c.category}.{c.check_name}" for c in all_checks
                    if c.status in ("FAIL", "BLOCKED") and c.severity == "CRITICAL"][:10]
    if demo_insufficient:
        reasons.append(f"clean demo canaries {ev.clean_demo_canary_count} < "
                       f"{cfg.min_clean_demo_canaries} (or failed/unresolved present)")
    if shadow_insufficient:
        reasons.append("renewed shadow evidence insufficient")
    res.blocking_reasons = reasons
    res.next_required_actions = (["draft Phase 12 production-canary plan (design only)"]
                                 if res.eligible_to_draft_phase12_plan else
                                 ["resolve blocking reasons; obtain manual attestations / signoff"])
    res.summary = (f"status={res.status} recommendation={res.recommendation} "
                   f"hard_fail={hard_fail} warnings={warnings} blocked={blocked}")

    report_path = None
    if write_report:
        try:
            from .report import write_report as _wr
            report_path = _wr(store, cfg, res, ctx)
        except Exception:  # noqa: BLE001
            report_path = None
    _persist(store, res, ctx, report_path)
    audit.write_audit(store, event_type="production_review", actor="production_review",
                      review_id=res.review_id, message=res.summary,
                      payload={"recommendation": res.recommendation})
    return res


def _persist(store, res, ctx, report_path):
    if store is None:
        return
    try:
        store.add_production_review_run(res.record(report_path))
        if res.evidence_summary:
            store.add_production_evidence_summary(res.evidence_summary.record(res.review_id))
        for cat, c in res.all_checks():
            store.add_production_review_check({
                "review_id": res.review_id, "category": c.category or cat,
                "check_name": c.check_name, "status": c.status, "severity": c.severity,
                "reason": c.reason, "observed_value": c.observed_value,
                "expected_value": c.expected_value, "evidence_ref": c.evidence_ref,
                "details_json": c.details or {}})
        if res.account_readiness:
            store.add_production_account_readiness(res.account_readiness.record(res.review_id))
        for vp in res.venue_permissions:
            store.add_production_venue_permission(vp.record(res.review_id))
        if res.endpoint_separation:
            store.add_production_endpoint_separation(res.endpoint_separation.record(res.review_id))
        if res.credential_custody:
            store.add_production_credential_custody(res.credential_custody.record(res.review_id))
        if res.production_conformance:
            store.add_production_conformance_run(res.production_conformance.record(res.review_id))
        if res.operational_readiness:
            store.add_production_operational_readiness(res.operational_readiness.record(res.review_id))
    except Exception:  # noqa: BLE001
        pass


class ProductionReviewer:
    def __init__(self, store, config: Optional[ProductionReviewConfig] = None):
        self.store = store
        self.cfg = config or ProductionReviewConfig.from_env()

    def run(self, request: Optional[ProductionReviewRequest] = None, *,
            fixture: Optional[dict] = None) -> ProductionReviewResult:
        return run_review(self.store, self.cfg, request=request, fixture=fixture)
