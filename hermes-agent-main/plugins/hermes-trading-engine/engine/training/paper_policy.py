"""PaperPolicy — sizing + paper proposal construction (v2).

Edge evaluation is delegated to :class:`engine.training.edge_engine.EdgeEngine`
(single source of truth). Sizing is a deterministic function of config: fixed
paper notional by default, with an optional capped fractional-Kelly. Grok never
sizes, approves, or places anything.

Quant scope — *Signal Generation & Strategy Development* + *Risk Management*:
directional paper proposals are built here. Flagship Bregman-arbitrage hedge
legs are built + sized by the trainer using the same ``TradeProposal`` contract
and routed through the identical RiskEngine + paper broker (never bypassing
risk). PAPER ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .edge_engine import EdgeEngine, EdgeResult  # re-exported for back-compat
from .probability_stack import ProbabilityEstimate

__all__ = ["EdgeResult", "TradeProposal", "PaperPolicy"]


@dataclass
class TradeProposal:
    market_id: str
    asset_id: str
    outcome: str
    side: str
    price: float
    notional_usd: float
    qty: float
    p_final: float
    net_edge: float
    confidence: float
    research_source: str
    sizing_method: str
    kelly_size_usd: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class PaperPolicy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = EdgeEngine(cfg)

    def evaluate_edge(self, est: ProbabilityEstimate, rec, *,
                      outcome: str = "YES", open_event_groups: Optional[set] = None,
                      open_trades: int = 0) -> EdgeResult:
        return self.engine.evaluate(est, rec, outcome=outcome,
                                    open_event_groups=open_event_groups,
                                    open_trades=open_trades)

    def best_side(self, est: ProbabilityEstimate, rec, *,
                  open_event_groups: Optional[set] = None, open_trades: int = 0) -> EdgeResult:
        return self.engine.best_side(est, rec, open_event_groups=open_event_groups,
                                     open_trades=open_trades)

    # -- sizing --------------------------------------------------------------
    def kelly_size(self, edge: EdgeResult, *, calibrated_probability: Optional[float] = None
                   ) -> float:
        """Fractional-Kelly notional for a calibrated directional edge.

        Uses the CALIBRATED probability when available (falling back to
        ``edge.p_final``), and is hard-clamped to ``kelly_max_fraction`` of
        bankroll AND the ``max_kelly_size_usd`` paper ceiling — it can never
        exceed the hard paper caps. Diagnostics / optional sizing only.
        """
        from .portfolio import kelly_size_usd
        cfg = self.cfg
        price = edge.executable_price or 0.5
        p = calibrated_probability if calibrated_probability is not None else edge.p_final
        return round(kelly_size_usd(
            p, price, bankroll=float(cfg.starting_bankroll),
            kelly_fraction=float(cfg.kelly_fraction),
            max_fraction=float(getattr(cfg, "kelly_max_fraction", 0.05)),
            max_size_usd=float(cfg.max_kelly_size_usd)), 2)

    def size(self, edge: EdgeResult, *, calibrated_probability: Optional[float] = None) -> tuple:
        cfg = self.cfg
        kelly = (self.kelly_size(edge, calibrated_probability=calibrated_probability)
                 if cfg.use_kelly_for_diagnostics else 0.0)
        if bool(cfg.use_kelly_for_size):
            return min(kelly, float(cfg.max_kelly_size_usd)), "fractional_kelly", kelly
        return float(cfg.fixed_notional_usd), "fixed", kelly

    def fill_quality_estimate(self, edge: EdgeResult, est: ProbabilityEstimate, rec,
                              *, order_usd: Optional[float] = None) -> dict:
        """Forward CLOB v2 fill-quality estimate for a directional paper proposal
        (fill probability / partial-fill risk / slippage forecast). Read-only
        analytics that let the trainer prefer realistically-fillable trades; it
        never sizes or places an order."""
        from .execution_quality import (fill_probability, partial_fill_risk,
                                         slippage_forecast)
        spread = float(getattr(est, "spread", 0.0) or 0.0)
        depth = float(getattr(rec, "top_depth_usd", 0.0) or 0.0)
        notional = (order_usd if order_usd is not None
                    else float(getattr(self.cfg, "fixed_notional_usd", 0.0)))
        stale = not bool(getattr(est, "fresh_book", True))
        max_spread = float(getattr(self.cfg, "max_spread", 0.08))
        return {
            "fill_probability": fill_probability(spread, depth, notional, stale=stale,
                                                 max_spread=max_spread),
            "partial_fill_risk": partial_fill_risk(notional, depth),
            "slippage_forecast_bps": slippage_forecast(notional, depth),
        }

    def explore_size(self) -> float:
        """Small exploratory size for an active-learning paper trade, hard-clamped
        to the paper order-notional ceiling (can never bypass risk caps)."""
        cfg = self.cfg
        return round(min(float(getattr(cfg, "exploration_notional_usd", 2.0)),
                         float(cfg.max_order_notional_usd)), 2)

    def build_exploration_proposal(self, edge: EdgeResult, est: ProbabilityEstimate,
                                   rec) -> TradeProposal:
        """Build a small active-learning exploratory proposal for a near-miss.

        The candidate already passed EVERY hard gate (it is a near-miss, not a
        hard-gate rejection); this only sizes a tiny clamped paper trade and is
        still routed through the identical RiskEngine + paper broker. PAPER ONLY —
        never sizes for live, never bypasses risk."""
        notional = self.explore_size()
        price = edge.executable_price or est.p_market_mid
        qty = (notional / price) if price > 0 else 0.0
        asset_id = rec.clob_token_ids[0] if rec.clob_token_ids else rec.market_id
        return TradeProposal(
            market_id=est.market_id, asset_id=str(asset_id), outcome=edge.outcome,
            side="BUY", price=round(price, 4), notional_usd=round(notional, 2),
            qty=round(qty, 4), p_final=round(edge.p_final, 4),
            net_edge=round(edge.net_edge, 5), confidence=round(est.confidence, 3),
            research_source=est.research_source, sizing_method="active_learning_exploration",
            kelly_size_usd=0.0)

    def build_proposal(self, edge: EdgeResult, est: ProbabilityEstimate, rec) -> TradeProposal:
        notional, method, kelly = self.size(
            edge, calibrated_probability=getattr(est, "calibrated_probability", None))
        price = edge.executable_price or est.p_market_mid
        qty = (notional / price) if price > 0 else 0.0
        asset_id = rec.clob_token_ids[0] if rec.clob_token_ids else rec.market_id
        return TradeProposal(
            market_id=est.market_id, asset_id=str(asset_id), outcome=edge.outcome,
            side="BUY", price=round(price, 4), notional_usd=round(notional, 2),
            qty=round(qty, 4), p_final=round(edge.p_final, 4),
            net_edge=round(edge.net_edge, 5), confidence=round(est.confidence, 3),
            research_source=est.research_source, sizing_method=method,
            kelly_size_usd=kelly)
