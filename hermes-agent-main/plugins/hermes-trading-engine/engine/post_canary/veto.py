"""PostCanaryVetoGate (Phase 10). Maps analysis status + eligibility to exactly
one allowed recommendation. NEVER returns size-increase / autonomous-live /
production-enable outcomes. Maximum positive outcome is REPEAT_DEMO_CANARY_SAME_SIZE
(or, with enough clean history, a production DESIGN review — not execution)."""

from __future__ import annotations

from .schemas import FORBIDDEN_RECOMMENDATIONS


def decide(status: str, *, eligible_production_design_review: bool = False,
           environment: str = "demo") -> str:
    if status in ("FAIL", "UNKNOWN_BLOCKING", "ERROR"):
        return "STOP"
    if status == "WARN_REQUIRES_REVIEW":
        return "FIX_AND_REPEAT_SHADOW"
    if status == "CLEAN_BUT_NOT_ENOUGH_DATA":
        return "REPEAT_DEMO_CANARY_SAME_SIZE"
    if status == "CLEAN":
        if eligible_production_design_review:
            return "MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN"
        return "REPEAT_DEMO_CANARY_SAME_SIZE"
    return "STOP"


def assert_safe(recommendation: str) -> str:
    """Guarantee a forbidden (scaling/autonomous/production-enable) outcome can
    never escape this module."""
    if recommendation in FORBIDDEN_RECOMMENDATIONS:
        return "STOP"
    return recommendation
