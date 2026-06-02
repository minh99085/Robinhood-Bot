"""ResearchAudit (Phase 10). Validates the research estimate behind the canary
and confirms Grok did not size/execute/approve. High ambiguity / low evidence
route to FIX_AND_REPEAT_SHADOW (ERROR severity, not CRITICAL STOP)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .schemas import ResearchAuditResult, aggregate_status, make_check

_GROK_FORBIDDEN_ACTORS = ("grok", "research")


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def run(ctx: dict, cfg) -> ResearchAuditResult:
    est = ctx.get("research")
    events = ctx.get("audit_events") or []
    checks = []

    if not est:
        checks.append(make_check("research_estimate_present", "WARN", "WARN",
                                 "no research estimate linked to canary"))
        grok_acted = any(str(e.get("actor", "")).lower() in _GROK_FORBIDDEN_ACTORS for e in events)
        checks.append(make_check("grok_did_not_execute", "FAIL" if grok_acted else "PASS",
                                 "CRITICAL", reason="grok/research actor triggered a live action"
                                 if grok_acted else ""))
        return ResearchAuditResult(status=aggregate_status(checks), checks=checks)

    ev = _d(est.get("evidence_score"))
    sc = est.get("source_count")
    amb = _d(est.get("ambiguity_score"))
    stale = bool(ctx.get("research_stale") or est.get("stale"))
    checks.append(make_check("research_not_stale", "FAIL" if stale else "PASS", "ERROR",
                             observed=stale))
    if ev is not None:
        checks.append(make_check("evidence_above_threshold",
                                 "PASS" if ev >= Decimal(str(cfg.min_evidence_score)) else "FAIL",
                                 "ERROR", observed=ev, threshold=cfg.min_evidence_score))
    if sc is not None:
        checks.append(make_check("source_count_sufficient",
                                 "PASS" if int(sc) >= cfg.min_source_count else "FAIL", "ERROR",
                                 observed=sc, threshold=cfg.min_source_count))
    if amb is not None:
        checks.append(make_check("ambiguity_within_limit",
                                 "PASS" if amb <= Decimal(str(cfg.max_ambiguity_score)) else "FAIL",
                                 "ERROR", observed=amb, threshold=cfg.max_ambiguity_score))
    grok_acted = any(str(e.get("actor", "")).lower() in _GROK_FORBIDDEN_ACTORS for e in events)
    checks.append(make_check("grok_did_not_execute", "FAIL" if grok_acted else "PASS", "CRITICAL"))

    return ResearchAuditResult(
        status=aggregate_status(checks), checks=checks, estimate_id=est.get("estimate_id"),
        p_ensemble=_d(est.get("p_ensemble")), confidence=_d(est.get("confidence")),
        evidence_score=ev, source_count=(int(sc) if sc is not None else None),
        ambiguity_score=amb, stale=stale, no_trade_reason=est.get("no_trade_reason"))
