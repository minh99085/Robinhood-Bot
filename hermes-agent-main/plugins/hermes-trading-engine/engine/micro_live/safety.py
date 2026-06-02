"""Micro-live SafetyEnvelope + RiskEngine adapter (Phase 9).

Quant scope — *Compliance/Security/Operational Excellence* + *Live Trading &
Monitoring*: the micro-live envelope and its MICRO caps are UNCHANGED by the
paper risk/portfolio upgrade. Aggressive PAPER sizing never touches this
live-execution control surface (which stays disabled by default).


MicroSafetyEnvelope runs the full safety check-set against a would-be live order
under MICRO caps (defaults: $1 notional, FOK only, demo only). It reuses the
Phase 8 SafetyEnvelopeDecision schema so decisions persist in the existing
``safety_envelope_decisions`` table. ``run_risk`` builds a TradeProposal and
RiskContext and calls the deterministic Phase 1 RiskEngine — every micro-live
submit must pass BOTH RiskEngine and SafetyEnvelope."""

from __future__ import annotations

import time
from decimal import Decimal

from ..risk import RiskContext, RiskEngine, RiskLimits
from ..schemas import TradeProposal
from .config import MicroLiveConfig


def _dec(v, d="0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(d)


class MicroSafetyEnvelope:
    def __init__(self, config: MicroLiveConfig):
        self.cfg = config

    def validate(self, ctx: dict):
        from ..guarded_live.schemas import SafetyEnvelopeDecision
        cfg = self.cfg
        checks: dict[str, bool] = {}

        def add(name, ok):
            checks[name] = bool(ok)

        env = ctx.get("environment", "demo")
        add("locks_open", ctx.get("locks_ok", False))
        add("kill_switch_absent", not ctx.get("kill_switch", cfg.kill_switch_active()))
        add("environment_allowed",
            env in cfg.allowed_environments and (env != "prod" or cfg.allow_production))
        add("venue_allowed", ctx.get("venue") in cfg.allowed_venues)
        add("market_allowed",
            (not cfg.market_allowlist) or ctx.get("market_ref") in cfg.market_allowlist)
        add("order_type_fok", cfg.order_type_allowed(ctx.get("order_type", "FOK")))
        add("tif_fok", cfg.tif_allowed(ctx.get("time_in_force", "fill_or_kill")))
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
        now = ctx.get("now_ms", int(time.time() * 1000))
        add("close_not_too_near",
            ct is None or (int(ct) - now) >= cfg.min_time_to_close_seconds * 1000)
        add("no_forbidden_endpoint", not ctx.get("forbidden_endpoint", False))
        add("secret_policy_ok", ctx.get("secret_ok", True))
        add("reconciliation_clean", ctx.get("reconciliation_clean", True))
        add("idempotency_ok", ctx.get("idempotency_ok", True))

        allowed = all(checks.values())
        reason = "ok" if allowed else next(n for n, ok in checks.items() if not ok)
        return SafetyEnvelopeDecision(
            allowed=allowed, mode="micro_live", state=ctx.get("state", "CANARY_READY"),
            reason=reason, checks=checks, config_hash=cfg.config_hash(),
            proposal_id=ctx.get("proposal_id"), client_order_id=ctx.get("client_order_id"))


def run_risk(config: MicroLiveConfig, ctx: dict):
    """Build a TradeProposal + RiskContext from the canary context and run the
    deterministic RiskEngine. Returns a RiskDecision."""
    notional = float(_dec(ctx.get("notional", 0)))
    # Use a tiny equity baseline so the absolute micro cap dominates.
    equity = float(ctx.get("equity", 100.0))
    limits = RiskLimits(
        max_order_notional_abs=float(config.max_order_notional_usd),
        max_daily_loss_frac=1.0)
    engine = RiskEngine(limits)
    proposal = TradeProposal(
        strategy="micro_live_canary", market="polymarket",
        symbol=ctx.get("market_ref", ""), side=ctx.get("side", "BUY"),
        notional=notional, price=float(_dec(ctx.get("limit_price", 0)) or 0),
        edge_after_costs=float(ctx.get("edge_after_costs", config.min_edge_after_costs)),
        spread=float(ctx.get("spread", 0)), data_age_s=float(ctx.get("stale_ms", 0)) / 1000.0,
        ambiguity_score=float(ctx.get("ambiguity_score", 0)), mode="live")
    rctx = RiskContext(equity=equity, total_exposure=float(ctx.get("total_exposure", 0)),
                       market_exposure=float(ctx.get("market_exposure", 0)),
                       open_orders=int(ctx.get("open_orders", 0)),
                       day_pnl=-float(ctx.get("daily_loss", 0)))
    return engine.evaluate(proposal, rctx)
