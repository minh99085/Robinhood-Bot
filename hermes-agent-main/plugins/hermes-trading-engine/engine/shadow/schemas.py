"""Shadow-mode schemas (Phase 7).

Shadow mode runs the full live decision stack WITHOUT submitting orders: it
records "would-have-traded" decisions, routes every proposal through the
RiskEngine, simulates fills with the Phase 3 PaperBroker, tracks subsequent
market outcomes, and produces a hard live-readiness report. No real order
submission, cancellation, live broker, wallet signing, or private channels.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ShadowSessionStatus = Literal[
    "CREATED", "STARTING", "RUNNING", "DEGRADED", "PAUSED", "STOPPING", "STOPPED", "FAILED"]

ReadinessGateStatus = Literal["PASS", "WARN", "FAIL", "NOT_ENOUGH_DATA"]

# Overall readiness statuses. There is intentionally NO "AUTO_LIVE" /
# "READY_FOR_LIVE_AUTO" — nothing here ever auto-enables live trading.
OverallReadiness = Literal[
    "NOT_READY", "SHADOW_RUNNING", "SHADOW_DEGRADED", "NOT_ENOUGH_DATA",
    "SHADOW_STABLE_BUT_NOT_APPROVED", "READY_FOR_MANUAL_REVIEW"]

DecisionKind = Literal["PROPOSED", "ABSTAINED", "RISK_REJECTED", "APPROVED_SHADOW", "ERROR"]

SHADOW_MODE = "shadow_live"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _s(x) -> Optional[str]:
    return None if x is None else str(x)


def _json(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class ShadowSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    shadow_session_id: str = Field(default_factory=lambda: _nid("shadow"))
    status: ShadowSessionStatus = "CREATED"
    started_ts_ms: int = Field(default_factory=_now_ms)
    stopped_ts_ms: Optional[int] = None
    config_hash: str = ""
    venues: list[str] = Field(default_factory=list)
    mode: str = SHADOW_MODE
    notes: Optional[str] = None


class CandidateMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidate_id: str = Field(default_factory=lambda: _nid("cand"))
    shadow_session_id: str = ""
    ts_ms: int = Field(default_factory=_now_ms)
    venue: str = "polymarket"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    question: str = ""
    category: Optional[str] = None
    close_ts_ms: Optional[int] = None
    liquidity_score: Optional[float] = None
    spread: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    ambiguity_score: Optional[float] = None
    metadata_complete: bool = False
    data_fresh: bool = False
    selected: bool = False
    rejection_reason: Optional[str] = None

    def record(self) -> dict:
        return {
            "candidate_id": self.candidate_id, "shadow_session_id": self.shadow_session_id,
            "ts_ms": self.ts_ms, "venue": self.venue, "market_id": self.market_id,
            "market_ticker": self.market_ticker, "asset_id": self.asset_id,
            "outcome": self.outcome, "question": self.question, "category": self.category,
            "close_ts_ms": self.close_ts_ms, "liquidity_score": _s(self.liquidity_score),
            "spread": _s(self.spread), "volume": _s(self.volume),
            "open_interest": _s(self.open_interest), "ambiguity_score": _s(self.ambiguity_score),
            "metadata_complete": int(self.metadata_complete), "data_fresh": int(self.data_fresh),
            "selected": int(self.selected), "rejection_reason": self.rejection_reason,
            "payload_json": _json({}),
        }


class ShadowDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id: str = Field(default_factory=lambda: _nid("dec"))
    shadow_session_id: str = ""
    ts_ms: int = Field(default_factory=_now_ms)
    cycle_id: str = ""
    venue: str = "polymarket"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    p_market: Optional[float] = None
    p_research: Optional[float] = None
    p_ensemble: Optional[float] = None
    confidence: Optional[float] = None
    ambiguity_score: Optional[float] = None
    evidence_score: Optional[float] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    midpoint: Optional[Decimal] = None
    intended_side: Optional[str] = None
    intended_limit_price: Optional[Decimal] = None
    intended_notional: Optional[Decimal] = None
    edge_after_costs: Optional[float] = None
    decision: DecisionKind = "ABSTAINED"
    reason: str = ""
    proposal_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def record(self) -> dict:
        return {
            "decision_id": self.decision_id, "shadow_session_id": self.shadow_session_id,
            "ts_ms": self.ts_ms, "cycle_id": self.cycle_id, "venue": self.venue,
            "market_id": self.market_id, "market_ticker": self.market_ticker,
            "asset_id": self.asset_id, "outcome": self.outcome, "p_market": _s(self.p_market),
            "p_research": _s(self.p_research), "p_ensemble": _s(self.p_ensemble),
            "confidence": _s(self.confidence), "ambiguity_score": _s(self.ambiguity_score),
            "evidence_score": _s(self.evidence_score), "best_bid": _s(self.best_bid),
            "best_ask": _s(self.best_ask), "spread": _s(self.spread), "midpoint": _s(self.midpoint),
            "intended_side": self.intended_side, "intended_limit_price": _s(self.intended_limit_price),
            "intended_notional": _s(self.intended_notional),
            "edge_after_costs": _s(self.edge_after_costs), "decision": self.decision,
            "reason": self.reason, "proposal_id": self.proposal_id,
            "risk_decision_id": self.risk_decision_id, "payload_json": _json(self.diagnostics),
        }


class ShadowOrder(BaseModel):
    model_config = ConfigDict(extra="ignore")
    shadow_order_id: str = Field(default_factory=lambda: _nid("sord"))
    shadow_session_id: str = ""
    decision_id: str = ""
    proposal_id: Optional[str] = None
    client_order_id: str = ""
    venue: str = "polymarket"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    side: str = "BUY"
    order_type: str = "MARKETABLE_LIMIT"
    limit_price: Optional[Decimal] = None
    quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    status: str = "CREATED"
    reject_reason: Optional[str] = None
    created_ts_ms: int = Field(default_factory=_now_ms)
    updated_ts_ms: int = Field(default_factory=_now_ms)

    def record(self) -> dict:
        return {
            "shadow_order_id": self.shadow_order_id, "shadow_session_id": self.shadow_session_id,
            "decision_id": self.decision_id, "proposal_id": self.proposal_id,
            "client_order_id": self.client_order_id, "venue": self.venue,
            "market_id": self.market_id, "market_ticker": self.market_ticker,
            "asset_id": self.asset_id, "outcome": self.outcome, "side": self.side,
            "order_type": self.order_type, "limit_price": _s(self.limit_price),
            "quantity": _s(self.quantity), "notional": _s(self.notional), "status": self.status,
            "reject_reason": self.reject_reason, "created_ts_ms": self.created_ts_ms,
            "updated_ts_ms": self.updated_ts_ms, "payload_json": _json({"mode": SHADOW_MODE}),
        }


class ShadowFill(BaseModel):
    model_config = ConfigDict(extra="ignore")
    shadow_fill_id: str = Field(default_factory=lambda: _nid("sfill"))
    shadow_session_id: str = ""
    shadow_order_id: str = ""
    client_order_id: str = ""
    venue: str = "polymarket"
    market_id: Optional[str] = None
    asset_id: Optional[str] = None
    side: str = "BUY"
    price: Decimal = Decimal(0)
    quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    liquidity_flag: str = "taker"
    ts_ms: int = Field(default_factory=_now_ms)

    def record(self) -> dict:
        return {
            "shadow_fill_id": self.shadow_fill_id, "shadow_session_id": self.shadow_session_id,
            "shadow_order_id": self.shadow_order_id, "client_order_id": self.client_order_id,
            "venue": self.venue, "market_id": self.market_id, "asset_id": self.asset_id,
            "side": self.side, "price": _s(self.price), "quantity": _s(self.quantity),
            "notional": _s(self.notional), "fee": _s(self.fee),
            "liquidity_flag": self.liquidity_flag, "ts_ms": self.ts_ms,
            "payload_json": _json({"mode": SHADOW_MODE}),
        }


class ShadowObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    observation_id: str = Field(default_factory=lambda: _nid("obs"))
    shadow_session_id: str = ""
    decision_id: Optional[str] = None
    shadow_order_id: Optional[str] = None
    venue: str = "polymarket"
    market_id: Optional[str] = None
    market_ticker: Optional[str] = None
    asset_id: Optional[str] = None
    outcome: str = "YES"
    horizon_ms: int = 0
    observed_ts_ms: int = Field(default_factory=_now_ms)
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    midpoint: Optional[Decimal] = None
    last_trade_price: Optional[Decimal] = None
    depth_near_touch: Optional[Decimal] = None
    resolved_outcome: Optional[str] = None
    markout: Optional[Decimal] = None

    def record(self) -> dict:
        return {
            "observation_id": self.observation_id, "shadow_session_id": self.shadow_session_id,
            "decision_id": self.decision_id, "shadow_order_id": self.shadow_order_id,
            "venue": self.venue, "market_id": self.market_id, "market_ticker": self.market_ticker,
            "asset_id": self.asset_id, "outcome": self.outcome, "horizon_ms": self.horizon_ms,
            "observed_ts_ms": self.observed_ts_ms, "best_bid": _s(self.best_bid),
            "best_ask": _s(self.best_ask), "spread": _s(self.spread), "midpoint": _s(self.midpoint),
            "last_trade_price": _s(self.last_trade_price), "depth_near_touch": _s(self.depth_near_touch),
            "resolved_outcome": self.resolved_outcome, "markout": _s(self.markout),
            "payload_json": _json({}),
        }


class ReadinessGateResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gate_name: str
    status: ReadinessGateStatus
    score: Optional[float] = None
    threshold: Optional[float] = None
    observed_value: Optional[float] = None
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class LiveReadinessReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    report_id: str = Field(default_factory=lambda: _nid("rdy"))
    shadow_session_id: str = ""
    generated_ts_ms: int = Field(default_factory=_now_ms)
    overall_status: OverallReadiness = "NOT_ENOUGH_DATA"
    gate_results: list[ReadinessGateResult] = Field(default_factory=list)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    recommended_next_step: str = "continue_shadow"
    explicit_no_live_orders_statement: str = (
        "No live orders were submitted. Shadow mode is read-only + simulated; it never "
        "calls a real order/cancel endpoint, never uses a live broker, never signs a "
        "Polymarket wallet transaction, and never calls a Kalshi order endpoint.")
