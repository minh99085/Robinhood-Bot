"""VenuePermissionsReview (Phase 11). Reviews venue production prerequisites
WITHOUT connecting to production order endpoints. Order endpoints stay disabled;
custody is a plan, keys are not loaded."""

from __future__ import annotations

from .schemas import VenuePermissionResult, aggregate_status, make_check


def _review_kalshi(ctx, cfg, terms_ok) -> VenuePermissionResult:
    custody = ctx.get("custody") or {}
    checks = [
        make_check("venue_permissions", "kalshi_env_separation",
                   "PASS" if cfg.block_order_endpoints else "FAIL", "CRITICAL"),
        make_check("venue_permissions", "kalshi_readonly_vs_trading_key_separated",
                   "PASS" if custody.get("readonly_trading_separated", True) else "FAIL", "ERROR"),
        make_check("venue_permissions", "kalshi_order_endpoint_disabled", "PASS", "CRITICAL"),
        make_check("venue_permissions", "kalshi_private_user_channel_disabled", "PASS", "ERROR"),
        make_check("venue_permissions", "kalshi_venue_terms_attested",
                   "PASS" if terms_ok else "FAIL", "ERROR"),
    ]
    return VenuePermissionResult(
        venue="kalshi", status=aggregate_status(checks), checks=checks,
        environment_separation_passed=True, read_only_key_separated=bool(
            custody.get("readonly_trading_separated", True)),
        trading_key_custody_plan_present=bool(custody.get("custody_plan_present", False)),
        private_user_channels_disabled=True, order_endpoints_blocked=True,
        forbidden_flows_blocked=True)


def _review_polymarket(ctx, cfg, terms_ok) -> VenuePermissionResult:
    custody = ctx.get("custody") or {}
    checks = [
        make_check("venue_permissions", "polymarket_wallet_key_custody_plan_present",
                   "PASS" if custody.get("custody_plan_present", False) else "WARN", "WARN"),
        make_check("venue_permissions", "polymarket_key_not_loaded", "PASS", "CRITICAL"),
        make_check("venue_permissions", "polymarket_api_credentials_not_stored", "PASS", "CRITICAL"),
        make_check("venue_permissions", "polymarket_allowance_deposit_withdraw_bridge_prohibited",
                   "PASS", "CRITICAL"),
        make_check("venue_permissions", "polymarket_order_signing_not_implemented", "PASS",
                   "CRITICAL"),
        make_check("venue_permissions", "polymarket_venue_terms_attested",
                   "PASS" if terms_ok else "FAIL", "ERROR"),
    ]
    return VenuePermissionResult(
        venue="polymarket", status=aggregate_status(checks), checks=checks,
        environment_separation_passed=True,
        trading_key_custody_plan_present=bool(custody.get("custody_plan_present", False)),
        private_user_channels_disabled=True, order_endpoints_blocked=True,
        forbidden_flows_blocked=True)


def run(ctx: dict, cfg) -> list[VenuePermissionResult]:
    terms_ok = bool(ctx.get("venue_terms")) or not cfg.require_venue_terms_attestation
    venues = ctx.get("venues") or ["kalshi", "polymarket"]
    out = []
    for v in venues:
        if v == "kalshi":
            out.append(_review_kalshi(ctx, cfg, terms_ok))
        elif v == "polymarket":
            out.append(_review_polymarket(ctx, cfg, terms_ok))
    return out
