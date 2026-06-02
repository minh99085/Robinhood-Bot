"""Bregman arbitrage opportunity certification (deterministic, offline, PAPER).

Quant scope — *Bregman arbitrage priority* + *Risk Management & Portfolio
Optimization* + *Execution Engine CLOB v2 simulation* + *Strategy Optimization &
Robustness Testing*:

Certify only **fully-hedged, all-leg-executable** "buy the complete set"
opportunities on mutually-exclusive + exhaustive Polymarket groups. A group of
N outcomes where exactly one resolves YES (paying $1) is an arbitrage iff the
total executable BUY cost of one share of every leg is below $1 **after** every
real-world cost and feasibility check:

    spread, taker fees, slippage, tick rounding (UP), depth limits, stale-book
    checks, Chainlink relevance, and settlement ambiguity.

Bregman arbitrage capital allocation — the certified ``sets`` / ``cost_per_set``
/ ``worst_case_pnl`` / per-leg depth here are the inputs to
:func:`engine.training.portfolio.bregman_bundle_size`, which allocates PAPER
capital with a leg-failure haircut and a hard Bregman-bundle exposure cap. Each
sized leg still routes through the mandatory RiskEngine.

Hard safety invariants:

* An opportunity is labelled ``risk_free`` ONLY when certification verifies a
  full hedge, all-leg executability, AND a positive worst-case PnL.
* A Bregman opportunity outranks a directional trade ONLY when its certified
  profit lower bound is strictly positive after all costs.
* PAPER ONLY. This module computes + certifies; it never sizes a live order,
  never submits, never bypasses the RiskEngine. Legacy cross-exchange arbitrage
  stays permanently disabled (``engine.arb.execution``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from .bregman import divergence_gap
from .bregman_grouping import SimplexGroup, SimplexLeg, validate_simplex

logger = logging.getLogger("hte.training.bregman_execution")

# Canonical Bregman no-trade / failure vocabulary.
FAILURE_MODES = (
    "invalid_simplex", "missing_leg", "no_executable_price", "stale_book",
    "tick_size_changed", "depth_too_thin", "spread_too_wide",
    "settlement_ambiguity", "chainlink_stale_or_irrelevant", "no_positive_edge",
    "zero_quantity",
)


@dataclass
class CertifiedLeg:
    market_id: str
    outcome: str
    token_id: str
    side: str
    executable_price: float
    quantity: float
    depth_usd: float
    tick_size: float

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


@dataclass
class CertifiedBregmanOpportunity:
    """A certified (or rejected) fully-hedged Bregman opportunity."""

    group_id: str
    group_type: str
    legs: list[CertifiedLeg]
    executable_prices: list[float]
    quantities: list[float]
    required_capital: float
    worst_case_pnl: float
    profit_lower_bound: float
    divergence_gap: float
    divergence_method: str
    failure_modes: list[str]
    fill_feasibility: float
    persistence_score: float
    no_trade_reason: str
    certified: bool
    risk_free: bool
    cost_per_set: float = 0.0
    sets: float = 0.0

    @property
    def is_opportunity(self) -> bool:
        """Tradable iff certified AND the profit lower bound is strictly positive."""
        return self.certified and self.profit_lower_bound > 0.0

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "group_type": self.group_type,
            "legs": [l.to_dict() for l in self.legs],
            "executable_prices": [round(p, 6) for p in self.executable_prices],
            "quantities": [round(q, 6) for q in self.quantities],
            "required_capital": round(self.required_capital, 6),
            "worst_case_pnl": round(self.worst_case_pnl, 6),
            "profit_lower_bound": round(self.profit_lower_bound, 6),
            "divergence_gap": round(self.divergence_gap, 8),
            "divergence_method": self.divergence_method,
            "failure_modes": list(self.failure_modes),
            "fill_feasibility": round(self.fill_feasibility, 6),
            "persistence_score": round(self.persistence_score, 6),
            "no_trade_reason": self.no_trade_reason,
            "certified": self.certified, "risk_free": self.risk_free,
            "cost_per_set": round(self.cost_per_set, 6),
            "sets": round(self.sets, 6), "is_opportunity": self.is_opportunity,
        }


def _round_up_to_tick(price: float, tick: float) -> float:
    """Conservatively round an executable BUY price UP to the next tick."""
    if tick and tick > 0:
        return math.ceil(price / tick - 1e-9) * tick
    return price


class BregmanArbitrageEngine:
    """Certify fully-hedged Bregman arbitrage opportunities (PAPER / replay)."""

    def __init__(self, cfg=None, *, chainlink=None, slippage_bps: float = 25.0,
                 taker_fee_bps: float = 0.0, min_profit_usd: float = 1e-4,
                 min_depth_usd: float = 50.0, max_spread: float = 0.08,
                 max_ambiguity: float = 0.35, target_capital_usd: float = 100.0,
                 divergence_method: str = "squared_euclidean"):
        # cfg (TrainingConfig) overrides explicit kwargs when present.
        self.cfg = cfg
        self.chainlink = chainlink
        self.slippage_bps = float(getattr(cfg, "slippage_bps", slippage_bps))
        self.taker_fee_bps = float(getattr(cfg, "taker_fee_bps", taker_fee_bps))
        self.min_profit_usd = float(getattr(cfg, "bregman_min_profit_usd", min_profit_usd))
        self.min_depth_usd = float(getattr(cfg, "min_depth_at_price", min_depth_usd))
        self.max_spread = float(getattr(cfg, "max_spread", max_spread))
        self.max_ambiguity = float(getattr(cfg, "max_ambiguity_score", max_ambiguity))
        self.target_capital_usd = float(
            getattr(cfg, "bregman_target_capital_usd", target_capital_usd))
        self.divergence_method = divergence_method

    # -- certification -------------------------------------------------------
    def certify(self, group: SimplexGroup, *, now: Optional[float] = None,
                fill_model=None, min_all_leg_fill_prob: float = 0.95
                ) -> CertifiedBregmanOpportunity:
        method = self.divergence_method
        gap = divergence_gap(group.observed_prices, method=method)
        failures: list[str] = []

        def reject(reason: str) -> CertifiedBregmanOpportunity:
            if reason not in failures:
                failures.append(reason)
            return CertifiedBregmanOpportunity(
                group_id=group.group_id, group_type=group.group_type, legs=[],
                executable_prices=[], quantities=[], required_capital=0.0,
                worst_case_pnl=0.0, profit_lower_bound=0.0, divergence_gap=gap,
                divergence_method=method, failure_modes=failures,
                fill_feasibility=0.0, persistence_score=0.0,
                no_trade_reason=reason, certified=False, risk_free=False)

        ok, why = validate_simplex(group)
        if not ok:
            return reject("invalid_simplex" if why.startswith(
                ("insufficient", "duplicate", "unknown", "non_positive"))
                else why)

        # --- per-leg feasibility + cost (conservative: rounds against us) ---
        slip = self.slippage_bps / 10000.0
        fee = self.taker_fee_bps / 10000.0
        cost_per_set = 0.0
        exec_prices: list[float] = []
        depth_qty: list[float] = []
        spreads: list[float] = []
        for leg in group.legs:
            if leg.ask is None or leg.ask <= 0.0:
                failures.append("missing_leg")
                return reject("no_executable_price")
            if not leg.fresh_book or leg.stale:
                return reject("stale_book")
            if leg.tick_size_dirty:
                return reject("tick_size_changed")
            if leg.chainlink_no_trade or not leg.chainlink_relevant:
                return reject("chainlink_stale_or_irrelevant")
            if leg.ambiguity_score > self.max_ambiguity:
                return reject("settlement_ambiguity")
            sp = leg.spread
            if sp is not None:
                spreads.append(sp)
                if sp > self.max_spread:
                    return reject("spread_too_wide")
            if leg.depth_usd < self.min_depth_usd:
                return reject("depth_too_thin")
            # conservative executable price: tick-round UP, then fee + slippage
            px = _round_up_to_tick(float(leg.ask), leg.tick_size)
            px = px * (1.0 + slip) + px * fee
            exec_prices.append(px)
            cost_per_set += px
            depth_qty.append(leg.depth_usd / px if px > 0 else 0.0)

        profit_per_set = group.payout - cost_per_set
        if profit_per_set <= 0.0:
            return reject("no_positive_edge")

        # --- sizing + fill feasibility ---
        q_target = self.target_capital_usd / cost_per_set if cost_per_set > 0 else 0.0
        q_depth = min(depth_qty) if depth_qty else 0.0
        sets = min(q_target, q_depth)
        if sets <= 0.0:
            return reject("zero_quantity")
        fill_feasibility = min(1.0, sets / q_target) if q_target > 0 else 0.0

        quantities = [sets] * len(group.legs)
        required_capital = sets * cost_per_set
        worst_case_pnl = sets * profit_per_set     # deterministic (exactly one leg pays)
        profit_lower_bound = worst_case_pnl        # fully hedged -> lower == realized

        persistence = self._persistence_score(group.legs, spreads, sets, q_depth)

        full_hedge = group.mutually_exclusive and group.exhaustive
        all_executable = all(l.executable for l in group.legs)
        certified = full_hedge and all_executable and worst_case_pnl > 0.0
        risk_free = certified and worst_case_pnl > 0.0 and full_hedge and all_executable

        # CLOB v2 fill-risk gate (PAPER realism): a full hedge is only risk-free
        # if EVERY leg can actually fill. When a fill model is supplied, compute
        # the all-leg fill probability; if it is below the floor, the hedge can
        # break under partial fills, so it is NOT risk-free. Conservative: this
        # only ever REMOVES the risk-free label, never adds one.
        if fill_model is not None and group.legs:
            all_leg_fill = 1.0
            for leg, px in zip(group.legs, exec_prices):
                sp = leg.spread if leg.spread is not None else 0.0
                all_leg_fill *= fill_model.fill_probability(
                    spread=float(sp), depth_usd=float(leg.depth_usd),
                    order_usd=float(sets * px), aggressiveness=1.0,
                    stale=bool(leg.stale or not leg.fresh_book))
            if all_leg_fill < float(min_all_leg_fill_prob):
                if "partial_fill_breaks_hedge" not in failures:
                    failures.append("partial_fill_breaks_hedge")
                risk_free = False

        certified_legs = [
            CertifiedLeg(market_id=l.market_id, outcome=l.outcome,
                         token_id=l.token_id or f"{l.market_id}:{l.outcome}",
                         side="BUY", executable_price=px, quantity=sets,
                         depth_usd=l.depth_usd, tick_size=l.tick_size)
            for l, px in zip(group.legs, exec_prices)]

        opp = CertifiedBregmanOpportunity(
            group_id=group.group_id, group_type=group.group_type, legs=certified_legs,
            executable_prices=exec_prices, quantities=quantities,
            required_capital=required_capital, worst_case_pnl=worst_case_pnl,
            profit_lower_bound=profit_lower_bound, divergence_gap=gap,
            divergence_method=method, failure_modes=failures,
            fill_feasibility=fill_feasibility, persistence_score=persistence,
            no_trade_reason="" if certified else "not_certified",
            certified=certified, risk_free=risk_free,
            cost_per_set=cost_per_set, sets=sets)
        logger.info("bregman certify group=%s certified=%s risk_free=%s "
                    "profit_lb=%.6f cost/set=%.4f sets=%.2f gap=%.6f",
                    group.group_id, certified, risk_free, profit_lower_bound,
                    cost_per_set, sets, gap)
        return opp

    def _persistence_score(self, legs: list[SimplexLeg], spreads: list[float],
                           sets: float, q_depth: float) -> float:
        """Heuristic 0..1 persistence: tighter spreads + ample depth headroom +
        all-fresh books => more likely to still be there when we execute."""
        if not legs:
            return 0.0
        spread_term = 1.0
        if spreads:
            spread_term = max(0.0, 1.0 - (max(spreads) / max(1e-9, self.max_spread)))
        depth_headroom = 0.0 if sets <= 0 else max(0.0, min(1.0, (q_depth - sets) / max(1e-9, sets)))
        fresh_term = 1.0 if all(l.fresh_book and not l.stale for l in legs) else 0.0
        return round(0.5 * spread_term + 0.3 * depth_headroom + 0.2 * fresh_term, 6)

    # -- scanning + ranking --------------------------------------------------
    def scan(self, groups: list[SimplexGroup], *, now: Optional[float] = None
             ) -> list[CertifiedBregmanOpportunity]:
        """Certify every group; return tradable opportunities sorted by certified
        profit lower bound (descending)."""
        opps = [self.certify(g, now=now) for g in groups]
        tradable = [o for o in opps if o.is_opportunity]
        tradable.sort(key=lambda o: o.profit_lower_bound, reverse=True)
        return tradable

    def certify_all(self, groups: list[SimplexGroup], *, now: Optional[float] = None
                    ) -> list[CertifiedBregmanOpportunity]:
        """Certify every group (tradable AND rejected) — for reporting / metrics."""
        return [self.certify(g, now=now) for g in groups]

    @staticmethod
    def outranks_directional(opp: CertifiedBregmanOpportunity,
                             directional_net_edge: float = 0.0) -> bool:
        """A Bregman opportunity outranks a directional trade ONLY when certified
        with a strictly-positive profit lower bound after all costs."""
        return bool(opp.certified and opp.profit_lower_bound > 0.0)


# --------------------------------------------------------------------------- #
# Multi-leg bundle execution simulator (PAPER ONLY)
# --------------------------------------------------------------------------- #
@dataclass
class BundleLegResult:
    market_id: str
    outcome: str
    requested_qty: float
    filled_qty: float
    fill_price: float
    fraction: float
    status: str          # "filled" | "partial" | "unfilled" | "cancelled"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


@dataclass
class BundleExecutionResult:
    """Outcome of simulating a certified Bregman bundle against realistic fills."""

    group_id: str
    total_legs: int
    filled_legs: int
    fully_hedged: bool
    hedge_complete: bool
    failure_mode: str
    realized_cost: float
    realized_pnl: float
    partial_fill_rate: float
    cancelled: bool
    timed_out: bool
    leg_results: list

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "total_legs": self.total_legs,
            "filled_legs": self.filled_legs, "fully_hedged": self.fully_hedged,
            "hedge_complete": self.hedge_complete, "failure_mode": self.failure_mode,
            "realized_cost": round(self.realized_cost, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "partial_fill_rate": round(self.partial_fill_rate, 6),
            "cancelled": self.cancelled, "timed_out": self.timed_out,
            "leg_results": [l.to_dict() for l in self.leg_results],
        }


class BregmanBundleExecutionSimulator:
    """Simulate executing a certified Bregman bundle leg-by-leg with realistic
    fills, a per-bundle timeout, cancel-on-leg-failure, and failure-mode
    reporting (CLOB v2 simulation + Bregman execution risk).

    The key realism: a certified "buy the complete set" hedge is only risk-free
    if EVERY leg fully fills. If any leg partials or fails (or the bundle times
    out), the hedge is BROKEN — the filled legs are an unhedged basket whose
    worst case (the missing leg wins) is a loss. PAPER ONLY; never submits."""

    def __init__(self, *, fill_model=None, timeout_ms: int = 2000,
                 cancel_on_leg_failure: bool = True, full_fill_tolerance: float = 1e-6):
        from ..execution.paper_broker import RealisticFillModel
        self.fill_model = fill_model or RealisticFillModel()
        self.timeout_ms = int(timeout_ms)
        self.cancel_on_leg_failure = bool(cancel_on_leg_failure)
        self.tol = float(full_fill_tolerance)

    def simulate(self, opp: CertifiedBregmanOpportunity, *,
                 leg_fill_fractions: Optional[list] = None,
                 leg_latencies_ms: Optional[list] = None,
                 now: Optional[float] = None) -> BundleExecutionResult:
        legs = list(opp.legs)
        n = len(legs)
        sets = float(opp.sets)
        cost_per_set = float(opp.cost_per_set)
        # group payout is $1 per share for a complete set (one outcome resolves YES)
        payout = 1.0

        leg_results: list = []
        cum_latency = 0.0
        timed_out = False
        cancelled = False
        realized_cost = 0.0
        fully_filled = 0
        partial_or_failed = 0

        for i, leg in enumerate(legs):
            px = float(leg.executable_price)
            req = float(leg.quantity)
            # latency / timeout
            cum_latency += float(leg_latencies_ms[i]) if (leg_latencies_ms
                                                          and i < len(leg_latencies_ms)) else 0.0
            if self.timeout_ms and cum_latency > self.timeout_ms:
                timed_out = True
                leg_results.append(BundleLegResult(
                    market_id=leg.market_id, outcome=leg.outcome, requested_qty=req,
                    filled_qty=0.0, fill_price=px, fraction=0.0, status="cancelled"))
                partial_or_failed += 1
                cancelled = True
                continue

            # fill fraction: explicit override, else modeled from leg depth
            if leg_fill_fractions is not None and i < len(leg_fill_fractions):
                frac = max(0.0, min(1.0, float(leg_fill_fractions[i])))
            else:
                frac = self.fill_model.fill_fraction(order_usd=sets * px,
                                                     depth_usd=float(leg.depth_usd))
            filled = req * frac
            realized_cost += filled * px
            if frac >= 1.0 - self.tol:
                status = "filled"
                fully_filled += 1
            elif filled > 0:
                status = "partial"
                partial_or_failed += 1
            else:
                status = "unfilled"
                partial_or_failed += 1
            leg_results.append(BundleLegResult(
                market_id=leg.market_id, outcome=leg.outcome, requested_qty=req,
                filled_qty=filled, fill_price=px, fraction=frac, status=status))
            # cancel the rest of the bundle once a leg fails to fully fill
            if status != "filled" and self.cancel_on_leg_failure and i < n - 1:
                cancelled = True
                for j in range(i + 1, n):
                    lj = legs[j]
                    leg_results.append(BundleLegResult(
                        market_id=lj.market_id, outcome=lj.outcome,
                        requested_qty=float(lj.quantity), filled_qty=0.0,
                        fill_price=float(lj.executable_price), fraction=0.0,
                        status="cancelled"))
                    partial_or_failed += 1
                break

        fully_hedged = (fully_filled == n) and not timed_out
        if fully_hedged:
            failure_mode = ""
            realized_pnl = sets * (payout - cost_per_set)        # the certified profit
        elif timed_out:
            failure_mode = "timeout"
            # filled legs are unhedged; worst case the missing leg wins => lose cost
            realized_pnl = -realized_cost
        else:
            failure_mode = "partial_fill_breaks_hedge"
            realized_pnl = -realized_cost
        partial_fill_rate = round(partial_or_failed / n, 6) if n else 0.0

        return BundleExecutionResult(
            group_id=opp.group_id, total_legs=n, filled_legs=fully_filled,
            fully_hedged=fully_hedged, hedge_complete=fully_hedged,
            failure_mode=failure_mode, realized_cost=round(realized_cost, 6),
            realized_pnl=round(realized_pnl, 6), partial_fill_rate=partial_fill_rate,
            cancelled=cancelled, timed_out=timed_out, leg_results=leg_results)
