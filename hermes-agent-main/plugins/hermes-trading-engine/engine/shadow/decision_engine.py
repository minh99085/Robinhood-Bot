"""ShadowDecisionEngine — candidate + book + research → zero/one TradeProposal.

Deterministic edge logic and FIXED config sizing. A Grok research estimate may
influence ``p_ensemble`` but it can NEVER set order size, side, or bypass the
RiskEngine. Every proposal is sent through the RiskEngine before it can become
an APPROVED_SHADOW decision.

Quant scope — *Signal Generation & Strategy Development* + *Live Trading &
Monitoring*: in the priority hierarchy (Bregman arbitrage P1 > calibrated
statistical mispricing P2 > directional predictive edge P3) shadow shipping is a
P3 directional research path. Bregman + statistical selection live in the
trainer's :mod:`engine.training.signal_resolver`; shadow keeps its conservative
research-only abstain-by-default contract.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from ..schemas import TradeProposal
from .config import ShadowConfig
from .schemas import ShadowDecision

ABSTAIN_REASONS = (
    "no_research_estimate", "stale_research_estimate", "low_evidence", "high_ambiguity",
    "insufficient_edge", "excessive_spread", "insufficient_liquidity", "close_too_near",
    "venue_degraded", "market_data_stale", "risk_rejected",
)


def _f(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ShadowDecisionEngine:
    def __init__(self, config: ShadowConfig):
        self.cfg = config

    def decide(self, candidate, *, best_bid=None, best_ask=None, spread=None, midpoint=None,
               research: Optional[dict] = None, risk_engine=None, risk_context=None,
               cycle_id: str = "c", now_ms: Optional[int] = None):
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        bb, ba = _f(best_bid), _f(best_ask)
        spr = _f(spread)
        mid = _f(midpoint) if midpoint is not None else (
            (bb + ba) / 2 if bb is not None and ba is not None else None)
        dec = ShadowDecision(
            shadow_session_id=getattr(candidate, "shadow_session_id", ""), ts_ms=now,
            cycle_id=cycle_id, venue=candidate.venue, market_id=candidate.market_id,
            market_ticker=candidate.market_ticker, asset_id=candidate.asset_id,
            outcome=candidate.outcome,
            p_market=mid, best_bid=_d(bb), best_ask=_d(ba), spread=_d(spr), midpoint=_d(mid))

        def abstain(reason: str):
            dec.decision = "ABSTAINED"
            dec.reason = reason
            return dec, None, None

        if not self.cfg.use_research or research is None:
            return abstain("no_research_estimate")
        if research.get("stale"):
            return abstain("stale_research_estimate")
        no_trade = research.get("no_trade_reason")
        p_ens = _f(research.get("p_ensemble"))
        dec.p_research = _f(research.get("p_llm_raw")) or _f(research.get("p_research"))
        dec.p_ensemble = p_ens
        dec.confidence = _f(research.get("confidence"))
        dec.evidence_score = _f(research.get("evidence_score"))
        dec.ambiguity_score = _f(research.get("ambiguity_score"))
        if p_ens is None:
            return abstain("no_research_estimate")
        if no_trade:
            return abstain("low_evidence" if "evidence" in str(no_trade) else
                           ("high_ambiguity" if "ambig" in str(no_trade) else "no_research_estimate"))
        if dec.evidence_score is not None and dec.evidence_score < self.cfg.min_evidence_score:
            return abstain("low_evidence")
        if dec.ambiguity_score is not None and dec.ambiguity_score > self.cfg.max_ambiguity_score:
            return abstain("high_ambiguity")
        if (self.cfg.require_bbo and (bb is None or ba is None)):
            return abstain("market_data_stale")
        if spr is not None and spr > self.cfg.max_spread:
            return abstain("excessive_spread")

        # deterministic edge: BUY if fair >> ask; SELL if fair << bid
        side = price = edge = None
        if ba is not None and (p_ens - ba) >= self.cfg.min_edge_after_costs:
            side, price, edge = "BUY", ba, p_ens - ba
        elif bb is not None and (bb - p_ens) >= self.cfg.min_edge_after_costs:
            side, price, edge = "SELL", bb, bb - p_ens
        else:
            return abstain("insufficient_edge")

        # FIXED sizing from config (never from Grok)
        notional = self.cfg.default_notional_usd
        if notional > self.cfg.max_order_notional_usd:
            notional = self.cfg.max_order_notional_usd
        qty = (notional / Decimal(str(price))) if price else Decimal(0)

        dec.decision = "PROPOSED"
        dec.intended_side = side
        dec.intended_limit_price = _d(price)
        dec.intended_notional = notional
        dec.edge_after_costs = edge
        dec.reason = "proposed"

        proposal = TradeProposal(
            strategy="shadow", market="polymarket", symbol=(candidate.market_id
                                                            or candidate.market_ticker or ""),
            side=side, notional=float(notional), price=float(price),
            edge_after_costs=float(edge), spread=float(spr or 0.0),
            ambiguity_score=float(dec.ambiguity_score or 0.0), data_age_s=0.0,
            allow_duplicate=False, mode="shadow",
            rationale=f"shadow edge={round(edge,4)} p_ens={p_ens}",
            meta={"venue": candidate.venue, "outcome": candidate.outcome,
                  "quantity": float(qty), "asset_id": candidate.asset_id})
        dec.proposal_id = proposal.proposal_id

        if risk_engine is None or risk_context is None:
            return dec, proposal, None
        rd = risk_engine.evaluate(proposal, risk_context)
        dec.risk_decision_id = proposal.proposal_id
        if getattr(rd, "approved", False):
            dec.decision = "APPROVED_SHADOW"
            dec.reason = "approved_shadow"
        else:
            dec.decision = "RISK_REJECTED"
            dec.reason = getattr(rd, "code", "risk_rejected")
        return dec, proposal, rd


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
