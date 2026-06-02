"""ProductionReviewVetoGate (Phase 11). Maps audit results + evidence to exactly
one allowed DESIGN-REVIEW recommendation. NEVER returns production-execution /
size-increase / autonomous-live outcomes."""

from __future__ import annotations

from .schemas import FORBIDDEN_PRODUCTION_RECOMMENDATIONS


def decide(*, critical_fail: bool, shadow_insufficient: bool, demo_insufficient: bool,
           other_blocking: bool, all_clean: bool, change_control_approved: bool,
           human_checklist_passed: bool) -> str:
    if critical_fail:
        return "NOT_READY"
    if demo_insufficient:
        return "FIX_AND_REPEAT_DEMO_CANARIES"
    if shadow_insufficient:
        return "FIX_AND_REPEAT_SHADOW"
    if other_blocking or not all_clean:
        return "NOT_READY"
    if change_control_approved and human_checklist_passed:
        return "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"
    return "READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW"


def assert_safe(recommendation: str) -> str:
    if recommendation in FORBIDDEN_PRODUCTION_RECOMMENDATIONS:
        return "NOT_READY"
    return recommendation
