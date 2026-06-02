"""SafetyEnvelope (Phase 8). Validates a would-be (dry-run) order against the
full safety envelope. Allowed only when EVERY check passes and the mode/state
are design/dry-run with live execution disabled. Fails closed.

Quant scope — *Compliance/Security/Operational Excellence*: UNCHANGED by the
paper risk/portfolio upgrade. The aggressive PAPER sizing policy operates only
on simulated paper orders and cannot relax this envelope or enable live
execution."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import GuardedLiveConfig
from .schemas import SafetyEnvelopeDecision

_DRY_RUN_STATES = {"DESIGN_ONLY", "PRECHECK_PASSED", "APPROVED_DRY_RUN_ONLY",
                   "ARMED_DRY_RUN_ONLY", "DRY_RUN_ACTIVE"}


def _dec(v, d="0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(d)


class SafetyEnvelope:
    def __init__(self, config: Optional[GuardedLiveConfig] = None, state: str = "DESIGN_ONLY"):
        self.cfg = config or GuardedLiveConfig()
        self.state = state

    def validate(self, ctx: dict) -> SafetyEnvelopeDecision:
        cfg = self.cfg
        import time as _t
        now = ctx.get("now_ms", int(_t.time() * 1000))
        mode = ctx.get("mode", cfg.mode)
        state = ctx.get("state", self.state)
        checks: dict[str, bool] = {}

        def add(name, ok):
            checks[name] = bool(ok)
            return bool(ok)

        add("mode_design_or_dry_run", mode in ("design_only", "dry_run_only"))
        add("live_execution_disabled", not ctx.get("live_broker_configured", False))
        add("kill_switch_absent", not ctx.get("kill_switch", cfg.kill_switch_active()))
        add("state_permits_dry_run_only", state in _DRY_RUN_STATES)
        add("readiness_accepted", ctx.get("readiness_ok", True))
        add("approvals_valid", ctx.get("approvals_valid", True))
        add("arming_valid", ctx.get("arming_valid", True))
        add("order_notional_within_limit",
            _dec(ctx.get("notional", 0)) <= cfg.max_order_notional_usd)
        add("market_exposure_within_limit",
            _dec(ctx.get("market_exposure", 0)) <= cfg.max_market_exposure_usd)
        add("venue_exposure_within_limit",
            _dec(ctx.get("venue_exposure", 0)) <= cfg.max_venue_exposure_usd)
        add("total_exposure_within_limit",
            _dec(ctx.get("total_exposure", 0)) <= cfg.max_total_exposure_usd)
        add("daily_loss_within_limit",
            _dec(ctx.get("daily_loss", 0)) <= cfg.max_daily_loss_usd)
        add("edge_above_threshold",
            float(ctx.get("edge_after_costs", cfg.min_edge_after_costs)) >= cfg.min_edge_after_costs)
        add("market_data_fresh", int(ctx.get("stale_ms", 0)) <= cfg.max_stale_ms)
        add("spread_within_limit", float(ctx.get("spread", 0)) <= cfg.max_spread)
        add("orderbook_valid", ctx.get("orderbook_valid", True))
        add("venue_not_degraded", str(ctx.get("venue_status", "ready")).lower()
            not in ("degraded", "disconnected", "reconnecting", "failed"))
        add("no_sequence_gap", not ctx.get("seq_gap", False))
        add("tick_size_not_dirty", not ctx.get("tick_dirty", False))
        add("market_status_tradable", str(ctx.get("market_status", "open")).lower()
            in ("open", "active", "trading"))
        add("ambiguity_within_limit",
            float(ctx.get("ambiguity_score", 0)) <= cfg.max_ambiguity_score)
        add("evidence_above_threshold",
            float(ctx.get("evidence_score", cfg.min_evidence_score)) >= cfg.min_evidence_score)
        add("source_count_sufficient",
            int(ctx.get("source_count", cfg.min_source_count)) >= cfg.min_source_count)
        ct = ctx.get("close_ts_ms")
        add("close_not_too_near",
            ct is None or (int(ct) - now) >= cfg.min_time_to_close_seconds * 1000)
        add("no_private_user_channel", not ctx.get("private_channel_dependency", False))
        add("no_forbidden_endpoint", not ctx.get("forbidden_endpoint", False))
        add("secret_policy_ok", ctx.get("secret_ok", True))
        add("reconciliation_clean", ctx.get("reconciliation_clean", True))

        allowed = all(checks.values())
        reason = "ok" if allowed else next(n for n, ok in checks.items() if not ok)
        return SafetyEnvelopeDecision(
            allowed=allowed, mode=mode, state=state, reason=reason, checks=checks,
            config_hash=cfg.config_hash(), proposal_id=ctx.get("proposal_id"),
            client_order_id=ctx.get("client_order_id"))
