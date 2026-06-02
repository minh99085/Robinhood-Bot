"""LiveReadinessGate (Phase 7).

Evaluates whether the shadow system is stable enough for a HUMAN to consider
designing a future guarded-live phase. It NEVER enables live trading and NEVER
returns an "auto-live" status. Fails closed.
"""

from __future__ import annotations

from typing import Optional

from .config import ShadowConfig
from .schemas import LiveReadinessReport, ReadinessGateResult

PASS, FAIL, WARN, NED = "PASS", "FAIL", "WARN", "NOT_ENOUGH_DATA"


def _g(name, status, *, score=None, threshold=None, observed=None, reason="") -> ReadinessGateResult:
    return ReadinessGateResult(gate_name=name, status=status, score=score,
                               threshold=threshold, observed_value=observed, reason=reason)


class LiveReadinessGate:
    def __init__(self, config: ShadowConfig):
        self.cfg = config

    def evaluate(self, metrics: dict, counters: dict,
                 session_id: str = "") -> LiveReadinessReport:
        cfg = self.cfg
        m = metrics or {}
        c = counters or {}

        def cnt(k, d=0):
            return int(c.get(k, d))

        def mv(k, d=None):
            v = m.get(k, d)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        hard: list[ReadinessGateResult] = []
        other: list[ReadinessGateResult] = []

        # ---- hard safety gates (any FAIL => NOT_READY) ----
        hard.append(_g("risk_bypass_count", FAIL if cnt("risk_bypass_count") > 0 else PASS,
                       observed=cnt("risk_bypass_count"), threshold=0,
                       reason="no proposal may bypass RiskEngine"))
        hard.append(_g("unhandled_exception_count",
                       FAIL if cnt("unhandled_exception_count") > 0 else PASS,
                       observed=cnt("unhandled_exception_count"), threshold=0))
        hard.append(_g("live_order_endpoint_calls",
                       FAIL if cnt("live_order_endpoint_calls") > 0 else PASS,
                       observed=cnt("live_order_endpoint_calls"), threshold=0,
                       reason="no live order/cancel endpoint may be called"))
        hard.append(_g("secret_leak_count", FAIL if cnt("secret_leak_count") > 0 else PASS,
                       observed=cnt("secret_leak_count"), threshold=0))
        hard.append(_g("reconciliation_clean",
                       PASS if c.get("reconciliation_clean", True) else FAIL))
        hard.append(_g("no_real_broker_configured",
                       FAIL if c.get("real_broker_configured", False) else PASS))
        hard.append(_g("orders_only_via_shadow_oms",
                       FAIL if cnt("orders_outside_oms") > 0 else PASS,
                       observed=cnt("orders_outside_oms"), threshold=0))
        hard.append(_g("orders_have_risk_decision",
                       FAIL if cnt("orders_without_risk") > 0 else PASS,
                       observed=cnt("orders_without_risk"), threshold=0))

        # ---- data-quality gates ----
        def thresh_gate(name, value, limit, *, higher_is_better, reason=""):
            if value is None:
                return _g(name, NED, threshold=limit, reason="no data")
            ok = value >= limit if higher_is_better else value <= limit
            return _g(name, PASS if ok else FAIL, observed=value, threshold=limit, reason=reason)

        other.append(thresh_gate("venue_uptime_pct", mv("venue_uptime_pct"),
                                 cfg.required_venue_uptime_pct, higher_is_better=True))
        other.append(thresh_gate("stale_book_rate", mv("stale_book_rate"),
                                 cfg.max_stale_book_rate, higher_is_better=False))
        other.append(thresh_gate("parse_error_rate", mv("parse_error_rate"),
                                 cfg.max_parse_error_rate, higher_is_better=False))
        other.append(thresh_gate("sequence_gap_rate", mv("sequence_gap_rate"),
                                 cfg.max_sequence_gap_rate, higher_is_better=False))

        # ---- execution-simulation gates ----
        other.append(thresh_gate("fill_ratio", mv("fill_ratio"), cfg.min_fill_ratio,
                                 higher_is_better=True))
        other.append(thresh_gate("edge_capture_ratio", mv("edge_capture_ratio"),
                                 cfg.min_edge_capture_ratio, higher_is_better=True))
        other.append(thresh_gate("reject_rate", mv("reject_rate"), cfg.max_reject_rate,
                                 higher_is_better=False))

        # ---- performance gates ----
        other.append(thresh_gate("max_drawdown_pct", mv("max_drawdown_pct"),
                                 cfg.max_drawdown_pct, higher_is_better=False))
        total_pnl = mv("total_pnl")
        other.append(_g("total_pnl_positive", NED if total_pnl is None
                        else (PASS if total_pnl > 0 else FAIL), observed=total_pnl))

        # ---- calibration gates (NOT_ENOUGH_DATA if too few resolved) ----
        resolved = cnt("resolved_sample_count", int(m.get("resolved_sample_count", 0) or 0))
        if resolved < cfg.min_calibration_samples:
            other.append(_g("calibration", NED, observed=resolved,
                            threshold=cfg.min_calibration_samples, reason="not enough resolved"))
        else:
            other.append(thresh_gate("brier_score", mv("brier_score"), cfg.max_brier_score,
                                     higher_is_better=False))
            other.append(thresh_gate("log_loss", mv("log_loss"), cfg.max_log_loss,
                                     higher_is_better=False))
            other.append(thresh_gate("ece", mv("ece"), cfg.max_ece, higher_is_better=False))

        gates = hard + other
        hard_fail = any(g.status == FAIL for g in hard)
        other_fail = any(g.status == FAIL for g in other)
        decisions = cnt("decisions")
        runtime_hours = float(c.get("runtime_hours", 0) or 0)
        not_enough = (decisions < cfg.min_decisions_for_readiness
                      or runtime_hours < cfg.min_runtime_hours_for_readiness)
        any_ned = any(g.status == NED for g in other)
        any_warn = any(g.status == WARN for g in gates)

        if hard_fail or other_fail:
            overall = "NOT_READY"
            step = "fix_data_quality" if other_fail and not hard_fail else "fix_risk_rejections"
        elif not_enough:
            overall = "NOT_ENOUGH_DATA"
            step = "collect_more_samples"
        elif any_ned or any_warn:
            overall = "SHADOW_STABLE_BUT_NOT_APPROVED"
            step = "collect_more_samples"
        else:
            overall = "READY_FOR_MANUAL_REVIEW"
            step = "manual_review_for_guarded_live_design"

        return LiveReadinessReport(
            shadow_session_id=session_id, overall_status=overall, gate_results=gates,
            metrics_summary={k: m.get(k) for k in (
                "fill_ratio", "edge_capture_ratio", "reject_rate", "max_drawdown_pct",
                "total_pnl", "stale_book_rate", "sequence_gap_rate", "venue_uptime_pct",
                "brier_score", "log_loss", "ece")},
            recommended_next_step=step)
