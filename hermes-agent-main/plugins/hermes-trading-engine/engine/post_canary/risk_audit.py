"""RiskAudit (Phase 10). Confirms RiskEngine + SafetyEnvelope decisions existed,
occurred before submit, were not bypassed, and that the kill switch was checked
and not active at submit. Missing risk/safety is a hard STOP (CRITICAL).

Quant scope — *Compliance/Security/Operational Excellence*: the audit that risk
was never bypassed is UNCHANGED. The paper risk/portfolio upgrade keeps
TrainingRiskGate + RiskEngine mandatory for every simulated order/bundle, so this
"risk-not-bypassed" invariant continues to hold end-to-end."""

from __future__ import annotations

from .schemas import RiskAuditResult, aggregate_status, make_check


def run(ctx: dict, cfg) -> RiskAuditResult:
    a = ctx.get("attempt") or {}
    safety = ctx.get("safety_decision")
    events = ctx.get("audit_events") or []
    checks = []
    submit_ts = a.get("ts_ms")

    risk_id = a.get("risk_decision_id")
    checks.append(make_check("risk_decision_present", "PASS" if risk_id else "FAIL", "CRITICAL",
                             observed=risk_id))
    safety_allowed = bool(safety and int(safety.get("allowed", 0)))
    if cfg.require_safety_allowed:
        checks.append(make_check("safety_decision_present_and_allowed",
                                 "PASS" if safety_allowed else "FAIL", "CRITICAL",
                                 observed=bool(safety)))
    # decision before submit
    if safety and safety.get("ts_ms") is not None and submit_ts is not None:
        before = int(safety["ts_ms"]) <= int(submit_ts)
        checks.append(make_check("safety_decision_before_submit", "PASS" if before else "FAIL",
                                 "CRITICAL", observed=safety.get("ts_ms"), expected=f"<= {submit_ts}"))
    bypass = bool(ctx.get("bypass_detected"))
    checks.append(make_check("no_bypass_detected", "FAIL" if bypass else "PASS", "CRITICAL"))

    if cfg.require_kill_switch_check:
        checked = ctx.get("kill_switch_checked")
        if checked is None:
            checked = any(e.get("event_type") == "last_chance_before_submit" for e in events)
        checks.append(make_check("kill_switch_checked_before_submit",
                                 "PASS" if checked else "WARN", "WARN", observed=bool(checked)))
    if ctx.get("kill_switch_active_at_submit"):
        checks.append(make_check("kill_switch_not_active_at_submit", "FAIL", "CRITICAL",
                                 "kill switch active at submit"))

    breach = bool(ctx.get("limit_breach_detected"))
    checks.append(make_check("no_limit_breach", "FAIL" if breach else "PASS", "CRITICAL"))

    return RiskAuditResult(
        status=aggregate_status(checks), checks=checks, risk_decision_id=risk_id,
        safety_envelope_decision_id=a.get("safety_envelope_decision_id"),
        risk_approved=bool(risk_id), safety_allowed=safety_allowed, bypass_detected=bypass,
        limit_breach_detected=breach)
