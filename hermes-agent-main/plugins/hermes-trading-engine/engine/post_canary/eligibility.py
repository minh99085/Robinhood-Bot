"""EligibilityTracker (Phase 10). Tracks clean/dirty/unresolved canary history
and renewed shadow evidence to decide whether another SAME-SIZE demo canary is
eligible, or a production-DESIGN review (not execution). Size increase and
autonomous live are ALWAYS False."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .schemas import CanaryEligibilitySummary

_UNRESOLVED_STATUS = {"UNKNOWN", "RECONCILE_FAILED", "PARTIALLY_FILLED", "SUBMITTING",
                      "ACKNOWLEDGED"}


def compute_eligibility(store, cfg, venue: str, environment: str, *,
                        renewed_shadow_hours: Optional[float] = None,
                        renewed_shadow_decisions: Optional[int] = None) -> CanaryEligibilitySummary:
    summary = CanaryEligibilitySummary(venue=venue, environment=environment)
    if store is None:
        summary.reason = "no_store"
        return summary

    try:
        attempts = [a for a in store.get_micro_live_attempts(1000)
                    if a.get("venue") == venue and a.get("environment") == environment
                    and int(a.get("submitted", 0))]
    except Exception:  # noqa: BLE001
        attempts = []
    attempts.sort(key=lambda a: int(a.get("ts_ms", 0)))
    try:
        analyses = store.get_post_canary_analyses(2000)
    except Exception:  # noqa: BLE001
        analyses = []
    by_attempt = {}
    for an in analyses:
        aid = an.get("live_order_attempt_id")
        if aid and aid not in by_attempt:  # analyses sorted desc by ts -> first is latest
            by_attempt[aid] = an
    try:
        cancels = [c for c in store.get_micro_live_emergency_cancels(500)
                   if c.get("venue") == venue]
    except Exception:  # noqa: BLE001
        cancels = []

    clean = failed = unresolved = 0
    last_clean_ts = None
    streak = 0
    streak_active = True
    for a in reversed(attempts):  # newest first for streak
        an = by_attempt.get(a.get("live_order_attempt_id"))
        an_status = (an or {}).get("status")
        if str(a.get("status")) in _UNRESOLVED_STATUS or an_status == "UNKNOWN_BLOCKING" or an is None:
            unresolved += 1
            streak_active = False
        elif an_status == "CLEAN":
            clean += 1
            last_clean_ts = last_clean_ts or int(a.get("ts_ms", 0))
            if streak_active:
                streak += 1
        else:
            failed += 1
            streak_active = False

    summary.total_canaries = len(attempts)
    summary.clean_canaries = clean
    summary.failed_canaries = failed
    summary.unresolved_canaries = unresolved
    summary.emergency_cancel_count = len(cancels)
    summary.clean_demo_canary_streak = streak
    summary.last_clean_canary_ts_ms = last_clean_ts
    if renewed_shadow_hours is not None:
        summary.renewed_shadow_hours_after_last_canary = Decimal(str(renewed_shadow_hours))
    summary.renewed_shadow_decisions_after_last_canary = renewed_shadow_decisions

    blockers = []
    if unresolved > 0:
        blockers.append("unresolved_canary")
    if any(int(c.get("sent", 0)) and not int(c.get("success", 0)) for c in cancels):
        blockers.append("failed_emergency_cancel")

    summary.eligible_repeat_demo_same_size = (environment == "demo" and not blockers)
    # production DESIGN review (never execution)
    prod_ok = (
        not blockers
        and clean >= cfg.min_clean_demo_canaries_for_prod_review
        and (not cfg.require_all_demo_canaries_clean_for_prod_review or failed == 0)
        and unresolved == 0
        and summary.emergency_cancel_count == 0
        and renewed_shadow_hours is not None
        and float(renewed_shadow_hours) >= cfg.min_renewed_shadow_hours_after_canary
        and renewed_shadow_decisions is not None
        and int(renewed_shadow_decisions) >= cfg.min_renewed_shadow_decisions_after_canary
    )
    summary.eligible_production_design_review = bool(prod_ok)
    summary.eligible_size_increase = False  # ALWAYS False in Phase 10
    summary.reason = ";".join(blockers) if blockers else (
        "production_design_review_eligible" if prod_ok else "repeat_demo_same_size_only")
    return summary
