"""GS Quant-style structured decision lifecycle records for the BTC pulse (Hermes-native).

These are small, auditable dataclasses (no gs-quant import, no external code) that give every
candidate a complete, reconcilable lifecycle:

    created -> feature_scored -> execution_costed -> accepted|rejected -> ledgered -> reported

They WRAP the existing flow (market data → signal → execution gate → paper fill → ledger);
they add structure/auditability only and never change decision logic. The execution-quality
gate remains the sole authority on whether a paper trade happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def ttc_bucket(ttc_s: Optional[float]) -> str:
    if ttc_s is None:
        return "na"
    if ttc_s < 60:
        return "<60s"
    if ttc_s < 120:
        return "60-120s"
    if ttc_s < 240:
        return "120-240s"
    return ">=240s"


def half_life_bucket(hl_s: Optional[float]) -> str:
    if hl_s is None:
        return "na"
    if hl_s < 30:
        return "<30s"
    if hl_s < 120:
        return "30-120s"
    return ">=120s"


@dataclass
class MarketContext:
    """Everything known about the market at the moment a candidate is created."""
    event_id: str
    market_id: str
    title: str
    asset: str = "BTC"
    open_ts: Optional[float] = None
    close_ts: Optional[float] = None
    ttc_s: Optional[float] = None
    oracle_source: str = "rtds_chainlink"
    s_open: Optional[float] = None
    s_now: Optional[float] = None
    sigma_per_sec: Optional[float] = None
    poly_yes: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    ask_depth_usd: Optional[float] = None
    lead_prices: dict = field(default_factory=dict)

    @property
    def ttc_bucket(self) -> str:
        return ttc_bucket(self.ttc_s)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "market_id": self.market_id, "title": self.title,
                "asset": self.asset, "ttc_s": (round(self.ttc_s, 1) if self.ttc_s is not None else None),
                "ttc_bucket": self.ttc_bucket, "oracle_source": self.oracle_source,
                "s_open": self.s_open, "s_now": self.s_now,
                "sigma_per_sec": self.sigma_per_sec, "poly_yes": self.poly_yes,
                "best_bid": self.best_bid, "best_ask": self.best_ask, "spread": self.spread,
                "ask_depth_usd": self.ask_depth_usd, "lead_prices": dict(self.lead_prices)}


@dataclass
class FeatureSnapshot:
    """Observe-only EP Chan feature view attached to a candidate (never trades)."""
    observe_only: bool = True
    hurst_regime: str = "insufficient_data"
    zscore_bucket: str = "na"
    half_life_s: Optional[float] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"observe_only": True, "hurst_regime": self.hurst_regime,
                "zscore_bucket": self.zscore_bucket, "half_life_s": self.half_life_s,
                **({} if not self.raw else {"raw": self.raw})}


@dataclass
class RegimeSnapshot:
    """Observe-only short-term regime view (populated by the Markov machine in a later phase)."""
    state: str = "unknown"
    probs: dict = field(default_factory=dict)
    observe_only: bool = True

    def to_dict(self) -> dict:
        return {"state": self.state, "probs": dict(self.probs), "observe_only": True}


@dataclass
class LearningRecord:
    """One settled-outcome learning row (feeds the Phase-10 learning loop)."""
    window_key: str
    terminal: str
    accepted: bool
    regime: Optional[str] = None
    zscore_bucket: Optional[str] = None
    pnl_usd: Optional[float] = None
    won: Optional[bool] = None

    def to_dict(self) -> dict:
        return {"window_key": self.window_key, "terminal": self.terminal,
                "accepted": self.accepted, "regime": self.regime,
                "zscore_bucket": self.zscore_bucket, "pnl_usd": self.pnl_usd, "won": self.won}


@dataclass
class CandidateDecision:
    """The directional model's view (not authoritative for execution)."""
    side: Optional[str]
    fair_p_up: Optional[float]
    outcome_prob: Optional[float]
    model_edge: float
    tradeable: bool
    reason: str

    def to_dict(self) -> dict:
        return {"side": self.side,
                "fair_p_up": (round(self.fair_p_up, 4) if self.fair_p_up is not None else None),
                "outcome_prob": (round(self.outcome_prob, 4) if self.outcome_prob is not None else None),
                "model_edge": round(self.model_edge, 4), "tradeable": self.tradeable,
                "reason": self.reason}


@dataclass
class ExecutionCostEstimate:
    """Output of the authoritative execution-quality gate (orderbook-reality EV)."""
    accepted: bool
    reason: str
    best_ask: Optional[float] = None
    vwap: Optional[float] = None
    slippage: float = 0.0
    ev_after_slippage: Optional[float] = None
    ev_at_mid: Optional[float] = None
    fillable_usd: float = 0.0
    spread: Optional[float] = None

    @classmethod
    def from_exec_result(cls, ex) -> "ExecutionCostEstimate":
        return cls(accepted=ex.accepted, reason=ex.reason, best_ask=ex.best_ask, vwap=ex.vwap,
                   slippage=ex.slippage, ev_after_slippage=ex.ev_after_slippage,
                   ev_at_mid=ex.ev_at_mid, fillable_usd=ex.fillable_usd, spread=ex.spread)

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason, "best_ask": self.best_ask,
                "vwap": (round(self.vwap, 6) if self.vwap is not None else None),
                "slippage": round(self.slippage, 6),
                "ev_after_slippage": (round(self.ev_after_slippage, 6)
                                      if self.ev_after_slippage is not None else None),
                "ev_at_mid": (round(self.ev_at_mid, 6) if self.ev_at_mid is not None else None),
                "fillable_usd": round(self.fillable_usd, 2), "spread": self.spread}


@dataclass
class TradeAction:
    kind: str = "trade"
    side: Optional[str] = None
    token_id: Optional[str] = None
    fill_price: Optional[float] = None
    size_usd: float = 0.0
    shares: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": "trade", "side": self.side, "fill_price": self.fill_price,
                "size_usd": self.size_usd, "shares": round(self.shares, 6)}


@dataclass
class RejectAction:
    kind: str = "reject"
    stage: str = "unknown"          # pre_candidate | directional | execution_gate
    reason: str = ""

    def to_dict(self) -> dict:
        return {"kind": "reject", "stage": self.stage, "reason": self.reason}


@dataclass
class PaperFill:
    window_key: str
    side: str
    fill_price: float
    shares: float
    size_usd: float

    def to_dict(self) -> dict:
        return {"window_key": self.window_key, "side": self.side, "fill_price": self.fill_price,
                "shares": round(self.shares, 6), "size_usd": self.size_usd}


@dataclass
class DecisionResult:
    """The complete, auditable lifecycle record for one candidate."""
    market_context: MarketContext
    candidate: CandidateDecision
    features: Optional[dict] = None
    signals: Optional[dict] = None              # observe-only raw signal snapshot (Phase 4)
    cost: Optional[ExecutionCostEstimate] = None
    action: Optional[object] = None             # TradeAction | RejectAction
    fill: Optional[PaperFill] = None
    status: str = "rejected"                    # accepted | rejected (legacy two-state view)
    terminal: str = "rejected"                  # accepted|rejected|skipped|expired|missing_data
    reject_stage: Optional[str] = None
    terminal_reason: Optional[str] = None       # reason for skipped/missing_data/expired
    lifecycle: list = field(default_factory=lambda: ["created"])

    def mark(self, stage: str) -> None:
        if stage not in self.lifecycle:
            self.lifecycle.append(stage)

    def finalize(self, terminal: str, *, reason: Optional[str] = None,
                 stage: Optional[str] = None) -> "DecisionResult":
        """Set the single terminal state (one of TERMINALS) — guarantees the candidate never
        disappears: it always ends classified."""
        self.terminal = terminal
        self.terminal_reason = reason
        self.status = "accepted" if terminal == "accepted" else "rejected"
        if terminal == "rejected":
            self.reject_stage = stage
        self.mark(terminal)
        if terminal == "accepted" and self.fill is not None:
            self.mark("ledgered")
        self.mark("reported")
        return self

    def to_dict(self) -> dict:
        return {"market_context": self.market_context.to_dict(),
                "candidate": self.candidate.to_dict(),
                "features": self.features,
                "signals": self.signals,
                "cost": (self.cost.to_dict() if self.cost else None),
                "action": (self.action.to_dict() if self.action else None),
                "fill": (self.fill.to_dict() if self.fill else None),
                "status": self.status, "terminal": self.terminal,
                "reject_stage": self.reject_stage, "terminal_reason": self.terminal_reason,
                "lifecycle": list(self.lifecycle)}


class LifecycleReconciler:
    """Tallies every candidate through the lifecycle so the report can PROVE no candidate
    disappears: each candidate ends in exactly one terminal state (accepted | rejected |
    skipped | expired | missing_data), and created == sum(terminals) == reported."""

    TERMINALS = ("accepted", "rejected", "skipped", "expired", "missing_data")

    def __init__(self):
        self.created = 0
        self.feature_scored = 0
        self.execution_costed = 0
        self.ledgered = 0
        self.reported = 0
        self.terminals = {t: 0 for t in self.TERMINALS}
        self.rejected_by_stage = {"directional": 0, "execution_gate": 0}
        self.skipped_by_reason: dict = {}
        self.missing_by_reason: dict = {}

    def record(self, dr: DecisionResult) -> None:
        self.created += 1
        self.reported += 1
        if dr.features is not None:
            self.feature_scored += 1
        if dr.cost is not None:
            self.execution_costed += 1
        t = dr.terminal if dr.terminal in self.terminals else "rejected"
        self.terminals[t] += 1
        if t == "accepted" and dr.fill is not None:
            self.ledgered += 1
        elif t == "rejected":
            stage = dr.reject_stage or "directional"
            self.rejected_by_stage[stage] = self.rejected_by_stage.get(stage, 0) + 1
        elif t == "skipped":
            r = dr.terminal_reason or "unknown"
            self.skipped_by_reason[r] = self.skipped_by_reason.get(r, 0) + 1
        elif t == "missing_data":
            r = dr.terminal_reason or "unknown"
            self.missing_by_reason[r] = self.missing_by_reason.get(r, 0) + 1

    def report(self) -> dict:
        term_sum = sum(self.terminals.values())
        return {"created": self.created, "feature_scored": self.feature_scored,
                "execution_costed": self.execution_costed, "ledgered": self.ledgered,
                "reported": self.reported, "terminals": dict(self.terminals),
                "rejected_by_stage": dict(self.rejected_by_stage),
                "skipped_by_reason": dict(self.skipped_by_reason),
                "missing_by_reason": dict(self.missing_by_reason),
                "no_candidate_disappeared": (self.created == term_sum
                                             and self.reported == self.created),
                "reconciled": (self.created == term_sum
                               and self.reported == self.created
                               and self.ledgered == self.terminals["accepted"])}
