"""Research-engine schemas (Phase 5).

Grok is a RESEARCH analyst here: it estimates probability and supplies evidence.
It cannot execute, cannot size orders, and cannot bypass the RiskEngine. These
models are the strict-validation contract for Grok output and the audited
probability bundle that strategy code may (optionally) consume.

Quant scope — *Statistical & Probabilistic Modeling* (the calibrated/ensemble
probability bundle) and *Compliance/Security/Operational Excellence* (strict
schema validation strips any execution/size field from Grok output). The
``diagnostics`` dict carries calibration metadata (method/version) for audit;
the probability bundle never carries an order, size, or approval field.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ResearchMode = Literal[
    "disabled", "offline_cache", "online_paper", "online_shadow", "guarded_live_readonly"
]
ONLINE_MODES = frozenset({"online_paper", "online_shadow", "guarded_live_readonly"})

ResearchRunStatus = Literal[
    "CREATED", "RUNNING", "SUCCEEDED", "FAILED", "BUDGET_BLOCKED",
    "VALIDATION_FAILED", "NO_EVIDENCE", "AMBIGUOUS", "CACHE_HIT", "CACHE_MISS",
]

SourceType = Literal[
    "official", "exchange", "market_resolution_source", "news", "government",
    "social_x", "market_page", "academic", "unknown",
]

EvidenceDirection = Literal[
    "supports_yes", "supports_no", "mixed", "neutral", "undermines_market_assumption",
]

AMBIGUITY_CATEGORIES = (
    "unclear_resolution_source", "subjective_judgment", "missing_deadline",
    "vague_threshold", "conflicting_sources", "multi_condition_resolution",
    "social_media_rumor_dependency", "legal_or_regulatory_interpretation",
    "oracle_or_dispute_risk", "stale_market_metadata",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _clamp01(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_id: str = Field(default_factory=lambda: _nid("ev"))
    source_id: Optional[str] = None
    source_type: SourceType = "unknown"
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    retrieved_ts_ms: int = Field(default_factory=_now_ms)
    published_ts_ms: Optional[int] = None
    claim: str = ""
    short_excerpt: Optional[str] = None
    direction: EvidenceDirection = "neutral"
    weight: float = 0.0
    credibility: float = 0.0
    freshness: float = 0.0
    relevance: float = 0.0

    @field_validator("weight", "credibility", "freshness", "relevance", mode="before")
    @classmethod
    def _c01(cls, v):
        return _clamp01(v)

    @field_validator("short_excerpt", mode="before")
    @classmethod
    def _trunc(cls, v):
        # store SHORT excerpts only (never full copyrighted articles)
        return None if v is None else str(v)[:500]

    @field_validator("claim", mode="before")
    @classmethod
    def _trunc_claim(cls, v):
        return str(v or "")[:500]


class MarketRuleSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_id: str
    asset_id: Optional[str] = None
    venue: str = "polymarket"
    question: str = ""
    outcome: Optional[str] = None
    resolution_source: Optional[str] = None
    close_ts_ms: Optional[int] = None
    resolution_deadline_ts_ms: Optional[int] = None
    criteria: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    ambiguous_terms: list[str] = Field(default_factory=list)
    ambiguity_categories: list[str] = Field(default_factory=list)
    ambiguity_score: float = 0.0
    parsed_ts_ms: int = Field(default_factory=_now_ms)

    @field_validator("ambiguity_score", mode="before")
    @classmethod
    def _c01(cls, v):
        return _clamp01(v)


class GrokProbabilityOutput(BaseModel):
    """STRICT schema for Grok research output. Extra fields are ignored (any
    execution/size fields are stripped here and flagged by validators)."""

    model_config = ConfigDict(extra="ignore")

    market_id: str
    asset_id: Optional[str] = None
    outcome: str = "YES"
    fair_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    key_assumptions: list[str] = Field(default_factory=list)
    resolution_notes: str = ""
    ambiguity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    no_trade_recommendation: bool = False
    no_trade_reason: Optional[str] = None
    do_not_trade_if: list[str] = Field(default_factory=list)
    expected_update_triggers: list[str] = Field(default_factory=list)
    source_coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)


class ProbabilityEstimateBundle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    estimate_id: str = Field(default_factory=lambda: _nid("est"))
    research_run_id: Optional[str] = None
    venue: str = "polymarket"
    market_id: str = ""
    asset_id: Optional[str] = None
    outcome: str = "YES"
    ts_ms: int = Field(default_factory=_now_ms)
    p_market_mid: Optional[float] = None
    p_llm_raw: Optional[float] = None
    p_model: Optional[float] = None
    p_calibrated: float = 0.5
    p_ensemble: float = 0.5
    confidence: float = 0.0
    ambiguity_score: float = 0.0
    evidence_score: float = 0.0
    source_count: int = 0
    calibration_version: str = "v1"
    ensemble_version: str = "v1"
    stale_after_ts_ms: int = 0
    no_trade_reason: Optional[str] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def record(self) -> dict:
        def s(x):
            return None if x is None else str(x)
        return {
            "estimate_id": self.estimate_id, "research_run_id": self.research_run_id,
            "venue": self.venue, "market_id": self.market_id, "asset_id": self.asset_id,
            "outcome": self.outcome, "ts_ms": self.ts_ms, "p_market_mid": s(self.p_market_mid),
            "p_llm_raw": s(self.p_llm_raw), "p_model": s(self.p_model),
            "p_calibrated": s(self.p_calibrated), "p_ensemble": s(self.p_ensemble),
            "confidence": s(self.confidence), "ambiguity_score": s(self.ambiguity_score),
            "evidence_score": s(self.evidence_score), "source_count": self.source_count,
            "calibration_version": self.calibration_version,
            "ensemble_version": self.ensemble_version, "stale_after_ts_ms": self.stale_after_ts_ms,
            "no_trade_reason": self.no_trade_reason,
            "payload_json": _json(self.diagnostics),
        }


class ResearchFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    research_run_id: str = Field(default_factory=lambda: _nid("rr"))
    market_id: str = ""
    asset_id: Optional[str] = None
    status: ResearchRunStatus = "FAILED"
    reason: str = ""
    retryable: bool = False
    ts_ms: int = Field(default_factory=_now_ms)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


def _json(obj) -> str:
    import json
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"
