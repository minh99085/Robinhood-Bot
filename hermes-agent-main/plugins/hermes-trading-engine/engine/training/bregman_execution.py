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
    "zero_quantity", "market_closed", "partial_fill_breaks_hedge",
)

# Pipeline STAGE for each reject reason — cures "silent certification" by naming the
# exact stage a discovered group stopped at (grouping vs structural validity vs
# settlement consistency vs realism vs edge). Reported on EVERY reject.
_STAGE_VALIDATE_SIMPLEX = "validate_simplex"
_STAGE_SETTLEMENT = "settlement_consistent"
_STAGE_REALISM = "realism"
_STAGE_EDGE = "edge"
_STAGE_CERTIFIED = "certified"

_REJECTION_STAGE = {
    "not_exhaustive": _STAGE_VALIDATE_SIMPLEX,
    "not_mutually_exclusive": _STAGE_VALIDATE_SIMPLEX,
    "invalid_simplex": _STAGE_VALIDATE_SIMPLEX,
    "duplicate_legs": _STAGE_VALIDATE_SIMPLEX,
    "insufficient_legs": _STAGE_VALIDATE_SIMPLEX,
    "settlement_ambiguity": _STAGE_SETTLEMENT,
    "chainlink_stale_or_irrelevant": _STAGE_SETTLEMENT,
    "missing_leg": _STAGE_REALISM,
    "no_executable_price": _STAGE_REALISM,
    "stale_book": _STAGE_REALISM,
    "tick_size_changed": _STAGE_REALISM,
    "depth_too_thin": _STAGE_REALISM,
    "spread_too_wide": _STAGE_REALISM,
    "market_closed": _STAGE_REALISM,
    "zero_quantity": _STAGE_REALISM,
    "partial_fill_breaks_hedge": _STAGE_REALISM,
    "no_positive_edge": _STAGE_EDGE,
}


def rejection_stage(reason: str) -> str:
    """Map a certifier reject reason to its pipeline stage (pure, exhaustive)."""
    return _REJECTION_STAGE.get(str(reason or ""), _STAGE_REALISM)


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
class BregmanCertificate:
    """Institutional-grade Bregman certificate (the full proof / rejection record).

    Captures the market set + outcome legs, executable prices, size, required
    capital, worst-case PnL, the full cost-drag decomposition (fee / spread /
    slippage / tick-rounding), depth sufficiency, all-leg fill probability,
    stale-book + settlement-ambiguity scores, settlement consistency, and the
    failure modes. ``risk_free`` is True ONLY when full hedge + all-leg
    executability + positive worst-case PnL + settlement consistency are proven.
    """

    group_id: str
    group_type: str
    market_set: list
    outcome_legs: list
    executable_prices: list
    size: float
    required_capital: float
    worst_case_pnl: float
    fee_drag: float
    spread_drag: float
    slippage_drag: float
    tick_rounding_drag: float
    depth_sufficiency: float
    fill_probability: float
    stale_book_score: float
    settlement_ambiguity_score: float
    full_hedge: bool
    all_leg_executable: bool
    settlement_consistent: bool
    certified: bool
    risk_free: bool
    failure_modes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 8)
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
    certificate: Optional["BregmanCertificate"] = None
    # certification-silence cure: the precise pipeline STAGE that stopped the group
    # (grouping / validate_simplex / settlement_consistent / realism / certified), plus
    # a structured per-group diagnostic logged EVEN ON REJECT (never silent).
    rejection_stage: str = ""
    certify_diagnostics: dict = field(default_factory=dict)
    # True when ANY leg uses a SYNTHETIC (derived 1-YES-bid) price -> diagnostic only,
    # never executable. Real CLOB-hydrated legs set this False so a real binary can
    # open (subject to all unchanged strict gates).
    has_synthetic_leg: bool = False

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
            "rejection_stage": self.rejection_stage,
            "certify_diagnostics": dict(self.certify_diagnostics),
            "has_synthetic_leg": self.has_synthetic_leg,
            "certificate": self.certificate.to_dict() if self.certificate else None,
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
        from engine.market_data.orderbook import stale_book_score as _stale_score
        method = self.divergence_method
        gap = divergence_gap(group.observed_prices, method=method)
        failures: list[str] = []

        # settlement-consistency + book-quality scores computable from the raw
        # legs even on an early reject (so the certificate always carries them).
        max_amb = max((float(l.ambiguity_score or 0.0) for l in group.legs), default=0.0)
        stale_sc = max((1.0 if (l.stale or not l.fresh_book)
                        else (_stale_score(l.book_age_s) if l.book_age_s is not None else 0.0)
                        for l in group.legs), default=0.0)
        settlement_consistent = bool(group.mutually_exclusive and group.exhaustive
                                     and max_amb <= self.max_ambiguity)

        def _certificate(*, certified: bool, risk_free: bool, exec_prices=None,
                         sets: float = 0.0, required_capital: float = 0.0,
                         worst_case_pnl: float = 0.0, drags=None,
                         depth_suff: float = 0.0, fill_prob: float = 0.0,
                         all_exec: bool = False) -> BregmanCertificate:
            dr = drags or {"fee": 0.0, "spread": 0.0, "slippage": 0.0, "tick": 0.0}
            return BregmanCertificate(
                group_id=group.group_id, group_type=group.group_type,
                market_set=[l.market_id for l in group.legs],
                outcome_legs=[f"{l.market_id}:{l.outcome}" for l in group.legs],
                executable_prices=list(exec_prices or []), size=float(sets),
                required_capital=float(required_capital),
                worst_case_pnl=float(worst_case_pnl),
                fee_drag=float(dr["fee"]), spread_drag=float(dr["spread"]),
                slippage_drag=float(dr["slippage"]), tick_rounding_drag=float(dr["tick"]),
                depth_sufficiency=float(depth_suff), fill_probability=float(fill_prob),
                stale_book_score=float(stale_sc), settlement_ambiguity_score=float(max_amb),
                full_hedge=bool(group.mutually_exclusive and group.exhaustive),
                all_leg_executable=bool(all_exec),
                settlement_consistent=settlement_consistent,
                certified=bool(certified), risk_free=bool(risk_free),
                failure_modes=list(failures))

        def reject(reason: str) -> CertifiedBregmanOpportunity:
            if reason not in failures:
                failures.append(reason)
            stage = rejection_stage(reason)
            # profit lower bound computable EVEN on early reject: payout - sum(observed
            # executable asks). ALWAYS a float (negative / zero / positive) so the
            # certifier is never silent. ``all_legs_priced`` flags reliability when
            # some legs have no executable ask (the float is then an under-estimate).
            try:
                implied = float(group.implied_sum)
            except Exception:  # noqa: BLE001
                implied = 0.0
            all_legs_priced = bool(group.legs) and all(
                (getattr(l, "ask", None) or 0) > 0 for l in group.legs)
            profit_lb = round(float(group.payout) - implied, 6)
            diag = {
                "group_id": group.group_id, "group_type": group.group_type,
                "n_legs": len(group.legs),
                "exhaustive": bool(group.exhaustive),
                "mutually_exclusive": bool(group.mutually_exclusive),
                "settlement_consistent": bool(settlement_consistent),
                "divergence_gap": round(float(gap), 8),
                "profit_lower_bound": profit_lb,            # ALWAYS a float
                "projected_profit_lower_bound": profit_lb,  # alias (back-compat)
                "all_legs_priced": all_legs_priced,
                "implied_sum": round(implied, 6),
                "max_ambiguity_score": round(float(max_amb), 6),
                "max_ambiguity_threshold": float(self.max_ambiguity),
                "stale_book_score": round(float(stale_sc), 6),
                "rejection_stage": stage, "failure_mode": reason,
                "rejection_reason": reason,
            }
            logger.debug("bregman reject: group=%s stage=%s reason=%s exhaustive=%s "
                         "settlement_consistent=%s gap=%.6g profit_lb=%.6f",
                         group.group_id, stage, reason, group.exhaustive,
                         settlement_consistent, gap, profit_lb)
            return CertifiedBregmanOpportunity(
                group_id=group.group_id, group_type=group.group_type, legs=[],
                executable_prices=[], quantities=[], required_capital=0.0,
                worst_case_pnl=0.0, profit_lower_bound=0.0, divergence_gap=gap,
                divergence_method=method, failure_modes=failures,
                fill_feasibility=0.0, persistence_score=0.0,
                no_trade_reason=reason, certified=False, risk_free=False,
                certificate=_certificate(certified=False, risk_free=False),
                rejection_stage=stage, certify_diagnostics=diag)

        ok, why = validate_simplex(group)
        if not ok:
            return reject("invalid_simplex" if why.startswith(
                ("insufficient", "duplicate", "unknown", "non_positive"))
                else why)

        # --- per-leg feasibility + cost (conservative: rounds against us) ---
        from engine.execution.slippage import drag_breakdown
        cost_per_set = 0.0
        exec_prices: list[float] = []
        depth_qty: list[float] = []
        spreads: list[float] = []
        # cost-drag accumulators (per set): fee / spread / slippage / tick-rounding
        drag = {"fee": 0.0, "spread": 0.0, "slippage": 0.0, "tick": 0.0}
        for leg in group.legs:
            if leg.ask is None or leg.ask <= 0.0:
                failures.append("missing_leg")
                return reject("no_executable_price")
            if not leg.fresh_book or leg.stale:
                return reject("stale_book")
            if not getattr(leg, "accepting_orders", True):
                return reject("market_closed")
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
            # conservative executable price + cost-drag decomposition (tick-up,
            # slippage, fee — only ever WORSE than the touch).
            b = drag_breakdown(float(leg.ask), leg.bid, leg.tick_size,
                               slippage_bps=self.slippage_bps, fee_bps=self.taker_fee_bps)
            px = float(b["exec_price"])
            drag["tick"] += float(b["tick_rounding"])
            drag["slippage"] += float(b["slippage"])
            drag["fee"] += float(b["fee"])
            drag["spread"] += float(b["half_spread"])
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
        certified = (full_hedge and all_executable and worst_case_pnl > 0.0
                     and settlement_consistent)
        risk_free = certified and worst_case_pnl > 0.0 and full_hedge and all_executable

        # all-leg fill probability — ALWAYS computed for the certificate. With a
        # supplied fill model it is the modelled product; otherwise a deterministic
        # depth-headroom proxy (1.0 only when every leg has ample depth headroom).
        depth_suff = min((depth_usd_l / max(1e-9, sets * px)
                          for depth_usd_l, px in
                          ((l.depth_usd, p) for l, p in zip(group.legs, exec_prices))),
                         default=0.0)
        depth_suff = max(0.0, min(1.0, depth_suff))
        if fill_model is not None and group.legs:
            all_leg_fill = 1.0
            for leg, px in zip(group.legs, exec_prices):
                sp = leg.spread if leg.spread is not None else 0.0
                all_leg_fill *= fill_model.fill_probability(
                    spread=float(sp), depth_usd=float(leg.depth_usd),
                    order_usd=float(sets * px), aggressiveness=1.0,
                    stale=bool(leg.stale or not leg.fresh_book))
        else:
            # depth-headroom proxy: full confidence only when every leg can absorb
            # the order ~1.5x over at the touch (conservative, deterministic).
            all_leg_fill = 1.0
            for leg, px in zip(group.legs, exec_prices):
                order_usd = sets * px
                headroom = leg.depth_usd / max(1e-9, order_usd * 1.5)
                all_leg_fill *= max(0.0, min(1.0, headroom))

        # CLOB v2 fill-risk gate (PAPER realism): a full hedge is only risk-free
        # if EVERY leg can actually fill. If the all-leg fill probability is below
        # the floor, the hedge can break under partial fills, so it is NOT
        # risk-free. Conservative: only ever REMOVES the risk-free label.
        if fill_model is not None and group.legs and all_leg_fill < float(min_all_leg_fill_prob):
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
            cost_per_set=cost_per_set, sets=sets,
            certificate=_certificate(
                certified=certified, risk_free=risk_free, exec_prices=exec_prices,
                sets=sets, required_capital=required_capital,
                worst_case_pnl=worst_case_pnl, drags=drag, depth_suff=depth_suff,
                fill_prob=all_leg_fill, all_exec=all_executable),
            has_synthetic_leg=any(getattr(l, "synthetic_price", False) for l in group.legs),
            rejection_stage=(_STAGE_CERTIFIED if certified else _STAGE_EDGE),
            certify_diagnostics={
                "group_id": group.group_id, "group_type": group.group_type,
                "n_legs": len(group.legs), "exhaustive": bool(group.exhaustive),
                "mutually_exclusive": bool(group.mutually_exclusive),
                "settlement_consistent": bool(settlement_consistent),
                "divergence_gap": round(float(gap), 8),
                "profit_lower_bound": round(float(profit_lower_bound), 6),
                "projected_profit_lower_bound": round(float(profit_lower_bound), 6),
                "all_legs_priced": True,
                "worst_case_pnl": round(float(worst_case_pnl), 6),
                "cost_per_set": round(float(cost_per_set), 6),
                "rejection_stage": (_STAGE_CERTIFIED if certified else _STAGE_EDGE),
                "failure_mode": "" if certified else "not_certified",
                "rejection_reason": "" if certified else "not_certified"})
        logger.info("bregman certify group=%s certified=%s risk_free=%s "
                    "profit_lb=%.6f cost/set=%.4f sets=%.2f gap=%.6f stage=%s",
                    group.group_id, certified, risk_free, profit_lower_bound,
                    cost_per_set, sets, gap,
                    _STAGE_CERTIFIED if certified else _STAGE_EDGE)
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
    hedge_break_blocked: bool = False   # True when the hedge broke (block sizing)
    unwind_cost: float = 0.0            # cost to unwind the un-hedged partial legs
    hedge_gap_ms: float = 0.0           # total time to assemble all legs

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "total_legs": self.total_legs,
            "filled_legs": self.filled_legs, "fully_hedged": self.fully_hedged,
            "hedge_complete": self.hedge_complete, "failure_mode": self.failure_mode,
            "realized_cost": round(self.realized_cost, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "partial_fill_rate": round(self.partial_fill_rate, 6),
            "cancelled": self.cancelled, "timed_out": self.timed_out,
            "hedge_break_blocked": self.hedge_break_blocked,
            "unwind_cost": round(self.unwind_cost, 6),
            "hedge_gap_ms": round(self.hedge_gap_ms, 3),
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
                 now: Optional[float] = None, leg_order: str = "as_is",
                 max_hedge_gap_ms: Optional[int] = None,
                 unwind_cost_bps: float = 0.0) -> BundleExecutionResult:
        legs = list(opp.legs)
        n = len(legs)
        sets = float(opp.sets)
        cost_per_set = float(opp.cost_per_set)
        # group payout is $1 per share for a complete set (one outcome resolves YES)
        payout = 1.0

        # leg ordering policy: execute the least-liquid (hardest) leg first so the
        # bundle fails fast before committing capital, or most-liquid first. The
        # fraction/latency overrides are kept aligned to the chosen order.
        order_idx = list(range(n))
        if leg_order in ("liquidity_asc", "liquidity_desc") and n:
            order_idx.sort(key=lambda i: float(getattr(legs[i], "depth_usd", 0.0) or 0.0),
                           reverse=(leg_order == "liquidity_desc"))
            legs = [legs[i] for i in order_idx]
            if leg_fill_fractions is not None:
                leg_fill_fractions = [leg_fill_fractions[i] if i < len(leg_fill_fractions)
                                      else 1.0 for i in order_idx]
            if leg_latencies_ms is not None:
                leg_latencies_ms = [leg_latencies_ms[i] if i < len(leg_latencies_ms)
                                    else 0.0 for i in order_idx]

        leg_results: list = []
        cum_latency = 0.0
        timed_out = False
        hedge_gap_exceeded = False
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

        # max hedge gap: even if every leg eventually filled, a hedge that took
        # too long to assemble is exposed to mid moves between legs -> broken.
        if (max_hedge_gap_ms is not None and fully_filled == n
                and cum_latency > float(max_hedge_gap_ms)):
            hedge_gap_exceeded = True

        fully_hedged = (fully_filled == n) and not timed_out and not hedge_gap_exceeded
        # partial-fill unwind cost: liquidating the un-hedged filled legs costs
        # the spread/slippage to exit (only ever ADDS to the loss).
        unwind_cost = (round(realized_cost * max(0.0, float(unwind_cost_bps)) / 10000.0, 6)
                       if not fully_hedged else 0.0)
        if fully_hedged:
            failure_mode = ""
            realized_pnl = sets * (payout - cost_per_set)        # the certified profit
        elif timed_out:
            failure_mode = "timeout"
            realized_pnl = -realized_cost - unwind_cost
        elif hedge_gap_exceeded:
            failure_mode = "hedge_gap_exceeded"
            realized_pnl = -realized_cost - unwind_cost
        else:
            failure_mode = "partial_fill_breaks_hedge"
            realized_pnl = -realized_cost - unwind_cost
        partial_fill_rate = round(partial_or_failed / n, 6) if n else 0.0

        return BundleExecutionResult(
            group_id=opp.group_id, total_legs=n, filled_legs=fully_filled,
            fully_hedged=fully_hedged, hedge_complete=fully_hedged,
            failure_mode=failure_mode, realized_cost=round(realized_cost, 6),
            realized_pnl=round(realized_pnl, 6), partial_fill_rate=partial_fill_rate,
            cancelled=cancelled, timed_out=timed_out, leg_results=leg_results,
            hedge_break_blocked=not fully_hedged, unwind_cost=unwind_cost,
            hedge_gap_ms=round(cum_latency, 3))


def bregman_capital_priority(opp) -> dict:
    """Capital-priority verdict for a Bregman opportunity.

    A certified opportunity (``certified`` + positive ``profit_lower_bound``) is
    granted FIRST-priority capital and may pre-empt directional edge; anything
    else is downgraded to capital-priority ``none`` (log/audit only — never
    funded). This is the single capital gate that lets Bregman jump the queue,
    and ONLY when certification passes. Read-only — never sizes / places."""
    certified = bool(getattr(opp, "certified", False)) and bool(
        getattr(opp, "is_opportunity", False))
    profit_lb = float(getattr(opp, "profit_lower_bound", 0.0) or 0.0)
    funded = certified and profit_lb > 0.0
    from engine.training.capital_allocator import BUCKET_BREGMAN
    return {
        "bucket": BUCKET_BREGMAN if funded else "",
        "priority": 0 if funded else 999,        # 0 == highest priority
        "fundable": funded,
        "preempts_directional": funded,
        "certified": certified,
        "profit_lower_bound": round(profit_lb, 6),
        "reason": "certified" if funded else "not_certified",
    }
