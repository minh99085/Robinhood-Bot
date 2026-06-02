"""PostCanaryAnalyzer (Phase 10). Coordinates all sub-audits, classifies the
canary, applies the veto gate, and persists results + artifacts. NEVER submits
or cancels orders."""

from __future__ import annotations

import time
from typing import Optional

from . import (chain_audit, eligibility, execution_quality, market_data_audit, markout,
               reconciliation_audit, research_audit, risk_audit, secret_audit, veto)
from .config import PostCanaryConfig
from .loader import LoaderError, load
from .schemas import (PostCanaryAnalysisRequest, PostCanaryAnalysisResult)

_CRITICAL = "CRITICAL"


def analyze_context(cfg: PostCanaryConfig, ctx: dict, *,
                    eligible_production_design_review: bool = False) -> PostCanaryAnalysisResult:
    a = ctx.get("attempt") or {}
    plan = ctx.get("plan") or {}
    res = PostCanaryAnalysisResult(
        live_order_attempt_id=a.get("live_order_attempt_id", ""),
        canary_plan_id=a.get("canary_plan_id") or plan.get("canary_plan_id"))

    res.reconciliation = reconciliation_audit.run(ctx, cfg)
    res.execution_quality = execution_quality.run(ctx, cfg)
    res.market_data = market_data_audit.run(ctx, cfg)
    res.research = research_audit.run(ctx, cfg)
    res.risk = risk_audit.run(ctx, cfg)
    res.chain = chain_audit.run(ctx, cfg)
    res.secrets = secret_audit.run(ctx, cfg)
    res.markout = markout.run(ctx, cfg)

    critical_fail = unknown_blocking = warnings = hard_fail = 0
    data_missing = False
    blocking_reasons, actions = [], []
    for cat, c in res.all_checks():
        if c.status == "FAIL":
            hard_fail += 1
            if c.severity == _CRITICAL:
                critical_fail += 1
                blocking_reasons.append(f"{cat}.{c.check_name}: {c.reason or 'FAIL'}")
            else:
                warnings += 1
                actions.append(f"fix {cat}.{c.check_name}")
        elif c.status == "UNKNOWN":
            if c.severity == _CRITICAL:
                unknown_blocking += 1
                blocking_reasons.append(f"{cat}.{c.check_name}: UNKNOWN")
            else:
                warnings += 1
        elif c.status == "WARN":
            warnings += 1
            actions.append(f"review {cat}.{c.check_name}")
        elif c.status == "NOT_APPLICABLE":
            data_missing = True

    # markout (separate, not in all_checks)
    if res.markout.status == "UNKNOWN":
        unknown_blocking += 1
        blocking_reasons.append("markout: no market data at any horizon")
    elif res.markout.status == "WARN":
        warnings += 1
        actions.append("review adverse markout")
    if any(o.data_missing for o in res.markout.observations):
        data_missing = True
    if not ctx.get("research"):
        data_missing = True

    res.hard_fail_count = hard_fail
    res.warning_count = warnings
    res.unknown_blocking_count = unknown_blocking

    if critical_fail > 0:
        res.status = "FAIL"
    elif unknown_blocking > 0:
        res.status = "UNKNOWN_BLOCKING"
    elif warnings > 0:
        res.status = "WARN_REQUIRES_REVIEW"
    elif data_missing:
        res.status = "CLEAN_BUT_NOT_ENOUGH_DATA"
    else:
        res.status = "CLEAN"

    env = plan.get("environment", "demo")
    rec = veto.decide(res.status, eligible_production_design_review=eligible_production_design_review,
                      environment=env)
    res.recommendation = veto.assert_safe(rec)

    res.clean_for_repeat_demo_same_size = (res.status == "CLEAN" and env == "demo")
    res.eligible_for_production_design_review = bool(eligible_production_design_review
                                                     and res.status == "CLEAN")
    res.eligible_for_size_increase = False
    res.eligible_for_autonomous_live = False
    res.blocking_reasons = blocking_reasons
    res.next_required_actions = actions or (["manual review"] if res.status == "CLEAN" else [])
    res.summary = (f"status={res.status} recommendation={res.recommendation} "
                   f"hard_fail={hard_fail} warnings={warnings} unknown={unknown_blocking}")
    return res


class PostCanaryAnalyzer:
    def __init__(self, store, config: Optional[PostCanaryConfig] = None):
        self.store = store
        self.cfg = config or PostCanaryConfig.from_env()

    def analyze(self, request: PostCanaryAnalysisRequest, *, fixture: Optional[dict] = None,
                write_report: bool = True) -> PostCanaryAnalysisResult:
        try:
            ctx = load(self.store, attempt_id=request.live_order_attempt_id or None,
                       fixture=fixture)
        except LoaderError as e:
            res = PostCanaryAnalysisResult(live_order_attempt_id=request.live_order_attempt_id,
                                           status="UNKNOWN_BLOCKING", recommendation="STOP",
                                           unknown_blocking_count=1, blocking_reasons=[str(e)],
                                           summary=f"loader failed: {e}")
            self._emit(res)
            return res

        venue = (ctx.get("plan") or {}).get("venue", "kalshi")
        env = (ctx.get("plan") or {}).get("environment", "demo")
        elig = eligibility.compute_eligibility(
            self.store, self.cfg, venue, env,
            renewed_shadow_hours=ctx.get("renewed_shadow_hours"),
            renewed_shadow_decisions=ctx.get("renewed_shadow_decisions"))
        res = analyze_context(self.cfg, ctx,
                              eligible_production_design_review=elig.eligible_production_design_review)

        report_path = None
        if write_report:
            try:
                from .report import write_report as _wr
                report_path = _wr(self.store, self.cfg, res, ctx, elig)
            except Exception:  # noqa: BLE001
                report_path = None
        self._persist(res, elig, report_path)
        self._emit(res)
        return res

    def _persist(self, res, elig, report_path):
        if self.store is None:
            return
        try:
            self.store.add_post_canary_analysis(res.record(report_path))
            for cat in ("reconciliation", "execution_quality", "market_data", "research", "risk",
                        "chain", "secrets"):
                sub = getattr(res, cat, None)
                if sub and getattr(sub, "checks", None):
                    for c in sub.checks:
                        self.store.add_post_canary_audit_check({
                            "analysis_id": res.analysis_id, "category": cat,
                            "check_name": c.check_name, "status": c.status, "severity": c.severity,
                            "reason": c.reason, "observed_value": c.observed_value,
                            "expected_value": c.expected_value, "threshold": c.threshold,
                            "details_json": c.details or {}})
            if res.reconciliation:
                self.store.add_post_canary_reconciliation_audit(res.reconciliation.record(res.analysis_id))
            if res.execution_quality:
                self.store.add_post_canary_execution_quality(res.execution_quality.record(res.analysis_id))
            if res.market_data:
                self.store.add_post_canary_market_data_audit(res.market_data.record(res.analysis_id))
            if res.research:
                self.store.add_post_canary_research_audit(res.research.record(res.analysis_id))
            if res.risk:
                self.store.add_post_canary_risk_audit(res.risk.record(res.analysis_id))
            if res.chain:
                self.store.add_post_canary_chain_audit(res.chain.record(res.analysis_id))
            if res.secrets:
                self.store.add_post_canary_secret_audit(res.secrets.record(res.analysis_id))
            if res.markout:
                for o in res.markout.observations:
                    self.store.add_post_canary_markout(o.record(res.analysis_id))
            self.store.add_post_canary_eligibility(elig.record())
        except Exception:  # noqa: BLE001 — storage best-effort after analysis
            pass

    def _emit(self, res):
        if self.store is None:
            return
        try:
            self.store.add_post_canary_audit_event({
                "ts_ms": int(time.time() * 1000), "analysis_id": res.analysis_id,
                "event_type": "post_canary_analysis", "severity": "INFO",
                "actor": "post_canary", "message": res.summary,
                "payload_json": {"recommendation": res.recommendation, "status": res.status}})
        except Exception:  # noqa: BLE001
            pass
