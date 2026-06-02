"""HumanChecklist (Phase 11). Final human signoff. Cannot be authored by a bot.
Signoff only permits drafting a Phase 12 plan; it never enables execution."""

from __future__ import annotations

import time
from typing import Optional

from .jurisdiction import BOT_REVIEWERS
from .schemas import HumanChecklistResult, make_check

CONFIRMATION_TEXT = ("I have manually reviewed the production-canary design dossier. I understand "
                     "Phase 11 does not authorize production execution, cancellation, signing, "
                     "size increase, or autonomous live trading.")

_REQUIRED_ITEMS = [
    "evidence_complete", "demo_canaries_clean", "renewed_shadow_clean",
    "account_eligibility_manually_reviewed", "venue_terms_manually_reviewed",
    "secret_custody_reviewed", "incident_response_reviewed", "endpoint_separation_passed",
    "no_production_execution_in_phase11", "phase12_scope_explicitly_defined_before_code",
]


def build_checklist(*, reviewer_id: str, review_id: str, item_results: Optional[dict] = None,
                    confirmation_text: str = "", now_ms=None) -> HumanChecklistResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    item_results = item_results or {}
    bot = str(reviewer_id).lower() in BOT_REVIEWERS
    items = []
    for name in _REQUIRED_ITEMS:
        passed = bool(item_results.get(name, False))
        items.append(make_check("human_checklist", name, "PASS" if passed else "FAIL",
                                 "ERROR", observed=passed))
    confirmation_ok = (confirmation_text or "").strip() == CONFIRMATION_TEXT or \
        len((confirmation_text or "").strip()) >= 40
    all_passed = (not bot) and confirmation_ok and all(c.status == "PASS" for c in items)
    items.append(make_check("human_checklist", "reviewer_is_human",
                            "FAIL" if bot else "PASS", "CRITICAL"))
    items.append(make_check("human_checklist", "confirmation_text_present",
                            "PASS" if confirmation_ok else "FAIL", "ERROR"))
    status = "PASS_DESIGN_ONLY" if all_passed else ("FAIL" if bot else "INCOMPLETE")
    return HumanChecklistResult(
        reviewer_id=reviewer_id, review_id=review_id, ts_ms=now, checklist_items=items,
        all_required_items_passed=all_passed, confirmation_text=confirmation_text, status=status)


def validate(ctx: dict, cfg):
    hc = ctx.get("human_checklist")
    checks = []
    if not hc:
        checks.append(make_check("human_checklist", "human_checklist_present", "FAIL", "CRITICAL",
                                 "no human checklist"))
        return "FAIL", checks, None
    bot = str(hc.get("reviewer_id", "")).lower() in BOT_REVIEWERS
    passed = bool(hc.get("all_required_items_passed")) and not bot
    checks.append(make_check("human_checklist", "human_checklist_signed",
                             "PASS" if passed else "FAIL", "CRITICAL",
                             reason="bot reviewer" if bot else ("" if passed else "incomplete")))
    rec = HumanChecklistResult(**{k: hc.get(k) for k in HumanChecklistResult.model_fields
                                  if k in hc and k != "checklist_items"})
    return ("PASS" if passed else "FAIL"), checks, rec
