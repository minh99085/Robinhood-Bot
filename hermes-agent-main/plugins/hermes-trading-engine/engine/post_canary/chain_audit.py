"""ChainAudit (Phase 10). Validates full traceability from shadow evidence to
report, plus audit-chain-hash continuity. Missing required links are blocking."""

from __future__ import annotations

from .schemas import ChainAuditResult, aggregate_status, make_check

# (link_name, severity) — shadow link is WARN (manual canaries may lack it).
_REQUIRED = [
    ("readiness_report_id", "CRITICAL"),
    ("approval_batch_id", "CRITICAL"),
    ("arming_token_id", "CRITICAL"),
    ("dry_run_intent_id", "CRITICAL"),
    ("canary_plan_id", "CRITICAL"),
    ("risk_decision_id", "CRITICAL"),
    ("safety_envelope_decision_id", "CRITICAL"),
    ("live_order_attempt_id", "CRITICAL"),
    ("reconciliation_id", "CRITICAL"),
]


def run(ctx: dict, cfg) -> ChainAuditResult:
    a = ctx.get("attempt") or {}
    plan = ctx.get("plan") or {}
    recon = ctx.get("reconciliation") or {}
    trace = {
        "shadow_session_id": plan.get("source_shadow_session_id"),
        "shadow_decision_id": plan.get("source_shadow_decision_id"),
        "readiness_report_id": plan.get("readiness_report_id"),
        "approval_batch_id": plan.get("approval_batch_id"),
        "arming_token_id": plan.get("arming_token_id"),
        "dry_run_intent_id": plan.get("source_dry_run_intent_id"),
        "canary_plan_id": a.get("canary_plan_id") or plan.get("canary_plan_id"),
        "preflight_id": ctx.get("preflight_id"),
        "risk_decision_id": a.get("risk_decision_id"),
        "safety_envelope_decision_id": a.get("safety_envelope_decision_id"),
        "live_order_attempt_id": a.get("live_order_attempt_id"),
        "reconciliation_id": (recon or {}).get("reconciliation_id"),
        "report_id": ctx.get("report_id"),
    }
    checks, missing = [], []
    for name, sev in _REQUIRED:
        present = bool(trace.get(name))
        checks.append(make_check(f"trace_{name}", "PASS" if present else "FAIL", sev,
                                 observed=trace.get(name)))
        if not present:
            missing.append(name)
    # shadow link: WARN (allowed for manual_canary)
    shadow_ok = bool(trace.get("shadow_session_id") or trace.get("shadow_decision_id")
                     or ctx.get("manual_canary_plan_id"))
    checks.append(make_check("trace_shadow_or_manual_link", "PASS" if shadow_ok else "WARN",
                             "WARN", observed=shadow_ok))
    if not shadow_ok:
        missing.append("shadow_session_id")

    chain_valid = ctx.get("audit_chain_valid")
    if chain_valid is None:
        # best-effort: events present and each has a hash
        events = ctx.get("audit_events") or []
        chain_valid = all(e.get("audit_chain_hash") for e in events) if events else None
    if cfg.require_audit_chain and chain_valid is False:
        checks.append(make_check("audit_chain_hash_valid", "FAIL", "CRITICAL",
                                 "audit chain hash invalid/broken"))
    elif chain_valid is True:
        checks.append(make_check("audit_chain_hash_valid", "PASS", "INFO"))
    else:
        checks.append(make_check("audit_chain_hash_valid", "WARN", "WARN",
                                 "audit chain not verifiable"))

    return ChainAuditResult(status=aggregate_status(checks), checks=checks, missing_links=missing,
                            audit_chain_hash_valid=(None if chain_valid is None else bool(chain_valid)),
                            trace=trace)
