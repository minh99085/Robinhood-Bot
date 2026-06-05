"""Tiered strategy router (PAPER ONLY, pure, deterministic).

Selects which strategy may act, with a strict priority order:

* **Tier 1 — certified Bregman arbitrage** (always outranks everything else).
* **Tier 2 — stale-crypto / Chainlink-fast BTC dislocation** (BTC Pulse lane),
  blocked when the regime is unknown/chop, after-cost EV is negative, or fill
  realism is weak.
* **Tier 3 — calibrated model edge** (probability ensemble vs market).
* **Tier 4 — exploration-only tiny paper trades** (last resort; never counts as
  validation evidence).

Adds online **threshold learning** (EV cutoffs that tighten after losses), per-tier
**dynamic EV cutoffs**, and **aggressive bad-fill rejection**. News/Grok never
select a trade here — they only adjust evidence weighting upstream.

Pure planner: no I/O, no trading, no wallet/order path. The deterministic
RiskEngine + paper OMS remain the only execution path.

Quant responsibilities
----------------------
* **Quant analyst** — defines tier eligibility + relationship universe.
* **Quant researcher** — sets EV cutoffs, threshold-learning dynamics, validates.
* **Quant developer** — owns this router + signal builders (typed, tested).
* **Trader** — acts only on the router's selected, fill-feasible signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional, Sequence

logger = logging.getLogger("hte.strategies.router")


class Tier(IntEnum):
    BREGMAN = 1            # certified coherence arbitrage
    DISLOCATION = 2       # stale crypto / Chainlink-fast BTC dislocation
    MODEL_EDGE = 3        # calibrated model edge
    EXPLORATION = 4       # exploration-only tiny


@dataclass
class StrategySignal:
    """A candidate action proposed by a strategy lane."""

    tier: Tier
    source: str
    edge: float = 0.0           # after-fee edge / expectancy (per unit)
    size: float = 0.0
    fill_ok: bool = True        # passed realistic-fill / depth checks
    certified: bool = False     # Tier-1 requires a deterministic certificate
    is_exploration: bool = False
    reason: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["tier"] = int(self.tier)
        return d


@dataclass
class RouterConfig:
    """Per-tier EV cutoffs + dynamics (explicit for sweeps)."""

    tier1_min_edge: float = 0.0          # certificate already guarantees > floor
    tier2_min_edge: float = 0.01         # base dislocation EV cutoff
    tier3_min_edge: float = 0.02         # base model-edge EV cutoff
    exploration_enabled: bool = True
    # threshold learning (applied to tier2/tier3 cutoffs)
    cutoff_loss_step: float = 0.005      # raise cutoff after a losing trade
    cutoff_win_relax: float = 0.001      # relax toward base after a win
    cutoff_max_extra: float = 0.05       # cap on learned tightening


class ThresholdLearner:
    """Online EV-cutoff tightener: more selective after losses, relaxes on wins.

    ``extra`` is added on top of the static per-tier cutoff and is bounded to
    ``[0, cutoff_max_extra]``. Deterministic given the realized-PnL sequence.
    """

    def __init__(self, config: RouterConfig):
        self.cfg = config
        self.extra: float = 0.0
        self.wins: int = 0
        self.losses: int = 0

    def update(self, realized_pnl: float) -> None:
        try:
            pnl = float(realized_pnl)
        except (TypeError, ValueError):
            return
        if pnl < 0:
            self.losses += 1
            self.extra = min(self.cfg.cutoff_max_extra, self.extra + self.cfg.cutoff_loss_step)
        elif pnl > 0:
            self.wins += 1
            self.extra = max(0.0, self.extra - self.cfg.cutoff_win_relax)

    def cutoff(self, base: float) -> float:
        return round(base + self.extra, 8)


@dataclass
class RouterDecision:
    selected: Optional[StrategySignal]
    tier: Optional[int]
    ranked: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "tier": self.tier,
            "ranked": [s.to_dict() for s in self.ranked],
            "rejected": [s.to_dict() for s in self.rejected],
            "reasons": list(self.reasons),
        }


class StrategyRouter:
    """Routes to the highest-priority eligible strategy signal (pure)."""

    def __init__(self, config: Optional[RouterConfig] = None,
                 threshold_learner: Optional[ThresholdLearner] = None):
        self.cfg = config or RouterConfig()
        self.learner = threshold_learner or ThresholdLearner(self.cfg)

    # -- signal builders -----------------------------------------------------
    @staticmethod
    def bregman_signal(opportunity: Any) -> StrategySignal:
        """Build a Tier-1 signal from a certified BregmanOpportunity."""
        cert = getattr(opportunity, "certificate", None)
        size = float(getattr(cert, "size", 0.0)) if cert is not None else 0.0
        fill = bool(getattr(cert, "fill_feasible", False)) if cert is not None else False
        certified = bool(getattr(cert, "certified", False)) if cert is not None else False
        return StrategySignal(
            tier=Tier.BREGMAN, source="bregman",
            edge=float(getattr(opportunity, "edge", 0.0)), size=size,
            fill_ok=fill, certified=certified,
            reason="certified" if certified else "uncertified",
            meta={"outcome_ids": list(getattr(opportunity, "outcome_ids", []))})

    @staticmethod
    def dislocation_signal(*, edge: float, size: float, fill_ok: bool,
                           regime: Optional[str] = None,
                           block_reason: Optional[str] = None) -> StrategySignal:
        return StrategySignal(
            tier=Tier.DISLOCATION, source="btc_pulse", edge=float(edge), size=float(size),
            fill_ok=bool(fill_ok), reason=block_reason or "ok",
            meta={"regime": regime, "block_reason": block_reason})

    @staticmethod
    def model_edge_signal(*, edge: float, size: float, fill_ok: bool,
                          source: str = "model") -> StrategySignal:
        return StrategySignal(tier=Tier.MODEL_EDGE, source=source, edge=float(edge),
                              size=float(size), fill_ok=bool(fill_ok))

    @staticmethod
    def exploration_signal(*, size: float, edge: float = 0.0) -> StrategySignal:
        return StrategySignal(tier=Tier.EXPLORATION, source="exploration", edge=float(edge),
                              size=float(size), fill_ok=True, is_exploration=True,
                              reason="exploration_only")

    # -- routing -------------------------------------------------------------
    def route(self, *, bregman: Optional[Sequence[Any]] = None,
              dislocation: Optional[StrategySignal] = None,
              model_edge: Optional[StrategySignal] = None,
              exploration: Optional[StrategySignal] = None) -> RouterDecision:
        """Select the highest-priority eligible signal. Tier 1 always wins."""
        ranked: list[StrategySignal] = []
        rejected: list[StrategySignal] = []
        reasons: list[str] = []

        # --- Tier 1: certified Bregman arbitrage (outranks everything) ---
        best_breg = None
        for opp in (bregman or []):
            sig = self.bregman_signal(opp) if not isinstance(opp, StrategySignal) else opp
            if sig.certified and sig.fill_ok and sig.edge >= self.cfg.tier1_min_edge and sig.size > 0:
                if best_breg is None or sig.edge > best_breg.edge:
                    best_breg = sig
            else:
                rejected.append(sig)
        if best_breg is not None:
            ranked.append(best_breg)
            reasons.append("tier1_bregman_certified")
            return RouterDecision(selected=best_breg, tier=int(Tier.BREGMAN),
                                  ranked=ranked, rejected=rejected, reasons=reasons)

        # --- Tier 2: stale-crypto / Chainlink-fast dislocation ---
        if dislocation is not None:
            cut = self.learner.cutoff(self.cfg.tier2_min_edge)
            blk = dislocation.meta.get("block_reason")
            if blk:
                rejected.append(dislocation)
                reasons.append(f"tier2_blocked:{blk}")
            elif not dislocation.fill_ok:
                rejected.append(dislocation)
                reasons.append("tier2_bad_fill")
            elif dislocation.edge < cut:
                rejected.append(dislocation)
                reasons.append(f"tier2_below_cutoff({cut})")
            else:
                ranked.append(dislocation)

        # --- Tier 3: calibrated model edge ---
        if model_edge is not None:
            cut = self.learner.cutoff(self.cfg.tier3_min_edge)
            if not model_edge.fill_ok:
                rejected.append(model_edge)
                reasons.append("tier3_bad_fill")
            elif model_edge.edge < cut:
                rejected.append(model_edge)
                reasons.append(f"tier3_below_cutoff({cut})")
            else:
                ranked.append(model_edge)

        # --- Tier 4: exploration-only tiny ---
        if exploration is not None and self.cfg.exploration_enabled:
            if exploration.fill_ok:
                ranked.append(exploration)
            else:
                rejected.append(exploration)
                reasons.append("tier4_bad_fill")

        ranked.sort(key=lambda s: int(s.tier))  # lowest tier number = highest priority
        selected = ranked[0] if ranked else None
        decision = RouterDecision(
            selected=selected, tier=int(selected.tier) if selected else None,
            ranked=ranked, rejected=rejected, reasons=reasons)
        if selected is not None:
            logger.info("router selected tier=%d source=%s edge=%.4f size=%.2f",
                        int(selected.tier), selected.source, selected.edge, selected.size)
        else:
            logger.debug("router selected nothing; reasons=%s", reasons)
        return decision
