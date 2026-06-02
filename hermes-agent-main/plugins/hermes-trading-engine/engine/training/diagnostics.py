"""Edge-evaluation diagnostics record (one per evaluated candidate).

Captures the full decision context (probabilities, edge, costs, uncertainty,
decision + no-trade reason) so every trade and every *skipped* trade is
auditable and learnable. PAPER ONLY — purely a record, never an action.

Quant scope — *Strategy Optimization & Robustness Testing* + *Live Trading &
Monitoring*: extended with the hierarchical signal-resolver context (chosen
strategy + priority, opportunity-quality / decay / uncertainty scores, the alpha
attribution across all sources, the conflict-resolution record, and the
per-rejected-signal no-trade diagnostics) so each decision's alpha is fully
attributable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DiagnosticsRecord:
    diagnostics_id: str
    ts_ms: int
    market_id: str
    asset_id: str
    outcome: str
    side: str
    p_market: Optional[float]
    p_model: Optional[float]
    p_research: Optional[float]
    p_raw: Optional[float]
    p_final: Optional[float]
    shrink_factor: Optional[float]
    executable_price: Optional[float]
    spread: Optional[float]
    depth: Optional[float]
    gross_edge: Optional[float]
    net_edge: Optional[float]
    uncertainty_band: Optional[float]
    decision: str
    no_trade_reason: Optional[str]
    probability_source: str
    # ---- hierarchical signal-resolver context (backward-compatible) ----
    strategy: str = "directional"
    priority: int = 3
    opportunity_quality: float = 0.0
    persistence: float = 0.0
    alpha_decay: float = 0.0
    uncertainty_penalty: float = 0.0
    chainlink_relevance: float = 0.0
    alpha_attribution: dict = field(default_factory=dict)
    conflict: dict = field(default_factory=dict)
    rejected_signals: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


def new_diagnostics_id() -> str:
    return f"diag-{uuid.uuid4().hex[:14]}"


def build_record(*, ts_ms: int, est, edge, rec, resolved=None) -> DiagnosticsRecord:
    """Build a diagnostics record. When a :class:`ResolvedSignal` is supplied the
    record carries the resolver's strategy/priority, scores, alpha attribution,
    conflict record, and per-rejected-signal no-trade diagnostics."""
    decision = "trade" if edge.should_trade else "no_trade"
    extra: dict = {}
    if resolved is not None:
        extra = dict(
            strategy=resolved.strategy, priority=resolved.priority,
            opportunity_quality=resolved.opportunity_quality,
            persistence=resolved.persistence, alpha_decay=resolved.alpha_decay,
            uncertainty_penalty=resolved.uncertainty_penalty,
            chainlink_relevance=resolved.chainlink_relevance,
            alpha_attribution=dict(resolved.alpha_attribution),
            conflict=dict(resolved.conflict),
            rejected_signals=list(resolved.rejected_signals))
    return DiagnosticsRecord(
        diagnostics_id=new_diagnostics_id(), ts_ms=ts_ms, market_id=est.market_id,
        asset_id=(rec.clob_token_ids[0] if rec.clob_token_ids else rec.market_id),
        outcome=edge.outcome, side=edge.side, p_market=est.p_market_mid,
        p_model=est.p_model, p_research=est.p_research, p_raw=est.p_raw,
        p_final=edge.p_final, shrink_factor=est.shrink,
        executable_price=edge.executable_price, spread=est.spread,
        depth=rec.top_depth_usd, gross_edge=edge.gross_edge, net_edge=edge.net_edge,
        uncertainty_band=edge.uncertainty_band, decision=decision,
        no_trade_reason=(None if edge.should_trade else edge.reason),
        probability_source=est.research_source,
        payload={"cost_components": edge.cost_components,
                 "band_components": edge.band_components,
                 "research_usable": est.research_usable,
                 "model_has_edge": est.model_has_edge,
                 "category": rec.category},
        **extra)
