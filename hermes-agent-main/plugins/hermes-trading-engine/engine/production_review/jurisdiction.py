"""JurisdictionEligibilityReview (Phase 11). Manual human attestations only. The
bot does NOT provide legal/tax advice and does NOT infer eligibility. Missing or
bot-authored attestation is FAIL."""

from __future__ import annotations

import time

from .schemas import (JurisdictionEligibilityAttestation, aggregate_status, make_check)

BOT_REVIEWERS = {"grok", "bot", "research", "research_engine", "auto", "system", "ai"}

JURISDICTION_PHRASE = ("I have independently reviewed venue eligibility, jurisdiction, and "
                       "account authorization outside this bot. This is not legal advice.")
ACCOUNT_PHRASE = ("I have independently verified account readiness, permissions, and "
                  "funding/collateral outside this bot. No funds were moved by this bot.")
VENUE_TERMS_PHRASE = ("I have independently reviewed the venue terms and prohibited "
                      "market/category restrictions outside this bot.")


def _redact_account(identifier: str) -> str:
    if not identifier:
        return ""
    s = str(identifier)
    return ("*" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else "****"


def create_attestation(*, kind: str, reviewer_id: str, venue: str, confirmation_text: str,
                       expiry_hours: float = 24.0, account_identifier: str = "",
                       now_ms=None) -> tuple[JurisdictionEligibilityAttestation, list[str]]:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    errs = []
    if str(reviewer_id).lower() in BOT_REVIEWERS:
        errs.append("reviewer_must_be_human")
    if not confirmation_text or len(confirmation_text.strip()) < 20:
        errs.append("confirmation_text_required")
    att = JurisdictionEligibilityAttestation(
        reviewer_id=reviewer_id, venue=venue, ts_ms=now,
        account_identifier_redacted=_redact_account(account_identifier),
        jurisdiction_reviewed=True, eligibility_confirmed_by_operator=True,
        venue_terms_reviewed=(kind in ("jurisdiction", "venue-terms")),
        prohibited_market_categories_reviewed=(kind in ("jurisdiction", "venue-terms")),
        tax_reporting_out_of_scope_acknowledged=True,
        legal_advice_not_provided_acknowledged=True, confirmation_text=confirmation_text,
        expires_ts_ms=now + int(expiry_hours * 3600_000),
        status="INVALID" if errs else "ACTIVE")
    return att, errs


def categorize(att: dict) -> str:
    txt = (att.get("confirmation_text") or "").lower()
    if "no funds were moved" in txt or "account readiness" in txt:
        return "account"
    if "venue terms" in txt and "jurisdiction" not in txt:
        return "venue_terms"
    return "jurisdiction"


def validate(ctx: dict, cfg, *, now_ms=None):
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    juris = ctx.get("jurisdiction") or []
    checks, valid = [], []
    if cfg.require_jurisdiction_attestation and not juris:
        checks.append(make_check("jurisdiction", "jurisdiction_attestation_present", "FAIL",
                                 "CRITICAL", "no jurisdiction attestation"))
    for a in juris:
        rid = str(a.get("reviewer_id", "")).lower()
        expired = int(a.get("expires_ts_ms", 0)) and now > int(a["expires_ts_ms"])
        bot = rid in BOT_REVIEWERS
        ok = (not bot and not expired and a.get("status") == "ACTIVE"
              and a.get("jurisdiction_reviewed") and a.get("eligibility_confirmed_by_operator")
              and a.get("legal_advice_not_provided_acknowledged"))
        checks.append(make_check("jurisdiction", f"attestation_{a.get('attestation_id')}",
                                 "PASS" if ok else "FAIL", "CRITICAL",
                                 reason=("bot reviewer" if bot else
                                         "expired" if expired else "" if ok else "incomplete"),
                                 observed=a.get("reviewer_id")))
        if ok:
            valid.append(JurisdictionEligibilityAttestation(**{
                k: a.get(k) for k in JurisdictionEligibilityAttestation.model_fields if k in a}))
    return aggregate_status(checks), checks, valid
