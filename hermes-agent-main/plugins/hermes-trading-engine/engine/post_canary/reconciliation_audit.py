"""ReconciliationAudit (Phase 10). Validates the canary reconciled cleanly
against exchange/account/order/fill data. UNKNOWN is always blocking."""

from __future__ import annotations

from decimal import Decimal

from .schemas import ReconciliationAuditResult, aggregate_status, make_check

_TERMINAL = {"FILLED", "REJECTED", "CANCELLED", "PARTIALLY_FILLED"}


def _d(v):
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def run(ctx: dict, cfg) -> ReconciliationAuditResult:
    a = ctx.get("attempt") or {}
    recon = ctx.get("reconciliation")
    cancels = ctx.get("emergency_cancels") or []
    accts = ctx.get("account_snapshots") or []
    events = ctx.get("audit_events") or []
    checks = []

    status = str(a.get("status"))
    checks.append(make_check("order_status_terminal_or_known",
                             "PASS" if status in _TERMINAL else
                             ("FAIL" if status in ("UNKNOWN", "RECONCILE_FAILED", "FAILED")
                              else "WARN"),
                             "CRITICAL" if status in ("UNKNOWN", "RECONCILE_FAILED") else "WARN",
                             reason=f"status={status}", observed=status))
    if status == "UNKNOWN" and cfg.unknown_status_blocks:
        checks.append(make_check("unknown_status_blocking", "UNKNOWN", "CRITICAL",
                                 "exchange status UNKNOWN blocks", observed=status))

    checks.append(make_check("pre_submit_account_snapshot",
                             "PASS" if accts else "WARN", "WARN",
                             reason="account snapshot present" if accts else "no account snapshot",
                             observed=len(accts)))
    checks.append(make_check("exchange_ack_captured",
                             "PASS" if int(a.get("acknowledged", 0)) else "WARN", "WARN",
                             observed=bool(int(a.get("acknowledged", 0)))))

    if recon is None:
        checks.append(make_check("reconciliation_present", "FAIL", "CRITICAL",
                                 "no reconciliation record"))
    else:
        ex_q = _d(recon.get("filled_quantity"))
        loc_q = _d(recon.get("local_filled_quantity"))
        a_q = _d(a.get("filled_quantity"))
        checks.append(make_check("filled_matches_exchange",
                                 "PASS" if (ex_q is not None and a_q is not None and ex_q == a_q)
                                 else "FAIL", "CRITICAL", observed=a_q, expected=ex_q))
        checks.append(make_check("local_matches_exchange",
                                 "PASS" if (ex_q is not None and loc_q is not None and ex_q == loc_q)
                                 else "FAIL", "CRITICAL", observed=loc_q, expected=ex_q))
        # fee tolerance
        ex_fee, loc_fee = _d(recon.get("fee")), _d(a.get("fee"))
        if ex_fee is not None and loc_fee is not None and ex_fee > 0:
            dev_bps = abs(ex_fee - loc_fee) / ex_fee * Decimal(10000)
            checks.append(make_check("fee_matches", "PASS" if dev_bps <=
                                     Decimal(str(cfg.max_fee_deviation_bps)) else "FAIL",
                                     "ERROR", observed=f"{dev_bps:.1f}bps",
                                     threshold=cfg.max_fee_deviation_bps))
        local_os = str(recon.get("local_order_status", ""))
        checks.append(make_check("no_unexplained_open_order",
                                 "FAIL" if local_os in ("OPEN", "RESTING", "ACKNOWLEDGED") else
                                 "PASS", "CRITICAL", observed=local_os))
        if str(recon.get("status")) in ("FAIL", "WARN"):
            checks.append(make_check("reconciliation_result_clean",
                                     "FAIL" if recon.get("status") == "FAIL" else "WARN",
                                     "ERROR", observed=recon.get("status")))

    # duplicate / idempotency
    checks.append(make_check("no_duplicate_client_order_id",
                             "FAIL" if ctx.get("duplicate_client_order_id") else "PASS",
                             "CRITICAL"))
    checks.append(make_check("no_duplicate_exchange_order_id",
                             "FAIL" if ctx.get("duplicate_exchange_order_id") else "PASS",
                             "CRITICAL"))
    idem = ctx.get("idempotency_key_before_submit")
    if idem is None:
        idem = any(e.get("event_type") == "idempotency_key_persisted" for e in events)
    checks.append(make_check("idempotency_key_before_submit", "PASS" if idem else "FAIL",
                             "CRITICAL", observed=bool(idem)))
    ncc = int(a.get("network_call_count", 0) or 0)
    checks.append(make_check("no_blind_retry", "PASS" if ncc <= 1 else "FAIL", "CRITICAL",
                             observed=ncc, threshold=1))

    # emergency cancel
    if cancels:
        any_fail = any(int(c.get("sent", 0)) and not int(c.get("success", 0)) for c in cancels)
        if any_fail:
            checks.append(make_check("emergency_cancel_resolved", "FAIL", "CRITICAL",
                                     "emergency cancel failed"))
        elif not cfg.allow_emergency_cancel_for_clean and not ctx.get("emergency_cancel_reviewed"):
            checks.append(make_check("no_emergency_cancel", "WARN", "ERROR",
                                     "emergency cancel occurred -> blocks CLEAN unless reviewed",
                                     observed=len(cancels)))
    res = ReconciliationAuditResult(
        status=aggregate_status(checks), checks=checks,
        exchange_status=(recon or {}).get("exchange_order_status"),
        local_status=(recon or {}).get("local_order_status"),
        filled_quantity=_d((recon or {}).get("filled_quantity")),
        local_filled_quantity=_d((recon or {}).get("local_filled_quantity")),
        fee=_d((recon or {}).get("fee")), local_fee=_d(a.get("fee")),
        position_delta=_d((recon or {}).get("position_delta")),
        discrepancies=[c.check_name for c in checks if c.status in ("FAIL", "UNKNOWN")])
    return res
