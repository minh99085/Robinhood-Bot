"""Hierarchical signal resolver for the Polymarket PAPER engine.

Quant scope — *Signal Generation & Strategy Development* + *Bregman arbitrage
priority* + *Strategy Optimization & Robustness Testing* + *Risk Management*:

Resolves, in strict priority order, which strategy (if any) should act on a
candidate:

  1. **Certified Bregman arbitrage bundle** — a fully-hedged, all-leg-executable
     opportunity with a positive certified profit lower bound after all costs.
  2. **Calibrated statistical mispricing** — the executable price is mispriced
     versus our CALIBRATED / model estimate (model-driven, not a fresh
     directional research view).
  3. **Directional probability edge** — a directional (research/Grok-driven)
     predictive bet.

It computes per-signal scores (confidence, persistence, alpha decay,
edge-after-costs, uncertainty penalty, Chainlink relevance, opportunity quality),
an **alpha attribution** across the eight alpha sources, a **conflict-resolution**
record across disagreeing sources (Bregman, Chainlink, research, microstructure,
learner), and **no-trade diagnostics** for every rejected signal.

Compliance: this is an advisory SELECTION layer only. It carries NO order size /
notional / placement surface; research (Grok) can never override the
deterministic edge gate, set the trade side, or escalate a signal above the
priority it earns. Sizing + risk + execution remain with the trainer's
RiskEngine + paper broker. PAPER ONLY.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from .bregman_execution import CertifiedBregmanOpportunity
from .edge_engine import EdgeResult
from .probability_stack import ProbabilityEstimate

logger = logging.getLogger("hte.training.signal_resolver")

STRATEGY_PRIORITIES: dict[str, int] = {
    "bregman_arbitrage": 1,
    "statistical_mispricing": 2,
    "directional": 3,
    "none": 0,
}

# Alpha-attribution sources (Strategy Optimization & Live Monitoring).
ALPHA_SOURCES = (
    "bregman_divergence", "market_microstructure", "chainlink_oracle",
    "research_grok", "calibration", "learner_category", "liquidity",
    "execution_edge",
)

_EPS = 1e-9


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _liq_quality(liq: float) -> float:
    liq = max(0.0, float(liq or 0.0))
    if liq <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log1p(liq) / math.log1p(100_000.0)))


def _ttr_factor(time_to_resolution_s: Optional[float]) -> float:
    if time_to_resolution_s is None:
        return 1.0
    t = max(0.0, float(time_to_resolution_s))
    ref = 7 * 24 * 3600.0
    return max(0.0, min(1.0, math.log1p(t) / math.log1p(ref)))


@dataclass
class ResolvedSignal:
    """The resolved (or rejected) signal for one candidate — advisory only."""

    strategy: str
    priority: int
    should_trade: bool
    market_id: str
    outcome: str
    side: str
    executable_price: Optional[float]
    edge_after_costs: float
    confidence: float
    persistence: float
    alpha_decay: float
    uncertainty_penalty: float
    chainlink_relevance: float
    opportunity_quality: float
    no_trade_reason: str
    rejected_signals: list[dict] = field(default_factory=list)
    alpha_attribution: dict = field(default_factory=dict)
    conflict: dict = field(default_factory=dict)
    bregman: Optional[dict] = None
    grok_advisory_only: bool = True

    def to_dict(self) -> dict:
        d = {
            "strategy": self.strategy, "priority": self.priority,
            "should_trade": self.should_trade, "market_id": self.market_id,
            "outcome": self.outcome, "side": self.side,
            "executable_price": (round(self.executable_price, 6)
                                 if self.executable_price is not None else None),
            "edge_after_costs": round(self.edge_after_costs, 6),
            "confidence": round(self.confidence, 6),
            "persistence": round(self.persistence, 6),
            "alpha_decay": round(self.alpha_decay, 6),
            "uncertainty_penalty": round(self.uncertainty_penalty, 6),
            "chainlink_relevance": round(self.chainlink_relevance, 6),
            "opportunity_quality": round(self.opportunity_quality, 6),
            "no_trade_reason": self.no_trade_reason,
            "rejected_signals": list(self.rejected_signals),
            "alpha_attribution": {k: round(v, 6) for k, v in self.alpha_attribution.items()},
            "conflict": dict(self.conflict),
            "bregman": self.bregman,
            "grok_advisory_only": self.grok_advisory_only,
        }
        return d


class SignalResolver:
    """Resolve the highest-priority tradable signal for a candidate."""

    def __init__(self, cfg=None, *, max_spread: float = 0.08):
        self.cfg = cfg
        self.max_spread = float(getattr(cfg, "max_spread", max_spread))

    # -- attribution + votes -------------------------------------------------
    def _alpha_attribution(self, est: ProbabilityEstimate, edge: Optional[EdgeResult],
                           bregman_opp: Optional[CertifiedBregmanOpportunity]) -> dict:
        mid = float(est.p_market_mid)
        return {
            "bregman_divergence": float(bregman_opp.divergence_gap) if bregman_opp else 0.0,
            "market_microstructure": abs(float(edge.gross_edge)) if edge else 0.0,
            "chainlink_oracle": _clamp01(est.chainlink_confidence),
            "research_grok": abs(float(est.p_research) - mid) if est.research_usable else 0.0,
            "calibration": abs(float(est.calibrated_probability) - float(est.p_final)),
            "learner_category": abs(float(est.p_model) - mid),
            "liquidity": _liq_quality(est.liquidity_usd),
            "execution_edge": max(0.0, float(edge.net_edge)) if edge else 0.0,
        }

    def _votes(self, est: ProbabilityEstimate, edge: Optional[EdgeResult],
               bregman_opp: Optional[CertifiedBregmanOpportunity]) -> dict:
        mid = float(est.p_market_mid)

        def dir_of(p: float) -> str:
            if p > mid + 1e-4:
                return "buy"
            if p < mid - 1e-4:
                return "sell"
            return "flat"

        if getattr(est, "chainlink_no_trade", False):
            chainlink = "stale"
        elif _clamp01(est.chainlink_confidence) > 0.0:
            chainlink = "relevant"
        else:
            chainlink = "none"
        return {
            "bregman": "arb" if (bregman_opp is not None and bregman_opp.is_opportunity) else "none",
            "chainlink": chainlink,
            "research": dir_of(float(est.p_research)) if est.research_usable else "none",
            "microstructure": (edge.side.lower() if edge and edge.side else "none"),
            "learner": dir_of(float(est.p_model)),
        }

    @staticmethod
    def _disagreement(votes: dict) -> bool:
        directional = [votes[s] for s in ("research", "microstructure", "learner")
                       if votes[s] in ("buy", "sell")]
        return len(set(directional)) > 1

    def _classify(self, est: ProbabilityEstimate) -> tuple[str, int]:
        """Model/calibration-driven -> statistical (P2); research-driven -> directional (P3)."""
        mid = float(est.p_market_mid)
        research_c = abs(float(est.p_research) - mid) if est.research_usable else 0.0
        model_c = abs(float(est.p_model) - mid)
        if research_c > model_c and research_c > _EPS:
            return "directional", 3
        return "statistical_mispricing", 2

    # -- main ----------------------------------------------------------------
    def resolve(self, *, est: ProbabilityEstimate, edge: Optional[EdgeResult],
                bregman_opp: Optional[CertifiedBregmanOpportunity] = None,
                time_to_resolution_s: Optional[float] = None,
                feedback_adjustment: float = 1.0) -> ResolvedSignal:
        attribution = self._alpha_attribution(est, edge, bregman_opp)
        votes = self._votes(est, edge, bregman_opp)
        conflict = {"votes": votes, "disagreement": self._disagreement(votes),
                    "resolution": "no_trade"}
        rejected: list[dict] = []

        # shared scores
        stale = _clamp01(getattr(est, "stale_score", 0.0))
        alpha_decay = _clamp01(0.5 * stale + 0.5 * (1.0 - _ttr_factor(time_to_resolution_s)))
        chainlink_relevance = _clamp01(est.chainlink_confidence)
        uncertainty_total = est.uncertainty_components.get("total") if isinstance(
            est.uncertainty_components, dict) else None
        if uncertainty_total is None:
            uncertainty_total = edge.uncertainty_band if edge else 0.0
        uncertainty_penalty = _clamp01(uncertainty_total)

        bregman_won = bregman_opp is not None and bregman_opp.is_opportunity

        # ---- priority 1: certified Bregman arbitrage bundle ----
        if bregman_won:
            conflict["resolution"] = "bregman_priority"
            persistence = _clamp01(bregman_opp.persistence_score)
            ret = (bregman_opp.profit_lower_bound / bregman_opp.required_capital
                   if bregman_opp.required_capital > _EPS else 0.0)
            quality = _clamp01(0.5 + ret) * persistence
            logger.info("signal=bregman_arbitrage group=%s profit_lb=%.4f quality=%.4f",
                        bregman_opp.group_id, bregman_opp.profit_lower_bound, quality)
            return ResolvedSignal(
                strategy="bregman_arbitrage", priority=1, should_trade=True,
                market_id=est.market_id, outcome="BUNDLE", side="BUY",
                executable_price=(bregman_opp.legs[0].executable_price
                                  if bregman_opp.legs else None),
                edge_after_costs=float(bregman_opp.profit_lower_bound),
                confidence=1.0, persistence=persistence, alpha_decay=alpha_decay,
                uncertainty_penalty=0.0, chainlink_relevance=chainlink_relevance,
                opportunity_quality=quality, no_trade_reason="",
                rejected_signals=rejected, alpha_attribution=attribution,
                conflict=conflict, bregman=bregman_opp.to_dict())

        if bregman_opp is not None:
            rejected.append({"strategy": "bregman_arbitrage",
                             "reason": bregman_opp.no_trade_reason or "no_bregman_opportunity"})
        else:
            rejected.append({"strategy": "bregman_arbitrage", "reason": "no_bregman_group"})

        # ---- priority 2/3: edge-gated statistical or directional ----
        persistence = _clamp01(1.0 - float(est.spread) / max(1e-6, self.max_spread))
        persistence *= 1.0 if est.fresh_book else 0.5
        persistence = _clamp01(persistence)

        if edge is not None and edge.should_trade:
            strategy, priority = self._classify(est)
            conflict["resolution"] = strategy
            edge_after = float(edge.net_edge)
            quality = _clamp01(max(0.0, edge_after) * 10.0) * est_confidence(est) \
                * persistence * (1.0 - uncertainty_penalty) * (1.0 - alpha_decay)
            other = "directional" if strategy == "statistical_mispricing" else "statistical_mispricing"
            rejected.append({"strategy": other, "reason": f"lower_priority_than_{strategy}"})
            logger.info("signal=%s priority=%d net_edge=%.4f quality=%.4f",
                        strategy, priority, edge_after, quality)
            return ResolvedSignal(
                strategy=strategy, priority=priority, should_trade=True,
                market_id=est.market_id, outcome=edge.outcome, side=edge.side,
                executable_price=edge.executable_price, edge_after_costs=edge_after,
                confidence=est_confidence(est), persistence=persistence,
                alpha_decay=alpha_decay, uncertainty_penalty=uncertainty_penalty,
                chainlink_relevance=chainlink_relevance, opportunity_quality=quality,
                no_trade_reason="", rejected_signals=rejected,
                alpha_attribution=attribution, conflict=conflict)

        # ---- no tradable signal ----
        reason = (edge.reason if edge is not None else "no_edge")
        rejected.append({"strategy": "statistical_mispricing", "reason": reason})
        rejected.append({"strategy": "directional", "reason": reason})
        logger.debug("signal=none reason=%s market=%s", reason, est.market_id)
        return ResolvedSignal(
            strategy="none", priority=0, should_trade=False, market_id=est.market_id,
            outcome=(edge.outcome if edge else "YES"),
            side=(edge.side if edge else "BUY"),
            executable_price=(edge.executable_price if edge else None),
            edge_after_costs=(float(edge.net_edge) if edge else 0.0),
            confidence=est_confidence(est), persistence=persistence,
            alpha_decay=alpha_decay, uncertainty_penalty=uncertainty_penalty,
            chainlink_relevance=chainlink_relevance, opportunity_quality=0.0,
            no_trade_reason=reason, rejected_signals=rejected,
            alpha_attribution=attribution, conflict=conflict)


def est_confidence(est: ProbabilityEstimate) -> float:
    """Signal confidence: research/model confidence, clamped to [0, 1]."""
    return _clamp01(getattr(est, "confidence", 0.5))


def rank_signals(signals: list[ResolvedSignal]) -> list[ResolvedSignal]:
    """Sort tradable signals by priority (1 best) then opportunity quality (desc).

    Bregman (priority 1) therefore outranks statistical (2) and directional (3);
    within a tier, higher opportunity quality wins. Non-tradable signals are
    dropped from the ranking."""
    tradable = [s for s in signals if s.should_trade]
    tradable.sort(key=lambda s: (s.priority, -s.opportunity_quality))
    return tradable


def select_best(signals: list[ResolvedSignal]) -> Optional[ResolvedSignal]:
    ranked = rank_signals(signals)
    return ranked[0] if ranked else None
