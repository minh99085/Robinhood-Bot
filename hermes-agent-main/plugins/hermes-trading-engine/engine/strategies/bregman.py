"""Bregman coherence arbitrage strategy (PRIMARY strategy, PAPER ONLY, pure).

Pipeline:

1. Compile the :class:`ConstraintGraph` to projection primitives.
2. KL/Bregman-project the market-implied probabilities onto the coherent set.
3. Flag groups whose local incoherence exceeds a threshold (candidates).
4. Certify each candidate with a cost/depth-aware worst-case certificate.
5. Emit ONLY certified, fill-feasible opportunities as tradeable. A candidate
   that is incoherent but fails certification is a *false positive*.

Tracks: candidates, certified count, certified profit, false positives, fill
feasibility, and opportunity decay (edge decays with age). Calibrated
probabilities (from the modeling layer) may *rank* opportunities, but a trade
requires the deterministic certificate ("no certified proof means no trade").

Quant responsibilities
----------------------
* **Quant analyst** — curates the constraint universe / relationships.
* **Quant researcher** — sets incoherence/edge thresholds, validates certificates.
* **Quant developer** — owns this module + graph/projection/certificate code.
* **Trader** — executes only ``tradeable()`` (certified + fill-feasible) output;
  monitors opportunity decay; never trades an uncertified candidate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..arbitrage.bregman_projection import (ProjectionResult, bregman_project,
                                            incoherence, local_incoherence)
from ..arbitrage.candidate import generate_candidates
from ..arbitrage.certificate import Certificate, FeeModel, certify_group
from ..arbitrage.constraint_graph import Constraint, ConstraintGraph

logger = logging.getLogger("hte.strategies.bregman")


@dataclass
class BregmanOpportunity:
    """A candidate coherence-arbitrage opportunity (certified or not)."""

    relation: str
    outcome_ids: list[str]
    local_incoherence: float
    certificate: Certificate
    created_ts: float
    edge: float = 0.0           # after-fee profit per set (0 if not certified)

    @property
    def tradeable(self) -> bool:
        """Tradeable iff certified AND fill-feasible (theoretical proof)."""
        return bool(self.certificate.certified) and bool(self.certificate.fill_feasible)

    @property
    def executable(self) -> bool:
        """EXECUTABLE iff the certificate is after-cost executable (the trade gate)."""
        return bool(getattr(self.certificate, "executable", False))

    def decayed_edge(self, now: Optional[float] = None, half_life_s: float = 300.0) -> float:
        """Edge decayed by age (opportunity decay): edge * 0.5**(age/half_life)."""
        if half_life_s <= 0:
            return self.edge
        now = time.time() if now is None else now
        age = max(0.0, now - self.created_ts)
        return round(self.edge * (0.5 ** (age / half_life_s)), 8)

    def to_dict(self) -> dict:
        return {"relation": self.relation, "outcome_ids": self.outcome_ids,
                "local_incoherence": self.local_incoherence, "edge": self.edge,
                "tradeable": self.tradeable, "certificate": self.certificate.to_dict(),
                "created_ts": self.created_ts}


@dataclass
class BregmanResult:
    candidates: int
    certified: int
    certified_profit: float
    false_positives: int
    fill_feasible: int
    opportunities: list = field(default_factory=list)
    projection: Optional[ProjectionResult] = None
    incoherence: dict = field(default_factory=dict)
    candidate_bundles: list = field(default_factory=list)  # CandidateBundle telemetry

    def tradeable(self) -> list:
        """Certified + fill-feasible (theoretical) opportunities only."""
        return [o for o in self.opportunities if o.tradeable]

    def executable(self) -> list:
        """EXECUTABLE_AFTER_COST_CERTIFIED opportunities only (the trade gate)."""
        return [o for o in self.opportunities if o.executable]

    def audit_diagnostics(self, *, half_life_s: float = 300.0) -> dict:
        """Decision-grade Bregman diagnostics for the Algorithmic Edge Audit (pure).

        Reports constraint groups scanned, incoherent groups, candidate vs
        certified arbitrages, executable-depth-certified count, the count rejected
        for fee/spread/depth/slippage reasons, expected minimum profit, worst-case
        payoff, execution atomicity risk, and the opportunity-decay half-life.
        """
        scanned = len(self.opportunities)
        incoherent = sum(1 for o in self.opportunities
                         if float(o.local_incoherence) > 0.0)
        exec_certified = sum(1 for o in self.opportunities
                             if o.certificate.certified and o.certificate.executable_depth_ok)
        executable_after_cost = sum(1 for o in self.opportunities if o.executable)
        status_counts: dict = {}
        for o in self.opportunities:
            st = getattr(o.certificate, "status", "") or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1
        reject_reasons: dict = {}
        for o in self.opportunities:
            if not o.certificate.certified and float(o.local_incoherence) > 0.0:
                reject_reasons[o.certificate.reason] = \
                    reject_reasons.get(o.certificate.reason, 0) + 1
        certified_profits = [o.certificate.after_fee_profit_per_set
                             for o in self.opportunities if o.certificate.certified]
        worst_payoffs = [o.certificate.worst_case_payoff_per_set
                         for o in self.opportunities if o.certificate.certified]
        atomicity_risk = any(len(o.outcome_ids) > 1
                             for o in self.opportunities if o.certificate.certified)
        return {
            "constraint_groups_scanned": scanned,
            "incoherent_groups": incoherent,
            "candidate_arbitrages": self.candidates,
            "certified_arbitrages": self.certified,
            "executable_depth_certified": exec_certified,
            "executable_after_cost_certified_arbitrages": executable_after_cost,
            "certificate_status_counts": status_counts,
            "fantasy_fills_rejected": sum(
                1 for o in self.opportunities if getattr(o.certificate, "fantasy_fill", False)),
            "rejected_fees_spread_depth_slippage": sum(reject_reasons.values()),
            "rejection_reasons": reject_reasons,
            "expected_min_profit": round(min(certified_profits), 6) if certified_profits else 0.0,
            "worst_case_payoff": round(min(worst_payoffs), 6) if worst_payoffs else 0.0,
            "execution_atomicity_risk": bool(atomicity_risk),
            "opportunity_decay_half_life_s": float(half_life_s),
        }

    def to_dict(self) -> dict:
        return {
            "candidates": self.candidates, "certified": self.certified,
            "certified_profit": self.certified_profit,
            "false_positives": self.false_positives,
            "fill_feasible": self.fill_feasible,
            "incoherence": dict(self.incoherence),
            "projection": self.projection.to_dict() if self.projection else None,
            "opportunities": [o.to_dict() for o in self.opportunities],
            "candidate_bundles": [b.to_dict() for b in self.candidate_bundles],
        }


class BregmanStrategy:
    """Primary coherence-arbitrage strategy (pure planner)."""

    def __init__(self, *, fee_model: Optional[FeeModel] = None,
                 profit_floor: float = 0.005, max_size: float = 1e9,
                 incoherence_tol: float = 1e-3, decay_half_life_s: float = 300.0):
        self.fee_model = fee_model or FeeModel()
        self.profit_floor = float(profit_floor)
        self.max_size = float(max_size)
        self.incoherence_tol = float(incoherence_tol)
        self.decay_half_life_s = float(decay_half_life_s)

    def evaluate(self, graph: ConstraintGraph, *, now: Optional[float] = None) -> BregmanResult:
        """Run project -> detect -> certify and return the result with metrics."""
        now = time.time() if now is None else now
        issues = graph.validate()
        if issues:
            logger.warning("constraint graph issues: %s", issues)

        prims = graph.to_primitives()
        x_market = graph.price_vector()
        proj = bregman_project(x_market, prims)
        incoh = incoherence(x_market, proj.x)

        opportunities: list[BregmanOpportunity] = []
        candidates = certified = false_positives = fill_feasible = 0
        certified_profit = 0.0

        # Evaluate ALL constraints: certify_group returns not-certified (with a
        # reason) for non-buy-set-arb structures, so incoherent-but-uncertifiable
        # groups (e.g. an overpriced mutually-exclusive set) are counted as
        # false positives rather than silently ignored.
        for c in graph.constraints():
            local = local_incoherence(x_market, proj.x, c.outcome_ids)
            cert = certify_group(graph, c, fee_model=self.fee_model,
                                 profit_floor=self.profit_floor, max_size=self.max_size)
            is_candidate = local > self.incoherence_tol or cert.certified
            opp = BregmanOpportunity(
                relation=c.type.value, outcome_ids=list(c.outcome_ids),
                local_incoherence=local, certificate=cert, created_ts=now,
                edge=cert.after_fee_profit_per_set if cert.certified else 0.0)
            opportunities.append(opp)
            if is_candidate:
                candidates += 1
            if cert.certified:
                certified += 1
                certified_profit += cert.total_after_fee_profit
                if cert.fill_feasible:
                    fill_feasible += 1
            elif local > self.incoherence_tol:
                # Looked mispriced but no executable certificate => false positive.
                false_positives += 1

        candidate_bundles = generate_candidates(
            graph, fee_model=self.fee_model, profit_floor=self.profit_floor,
            max_size=self.max_size)
        result = BregmanResult(
            candidates=candidates, certified=certified,
            certified_profit=round(certified_profit, 6),
            false_positives=false_positives, fill_feasible=fill_feasible,
            opportunities=opportunities, projection=proj, incoherence=incoh,
            candidate_bundles=candidate_bundles)
        logger.info("bregman eval: candidates=%d certified=%d profit=%.4f "
                    "false_positives=%d fill_feasible=%d max_violation=%.4g",
                    candidates, certified, result.certified_profit,
                    false_positives, fill_feasible, proj.max_violation)
        return result

    def tradeable(self, result: BregmanResult, *, now: Optional[float] = None,
                  min_decayed_edge: float = 0.0) -> list:
        """Return certified + fill-feasible opportunities whose decayed edge still
        clears ``min_decayed_edge``. Enforces 'no certified proof => no trade'."""
        out = []
        for o in result.tradeable():
            if o.decayed_edge(now=now, half_life_s=self.decay_half_life_s) >= min_decayed_edge:
                out.append(o)
        return out

    def plan_execution(self, opportunity: BregmanOpportunity, books: dict, *,
                       decision_ts_ms: int, sets: Optional[float] = None,
                       planner=None):
        """Build a PAPER-ONLY CLOB v2 execution plan for one opportunity.

        ``books`` maps each leg ``outcome_id`` to an
        :class:`engine.simulation.fill_model.OrderBook` snapshot. The plan marks
        ``executable`` only when the opportunity is certified AND the multi-leg
        fill can be guaranteed atomically risk-free; otherwise the certified
        opportunity is logged but not executable. Pure (delegates to the planner).
        """
        from engine.execution.clob_v2 import ClobV2ExecutionPlanner, ExecLeg
        from engine.simulation.fill_model import OrderBook

        planner = planner or ClobV2ExecutionPlanner()
        cert = opportunity.certificate
        n_sets = float(sets) if sets is not None else float(cert.size or 1.0)
        legs = []
        for oid in opportunity.outcome_ids:
            book = books.get(oid) if isinstance(books, dict) else None
            if book is None:
                book = OrderBook(ts_ms=decision_ts_ms, asks=[], bids=[])
            legs.append(ExecLeg(id=oid, book=book, side="buy", size=n_sets))
        plan = planner.plan(
            legs, decision_ts_ms=decision_ts_ms, sets=n_sets,
            worst_case_payoff_per_set=float(cert.worst_case_payoff_per_set or 1.0),
            certified=bool(cert.certified and cert.fill_feasible))
        if plan.certified and not plan.executable:
            logger.info("bregman opportunity certified but NOT executable (logged only): "
                        "legs=%s reason=%s", opportunity.outcome_ids, plan.reason)
        return plan
