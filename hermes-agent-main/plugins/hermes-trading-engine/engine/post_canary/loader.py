"""PostCanaryLoader (Phase 10). Builds an analysis context from storage (or a
fixture dict). Fails closed when required records are missing. Never calls the
exchange network by default; read-only refresh is opt-in and unimplemented for
real venues here (kept disabled)."""

from __future__ import annotations

from typing import Optional


class LoaderError(Exception):
    pass


def _latest_recon_for(store, attempt_id):
    try:
        recons = store.get_micro_live_reconciliations(200)
    except Exception:  # noqa: BLE001
        return None
    matches = [r for r in recons if r.get("live_order_attempt_id") == attempt_id]
    return matches[0] if matches else None


def _emergency_for(store, client_order_id, exchange_order_id):
    try:
        rows = store.get_micro_live_emergency_cancels(200)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if (client_order_id and r.get("client_order_id") == client_order_id) or \
           (exchange_order_id and r.get("exchange_order_id") == exchange_order_id):
            out.append(r)
    return out


def build_context_from_store(store, attempt_id: str) -> dict:
    attempt = store.get_micro_live_attempt(attempt_id) if store else None
    if not attempt:
        raise LoaderError("live_order_attempt_not_found")
    plan = store.get_micro_live_canary_plan(attempt.get("canary_plan_id")) if attempt.get(
        "canary_plan_id") else None
    dry = None
    if plan and plan.get("source_dry_run_intent_id"):
        dry = store.get_dry_run_order_intent(plan["source_dry_run_intent_id"])
    safety = None
    sid = attempt.get("safety_envelope_decision_id")
    if sid:
        try:
            safety = store.get_safety_envelope_decision(sid)
        except Exception:  # noqa: BLE001
            safety = None
    readiness = None
    if plan and plan.get("readiness_report_id"):
        readiness = store.get_readiness_report(plan["readiness_report_id"])
    research = None
    if plan and plan.get("market_id"):
        try:
            ests = store.get_probability_estimates(venue=plan.get("venue"),
                                                   market_id=plan.get("market_id"), limit=1)
            research = ests[0] if ests else None
        except Exception:  # noqa: BLE001
            research = None
    audit_events = []
    try:
        audit_events = [e for e in store.get_micro_live_audit_events(500)
                        if e.get("live_order_attempt_id") == attempt_id
                        or e.get("canary_plan_id") == attempt.get("canary_plan_id")]
    except Exception:  # noqa: BLE001
        pass
    accts = []
    try:
        accts = [a for a in store.get_micro_live_account_snapshots(200)
                 if a.get("venue") == attempt.get("venue")]
    except Exception:  # noqa: BLE001
        pass
    return {
        "attempt": attempt, "plan": plan or {}, "dry_run_intent": dry or {},
        "reconciliation": _latest_recon_for(store, attempt_id),
        "emergency_cancels": _emergency_for(store, attempt.get("client_order_id"),
                                            attempt.get("exchange_order_id")),
        "account_snapshots": accts, "audit_events": audit_events,
        "safety_decision": safety, "readiness": readiness, "research": research,
        "shadow_decision": None, "market_data": {}, "network_guard_events": [],
        "secret_violations": [], "kill_switch_active_at_submit": False,
    }


def load(store, *, attempt_id: Optional[str] = None,
         fixture: Optional[dict] = None) -> dict:
    """Return an analysis context. Fixture (a ctx dict) takes precedence and
    avoids any storage/network access. Fails closed if attempt is missing."""
    if fixture is not None:
        ctx = dict(fixture)
        if not ctx.get("attempt") or not ctx["attempt"].get("live_order_attempt_id"):
            raise LoaderError("fixture_missing_live_order_attempt")
        ctx.setdefault("plan", {})
        ctx.setdefault("dry_run_intent", {})
        ctx.setdefault("emergency_cancels", [])
        ctx.setdefault("account_snapshots", [])
        ctx.setdefault("audit_events", [])
        ctx.setdefault("market_data", {})
        ctx.setdefault("network_guard_events", [])
        ctx.setdefault("secret_violations", [])
        return ctx
    if not attempt_id:
        raise LoaderError("live_order_attempt_id_required")
    return build_context_from_store(store, attempt_id)
