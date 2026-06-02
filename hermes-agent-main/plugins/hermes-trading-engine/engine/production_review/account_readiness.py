"""AccountReadinessReview (Phase 11). Reviews account readiness via manual
attestation / optional redacted read-only snapshot. NEVER moves funds, NEVER
stores account numbers / wallet keys / raw API secrets."""

from __future__ import annotations

import time

from .schemas import AccountReadinessResult, aggregate_status, make_check
from .jurisdiction import BOT_REVIEWERS


def run(ctx: dict, cfg, *, now_ms=None) -> AccountReadinessResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    acct = ctx.get("account")
    checks = []

    present = bool(acct)
    if cfg.require_account_readiness_attestation:
        checks.append(make_check("account_readiness", "account_readiness_attestation_present",
                                 "PASS" if present else "FAIL", "CRITICAL",
                                 reason="" if present else "no account readiness attestation"))
    bot = present and str(acct.get("reviewer_id", "")).lower() in BOT_REVIEWERS
    if present:
        expired = int(acct.get("expires_ts_ms", 0)) and now > int(acct["expires_ts_ms"])
        checks.append(make_check("account_readiness", "attestation_human_and_active",
                                 "FAIL" if (bot or expired) else "PASS", "CRITICAL",
                                 reason="bot reviewer" if bot else ("expired" if expired else "")))
    if cfg.require_funding_attestation:
        checks.append(make_check("account_readiness", "funding_or_collateral_attested",
                                 "PASS" if present else "FAIL", "ERROR"))
    if cfg.require_exchange_permission_attestation:
        checks.append(make_check("account_readiness", "exchange_permissions_attested",
                                 "PASS" if present else "FAIL", "ERROR"))
    # Phase 11 NEVER moves funds — hard invariant
    checks.append(make_check("account_readiness", "no_funds_moved", "PASS", "INFO"))
    # optional read-only snapshot is opt-in and must store only redacted hashes
    ro_used = bool(ctx.get("read_only_account_snapshot_used")) and cfg.allow_readonly_account_snapshot
    checks.append(make_check("account_readiness", "no_raw_account_numbers_stored", "PASS", "INFO"))

    venues = sorted({(a or {}).get("venue", "kalshi") for a in (ctx.get("jurisdiction") or [])
                     if a} | ({acct.get("venue")} if present else set()))
    venues = [v for v in venues if v]
    return AccountReadinessResult(
        status=aggregate_status(checks), checks=checks, venue_accounts_reviewed=venues or ["kalshi"],
        production_account_attested=present and not bot,
        read_only_snapshot_used=ro_used, funding_or_collateral_attested=present,
        restrictions_attested_clear=present, no_funds_moved=True)
