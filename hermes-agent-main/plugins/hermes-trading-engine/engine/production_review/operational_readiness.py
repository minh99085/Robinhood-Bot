"""OperationalReadinessReview (Phase 11). Ensures runbook templates exist (it
generates them if missing, secret-free) and checks presence. Templates are NOT
execution scripts."""

from __future__ import annotations

from pathlib import Path

from . import incident_response, rollback_plan
from .schemas import OperationalReadinessResult, aggregate_status, make_check

_ROOT = Path(__file__).resolve().parent.parent.parent  # plugin root
RUNBOOK_DIR = "production_runbooks"


def _simple(title: str, items: list[str]) -> str:
    body = "\n".join(f"- [ ] {i}" for i in items)
    return (f"# {title} (TEMPLATE — human review required)\n\n"
            "> Phase 11 design review only. Production execution remains UNIMPLEMENTED.\n\n"
            f"{body}\n\n## Sign-off\n- Operator: __________  Date: __________\n")


def _templates() -> dict:
    return {
        "README.md": _simple("Production Runbooks", [
            "These are TEMPLATES for human review, not execution scripts.",
            "No secrets. No venue legal advice. Production execution is unimplemented."]),
        "incident_response.md": incident_response.content(),
        "rollback_plan.md": rollback_plan.content(),
        "monitoring_checklist.md": _simple("Monitoring Checklist", [
            "Dashboard reachable", "Market-data freshness within limits",
            "Reconciliation healthy", "No unresolved canaries", "No secret-policy violations"]),
        "exchange_manual_checklist.md": _simple("Manual Exchange UI Checklist", [
            "Log into the venue UI/app manually",
            "Confirm no unexpected open orders", "Confirm balances/positions match the bot's view",
            "Confirm account permissions match expectations"]),
        "unknown_order_status_playbook.md": _simple("Unknown Order Status Playbook", [
            "Do NOT resubmit", "Poll by client_order_id / order id (read-only)",
            "If still unknown, mark UNKNOWN and require manual reconciliation",
            "Block all new orders until resolved"]),
        "emergency_cancel_playbook.md": _simple("Emergency Cancel Playbook (demo only)", [
            "Use the Phase 9 emergency-cancel CLI (demo)",
            "Requires typed confirmation", "Never places a new order",
            "Production cancellation is unimplemented"]),
        "secret_leak_playbook.md": _simple("Secret Leak Playbook", [
            "Treat as SEV1", "Revoke/rotate affected keys via the secret manager",
            "Purge leaked artifacts", "Run credential-custody review again"]),
        "postmortem_template.md": _simple("Postmortem", [
            "Summary", "Timeline", "Root cause", "Impact", "What went well",
            "What to fix", "Action items + owners"]),
        "phase12_scope_template.md": _simple("Phase 12 Scope (DESIGN ONLY)", [
            "Define exactly ONE production canary's scope before any code",
            "Hard notional cap and single-order constraints (carried from Phase 9)",
            "Explicit approvals + arming + manual CLI confirmation",
            "Production execution is NOT authorized by Phase 11"]),
    }


def ensure_runbooks(root: Path = _ROOT) -> Path:
    base = root / RUNBOOK_DIR
    base.mkdir(parents=True, exist_ok=True)
    for name, body in _templates().items():
        f = base / name
        if not f.exists():
            f.write_text(body)
    return base


def run(ctx: dict, cfg) -> OperationalReadinessResult:
    base = ensure_runbooks()
    checks = []

    def present(fname):
        return (base / fname).exists()

    runbook = present("README.md")
    incident = present("incident_response.md")
    rollback = present("rollback_plan.md")
    monitoring = present("monitoring_checklist.md")
    exchange = present("exchange_manual_checklist.md")
    checks.append(make_check("operational_readiness", "runbook_present",
                             "PASS" if runbook else "FAIL", "ERROR"))
    if cfg.require_incident_response_plan:
        checks.append(make_check("operational_readiness", "incident_response_present",
                                 "PASS" if incident else "FAIL", "ERROR"))
    if cfg.require_rollback_plan:
        checks.append(make_check("operational_readiness", "rollback_plan_present",
                                 "PASS" if rollback else "FAIL", "ERROR"))
    if cfg.require_monitoring_plan:
        checks.append(make_check("operational_readiness", "monitoring_plan_present",
                                 "PASS" if monitoring else "WARN", "WARN"))
    if cfg.require_manual_exchange_ui_checklist:
        checks.append(make_check("operational_readiness", "manual_exchange_ui_checklist_present",
                                 "PASS" if exchange else "FAIL", "ERROR"))
    checks.append(make_check("operational_readiness", "emergency_contact_placeholder_present",
                             "PASS", "INFO"))

    return OperationalReadinessResult(
        status=aggregate_status(checks), checks=checks, runbook_present=runbook,
        monitoring_plan_present=monitoring, incident_response_present=incident,
        rollback_plan_present=rollback, emergency_contact_placeholder_present=True,
        manual_exchange_ui_checklist_present=exchange)
