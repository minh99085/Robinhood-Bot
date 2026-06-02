"""ExecutionQualityAnalyzer (Phase 10). Compares intended/dry-run/submitted/fill
fields and flags drift, wrong-target, over-notional, partial/resting anomalies.

Quant scope — *Execution Engine CLOB v2 simulation* + *Live Trading & Monitoring*:
the live-canary execution-quality analyzer (UNCHANGED). The PAPER/replay forward
execution-quality estimates (queue position, fill probability, slippage forecast,
spread blowout, partial-fill risk, markout, bundle quality) live in
``engine.training.execution_quality`` and never touch this live path."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .schemas import ExecutionQualityResult, aggregate_status, make_check
from .slippage import slippage_bps


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def run(ctx: dict, cfg) -> ExecutionQualityResult:
    a = ctx.get("attempt") or {}
    plan = ctx.get("plan") or {}
    dry = ctx.get("dry_run_intent") or {}
    recon = ctx.get("reconciliation") or {}
    checks = []

    def eq(name, expected, actual, sev="ERROR", flag=None):
        ok = (expected is None or actual is None or str(expected) == str(actual))
        checks.append(make_check(name, "PASS" if ok else "FAIL", sev,
                                 reason="" if ok else f"expected {expected} got {actual}",
                                 observed=actual, expected=expected))
        return ok

    # payload hash drift (vs approved dry-run intent / expected hash)
    expected_hash = plan.get("expected_payload_hash")
    submitted_hash = a.get("request_payload_hash")
    drift = bool(expected_hash and submitted_hash and expected_hash != submitted_hash)
    checks.append(make_check("payload_drift", "FAIL" if drift else "PASS", "CRITICAL",
                             reason="submitted payload hash != approved" if drift else "",
                             observed=submitted_hash, expected=expected_hash,
                             threshold=cfg.max_payload_drift_fields))

    # target correctness
    eq("venue_matches", plan.get("venue") or dry.get("venue"), a.get("venue"), "CRITICAL")
    eq("market_matches", dry.get("market_ticker") or plan.get("market_ticker"),
       plan.get("market_ticker"), "CRITICAL")
    eq("side_matches", dry.get("side") or plan.get("side"), plan.get("side"), "CRITICAL")
    eq("outcome_matches", dry.get("outcome") or plan.get("outcome"), plan.get("outcome"),
       "CRITICAL")
    # order type / TIF must be FOK / fill_or_kill
    checks.append(make_check("order_type_fok",
                             "PASS" if str(plan.get("order_type", "FOK")).upper() == "FOK" else
                             "FAIL", "CRITICAL", observed=plan.get("order_type")))
    checks.append(make_check("tif_fok",
                             "PASS" if str(plan.get("time_in_force", "fill_or_kill")).lower()
                             == "fill_or_kill" else "FAIL", "CRITICAL",
                             observed=plan.get("time_in_force")))
    # environment must be demo (Phase 10 never accepts prod execution)
    expected_env = ctx.get("expected_environment", plan.get("environment", "demo"))
    actual_env = a.get("environment", plan.get("environment"))
    env_ok = str(actual_env) == str(expected_env) and str(actual_env) == "demo" \
        if not ctx.get("production_allowed_expected") else str(actual_env) == str(expected_env)
    checks.append(make_check("environment_demo",
                             "PASS" if str(actual_env) == "demo" else "FAIL", "CRITICAL",
                             reason="" if str(actual_env) == "demo" else "non-demo execution",
                             observed=actual_env, expected="demo"))

    # notional vs approved cap
    intended_notional = _d(plan.get("notional"))
    filled_notional = _d(a.get("notional_filled")) or _d(a.get("notional_submitted"))
    cap = _d(ctx.get("max_notional")) or intended_notional
    over = bool(filled_notional is not None and cap is not None and filled_notional > cap)
    checks.append(make_check("notional_within_cap", "FAIL" if over else "PASS", "CRITICAL",
                             observed=filled_notional, threshold=cap))

    # partial / resting anomalies
    status = str(a.get("status"))
    partial = status == "PARTIALLY_FILLED" or bool(ctx.get("unexpected_partial_fill"))
    if partial:
        checks.append(make_check("no_unexpected_partial_fill",
                                 "PASS" if cfg.allow_partial_fill else "FAIL", "ERROR",
                                 reason="partial fill" if not cfg.allow_partial_fill else ""))
    resting = str(recon.get("local_order_status", "")) in ("OPEN", "RESTING") or \
        bool(ctx.get("unexpected_resting_order"))
    if resting:
        checks.append(make_check("no_unexpected_resting_order",
                                 "PASS" if cfg.allow_unexpected_resting_order else "FAIL",
                                 "CRITICAL", reason="FOK rested/open"))

    # slippage
    intended_price = _d(plan.get("limit_price"))
    fill_price = _d(a.get("avg_fill_price"))
    slip = slippage_bps(intended_price, fill_price, plan.get("side", "BUY"))
    if slip is not None:
        checks.append(make_check("slippage_within_threshold",
                                 "PASS" if slip <= Decimal(str(cfg.max_slippage_bps)) else "WARN",
                                 "WARN", observed=f"{slip:.1f}bps", threshold=cfg.max_slippage_bps))

    return ExecutionQualityResult(
        status=aggregate_status(checks), checks=checks, intended_price=intended_price,
        submitted_price=intended_price, avg_fill_price=fill_price,
        intended_quantity=_d(plan.get("quantity")), filled_quantity=_d(a.get("filled_quantity")),
        intended_notional=intended_notional, filled_notional=filled_notional, slippage_bps=slip,
        payload_drift_detected=drift, unexpected_partial_fill=partial,
        unexpected_resting_order=resting)
