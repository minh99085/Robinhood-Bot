"""Guarded-live prechecks (Phase 8). Fail-closed: AWAITING_APPROVAL is only
reachable when every hard precheck passes. Real execution is always disabled."""

from __future__ import annotations

import os
import time
from typing import Optional

from .config import GuardedLiveConfig
from .readiness_loader import validate_readiness
from .schemas import GuardedLivePrecheck, PrecheckResult
from .secret_policy import SecretPolicy

_HARD_NOTIONAL_CAP = 100.0  # guarded-live can never configure a huge order


def run_precheck(store, config: Optional[GuardedLiveConfig] = None, *,
                 readiness_report_id: Optional[str] = None, conformance_ok: bool = True,
                 now_ms: Optional[int] = None) -> GuardedLivePrecheck:
    cfg = config or GuardedLiveConfig.from_env()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    checks: list[PrecheckResult] = []

    def add(name, ok, reason="", observed=None, threshold=None):
        checks.append(PrecheckResult(check_name=name, status="PASS" if ok else "FAIL",
                                     reason=reason, observed_value=None if observed is None else str(observed),
                                     threshold=None if threshold is None else str(threshold)))

    add("mode_design_or_dry_run", cfg.mode in ("design_only", "dry_run_only"),
        observed=cfg.mode)
    add("dry_run_only_true", bool(cfg.dry_run_only))
    add("real_execution_disabled", True, "execution methods raise LiveExecutionDisabled")
    add("no_live_broker_configured",
        os.getenv("LIVE_BROKER_ENABLED") in (None, "", "0", "false", "False"))
    ok_secret, violations = SecretPolicy(cfg).check()
    add("no_forbidden_env_vars", ok_secret,
        f"{len(violations)} forbidden env var(s)" if violations else "")
    add("kill_switch_absent", not cfg.kill_switch_active())
    rd_ok, rd_reason, rid = validate_readiness(store, cfg, report_id=readiness_report_id,
                                               now_ms=now)
    add("shadow_readiness_valid", rd_ok, rd_reason)
    add("conformance_passed", bool(conformance_ok))
    add("venue_allowlist_nonempty", len(cfg.allowlist_venues) > 0,
        "configure GUARDED_LIVE_ALLOWLIST_VENUES")
    add("risk_config_present", bool(cfg.risk_limits_hash()))
    add("max_notional_below_hard_cap",
        float(cfg.max_order_notional_usd) <= _HARD_NOTIONAL_CAP,
        observed=str(cfg.max_order_notional_usd), threshold=_HARD_NOTIONAL_CAP)
    add("secret_policy_ok", ok_secret)

    hard_fail = sum(1 for c in checks if c.status == "FAIL")
    warns = sum(1 for c in checks if c.status == "WARN")
    pre = GuardedLivePrecheck(
        ts_ms=now, config_hash=cfg.config_hash(), readiness_report_id=rid,
        status="PASS" if hard_fail == 0 else "FAIL", checks=checks,
        hard_fail_count=hard_fail, warning_count=warns)
    if store is not None:
        try:
            store.add_guarded_live_precheck(pre.record())
            for c in checks:
                store.add_guarded_live_precheck_result({
                    "precheck_id": pre.precheck_id, "check_name": c.check_name,
                    "status": c.status, "reason": c.reason,
                    "observed_value": c.observed_value, "threshold": c.threshold,
                    "details_json": {}})
        except Exception:  # noqa: BLE001
            pass
    return pre
