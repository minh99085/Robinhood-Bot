"""Paper-only multi-leg execution PLANNING for Bregman arbitrage (CLOB v2).

This is a *simulation/validation* planner — it never sends a real order, touches a
wallet, or mutates venue state. It answers one question for a certified Bregman
opportunity: *could this be executed as a truly atomic, risk-free fill on
Polymarket CLOB v2, right now, given the displayed book?*

Pipeline (deterministic, pure):

1. **Executable-depth snapshot** — read displayed depth per leg.
2. **Leg ordering** — sequence the riskiest (thinnest) leg first, so a failure
   happens before any capital is committed (leg-in risk control).
3. **FOK / IOC simulation** — Fill-Or-Kill requires every leg to fully fill or
   the whole plan is killed; IOC permits partials (which break the hedge).
4. **Atomicity-risk rejection** — Polymarket CLOB v2 has no atomic multi-order
   primitive, so a multi-leg arb is NOT atomically risk-free: it is **logged but
   not marked executable** unless the venue is declared atomic-capable.
5. **Worst-case slippage**, **timeout handling**, **reconciliation** (intended vs
   filled), and **post-trade attribution** (per-leg cost/fees + net edge).

Contract: *if true atomic risk-free execution cannot be guaranteed, the certified
opportunity is logged but ``executable`` is False.*

Quant responsibilities (data acquisition → compliance/security/ops)
-------------------------------------------------------------------
* **Data acquisition / market data** — supplies the read-only CLOB v2 book
  snapshots (depth, timestamps) this planner consumes.
* **Quant researcher** — sets FOK/IOC policy, timeout + slippage budgets, and the
  atomicity stance; validates against observed live fills.
* **Quant developer** — owns this pure planner + its rejection contract (tested).
* **Trader / execution** — acts only on ``executable`` plans; logs certified-but-
  non-executable opportunities for review.
* **Monitoring** — consumes rejected-fill / slippage / latency / timeout reasons.
* **Compliance / security / ops** — PAPER-only; no order/wallet path; every
  certified opportunity that cannot be atomically executed is recorded, not sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from engine.simulation.fill_model import (LatencyModel, OrderBook, ReplayFeeModel,
                                          simulate_fill)

logger = logging.getLogger("hte.execution.clob_v2")


@dataclass
class ExecLeg:
    """One leg to execute: buy/sell ``size`` shares of ``id`` against ``book``."""

    id: str
    book: OrderBook
    side: str = "buy"
    size: float = 1.0


@dataclass
class ExecLegResult:
    id: str
    side: str
    requested: float
    filled: float
    avg_price: float
    fees: float
    slippage_frac: float
    status: str          # filled | partial | rejected | timeout
    reason: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ClobV2Config:
    """Execution-planning policy (paper). Conservative defaults."""

    mode: str = "FOK"                          # FOK (all-or-nothing) or IOC
    timeout_ms: int = 1500
    max_worst_case_slippage_frac: float = 0.02
    # Polymarket CLOB v2 has NO atomic multi-order fill; keep False (safe).
    venue_supports_atomic_multileg: bool = False
    latency: LatencyModel = field(default_factory=LatencyModel)
    fee_model: ReplayFeeModel = field(default_factory=ReplayFeeModel)


@dataclass
class ExecutionPlan:
    executable: bool
    atomic_risk_free: bool
    certified: bool
    mode: str
    leg_order: list
    legs: list
    worst_case_slippage_frac: float
    total_cost: float
    total_fees: float
    after_cost_edge: float
    reason: str
    reconciliation: dict = field(default_factory=dict)
    attribution: dict = field(default_factory=dict)
    required_capital: float = 0.0
    fantasy_fills_rejected: int = 0
    certificate_status: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["legs"] = [l.to_dict() for l in self.legs]
        return d


class ClobV2ExecutionPlanner:
    """Pure, paper-only multi-leg execution planner for CLOB v2."""

    def __init__(self, config: Optional[ClobV2Config] = None):
        self.cfg = config or ClobV2Config()

    # -- depth + ordering ----------------------------------------------------
    @staticmethod
    def _available_depth(leg: ExecLeg) -> float:
        book = leg.book.asks if leg.side == "buy" else leg.book.bids
        return sum(max(0.0, float(l.size)) for l in book)

    def snapshot_depth(self, legs: Sequence[ExecLeg]) -> dict:
        """Displayed executable depth per leg (read-only snapshot)."""
        return {leg.id: round(self._available_depth(leg), 6) for leg in legs}

    def order_legs(self, legs: Sequence[ExecLeg]) -> list:
        """Order legs riskiest-first (thinnest available depth first) so a likely
        failure happens before any leg is committed (leg-in risk control)."""
        return sorted(legs, key=lambda l: self._available_depth(l))

    # -- planning ------------------------------------------------------------
    def plan(self, legs: Sequence[ExecLeg], *, decision_ts_ms: int, sets: float = 1.0,
             worst_case_payoff_per_set: float = 1.0, certified: bool = True) -> ExecutionPlan:
        """Build an execution plan for ``sets`` of a multi-leg arbitrage (pure)."""
        cfg = self.cfg
        n_sets = max(0.0, float(sets))
        ordered = self.order_legs(legs)
        order_ids = [l.id for l in ordered]

        # Global timeout: if round-trip latency exceeds the budget, nothing fills.
        timed_out = int(cfg.latency.latency_ms) > int(cfg.timeout_ms)

        results: list[ExecLegResult] = []
        total_cost = 0.0
        total_fees = 0.0
        worst_slip = 0.0
        all_filled = True
        first_fail = ""

        for leg in ordered:
            if timed_out:
                results.append(ExecLegResult(leg.id, leg.side, n_sets, 0.0, 0.0, 0.0,
                                             0.0, "timeout",
                                             f"latency {cfg.latency.latency_ms}ms > "
                                             f"timeout {cfg.timeout_ms}ms"))
                all_filled = False
                first_fail = first_fail or f"timeout:{leg.id}"
                continue
            oc = simulate_fill(side=leg.side, size=n_sets, book=leg.book,
                               decision_ts_ms=decision_ts_ms, fee_model=cfg.fee_model,
                               latency=cfg.latency)
            if oc.rejected:
                status = "rejected"
            elif oc.partial or oc.filled + 1e-12 < n_sets:
                status = "partial"
            else:
                status = "filled"
            results.append(ExecLegResult(
                leg.id, leg.side, round(n_sets, 8), oc.filled, oc.avg_price, oc.fees,
                oc.slippage_frac, status, oc.reason))
            total_cost += oc.notional
            total_fees += oc.fees
            worst_slip = max(worst_slip, oc.slippage_frac)
            if status != "filled":
                all_filled = False
                first_fail = first_fail or f"{status}:{leg.id}({oc.reason})"

        # FOK kills the whole plan if any leg is not fully filled.
        fok_killed = cfg.mode.upper() == "FOK" and not all_filled

        n_legs = len(ordered)
        venue_atomic = bool(cfg.venue_supports_atomic_multileg) or n_legs <= 1
        slippage_ok = worst_slip <= cfg.max_worst_case_slippage_frac
        atomic_risk_free = bool(all_filled and venue_atomic and not timed_out)

        executable = bool(certified and atomic_risk_free and slippage_ok
                          and not fok_killed and not timed_out)

        if not certified:
            reason = "not_certified_no_execution"
        elif timed_out:
            reason = "timeout"
        elif not all_filled:
            reason = f"unfillable_leg:{first_fail}" if cfg.mode.upper() == "FOK" \
                else f"partial_fill_breaks_hedge:{first_fail}"
        elif not slippage_ok:
            reason = (f"worst_case_slippage {worst_slip:.4f} > "
                      f"{cfg.max_worst_case_slippage_frac:.4f}")
        elif not venue_atomic:
            reason = "atomicity_risk_multi_leg_non_atomic_venue"
        else:
            reason = "executable_atomic_risk_free"

        worst_case_payoff = round(worst_case_payoff_per_set * n_sets, 10) if all_filled else 0.0
        after_cost_edge = round(worst_case_payoff - total_cost - total_fees, 10) if all_filled else 0.0

        reconciliation = {
            "intended_sets": round(n_sets, 8),
            "legs": {r.id: {"intended": round(n_sets, 8), "filled": r.filled,
                            "matched": abs(r.filled - n_sets) < 1e-9, "status": r.status}
                     for r in results},
            "all_matched": all(abs(r.filled - n_sets) < 1e-9 for r in results) if results else False,
        }
        attribution = {
            "total_cost": round(total_cost, 8), "total_fees": round(total_fees, 8),
            "after_cost_edge": after_cost_edge,
            "per_leg": {r.id: {"cost": round(r.avg_price * r.filled, 8), "fees": r.fees}
                        for r in results},
        }

        from engine.arbitrage.certificate import CertificateStatus
        fantasy_fills_rejected = sum(1 for r in results
                                     if r.status in ("partial", "rejected"))
        if executable:
            cert_status = CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED
        elif certified and all_filled and not timed_out and slippage_ok:
            # proven fills but multi-leg / non-atomic venue -> theoretical only
            cert_status = CertificateStatus.CERTIFIED_THEORETICAL_NOT_EXECUTABLE
        elif timed_out or any(r.status in ("rejected", "timeout") for r in results):
            cert_status = CertificateStatus.REJECTED_STALE_BOOK if "stale" in reason \
                else CertificateStatus.REJECTED_INSUFFICIENT_DEPTH
        else:
            cert_status = CertificateStatus.REJECTED_AFTER_COST_NONPOSITIVE

        plan = ExecutionPlan(
            executable=executable, atomic_risk_free=atomic_risk_free, certified=bool(certified),
            mode=cfg.mode.upper(), leg_order=order_ids, legs=results,
            worst_case_slippage_frac=round(worst_slip, 8), total_cost=round(total_cost, 8),
            total_fees=round(total_fees, 8), after_cost_edge=after_cost_edge, reason=reason,
            reconciliation=reconciliation, attribution=attribution,
            required_capital=round(total_cost, 8),
            fantasy_fills_rejected=fantasy_fills_rejected, certificate_status=cert_status)

        if executable:
            logger.info("clob_v2 plan EXECUTABLE legs=%d sets=%.2f after_cost_edge=%.4f",
                        n_legs, n_sets, after_cost_edge)
        elif certified:
            # Certified but cannot guarantee atomic risk-free execution -> LOG only.
            logger.info("clob_v2 plan certified-but-NOT-executable: reason=%s legs=%d",
                        reason, n_legs)
        else:
            logger.debug("clob_v2 plan not executable: %s", reason)
        return plan
