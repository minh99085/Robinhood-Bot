"""
Pydantic models for chart vision (plugin-local copy for Docker independence).

When ``MONTE_CARLO_SIM_PATH`` is available, the MC pipeline re-validates
compatible dicts with Monte-Carlo-Sim's ``chart_vision_models``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class LevelKind(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    PIVOT = "pivot"
    OTHER = "other"


class Action(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    DOWNWEIGHTED = "downweighted"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class PriceLevel(BaseModel):
    price: float = Field(..., gt=0)
    kind: LevelKind = LevelKind.OTHER
    strength: float = Field(0.5, ge=0.0, le=1.0)
    label: Optional[str] = None

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        return float(v)


class RSIState(BaseModel):
    value: Optional[float] = Field(None, ge=0.0, le=100.0)
    zone: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class MACDState(BaseModel):
    macd_line: Optional[float] = None
    signal_line: Optional[float] = None
    histogram: Optional[float] = None
    cross: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class IndicatorBundle(BaseModel):
    rsi: RSIState = Field(default_factory=RSIState)
    macd: MACDState = Field(default_factory=MACDState)
    extras: Dict[str, Any] = Field(default_factory=dict)


class FieldConfidence(BaseModel):
    ticker: float = Field(0.0, ge=0.0, le=1.0)
    timeframe: float = Field(0.0, ge=0.0, le=1.0)
    indicators: float = Field(0.0, ge=0.0, le=1.0)
    levels: float = Field(0.0, ge=0.0, le=1.0)
    bias: float = Field(0.0, ge=0.0, le=1.0)
    price: float = Field(0.0, ge=0.0, le=1.0)
    overall: float = Field(0.0, ge=0.0, le=1.0)


class ChartExtractionResult(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=16)
    timeframe: str = Field(..., min_length=1)
    indicators: IndicatorBundle = Field(default_factory=IndicatorBundle)
    levels: List[PriceLevel] = Field(default_factory=list)
    bias: Bias = Bias.UNCLEAR
    confidence: FieldConfidence = Field(default_factory=FieldConfidence)
    raw_model_description: str = ""
    extraction_warnings: List[str] = Field(default_factory=list)
    image_last_price: Optional[float] = Field(None, gt=0)
    provider: Optional[str] = None
    model: Optional[str] = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: Any) -> str:
        s = str(v or "").strip().upper()
        if ":" in s:
            s = s.split(":")[-1]
        if not s:
            raise ValueError("ticker must be non-empty")
        return s

    @field_validator("timeframe", mode="before")
    @classmethod
    def _normalize_tf(cls, v: Any) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("timeframe must be non-empty")
        return s

    @field_validator("bias", mode="before")
    @classmethod
    def _normalize_bias(cls, v: Any) -> str:
        if v is None or v == "":
            return Bias.UNCLEAR.value
        if isinstance(v, Bias):
            return v.value
        s = str(v).strip().lower()
        if s.startswith("bias."):
            s = s.split(".", 1)[1]
        aliases = {
            "bull": "bullish",
            "long": "bullish",
            "buy": "bullish",
            "up": "bullish",
            "bear": "bearish",
            "short": "bearish",
            "sell": "bearish",
            "down": "bearish",
            "sideways": "neutral",
            "range": "neutral",
            "unknown": "unclear",
            "n/a": "unclear",
        }
        s = aliases.get(s, s)
        if s not in {b.value for b in Bias}:
            return Bias.UNCLEAR.value
        return s

    @model_validator(mode="after")
    def _default_overall_confidence(self) -> "ChartExtractionResult":
        c = self.confidence
        if c.overall <= 0:
            parts = [c.ticker, c.timeframe, c.indicators, c.levels, c.bias, c.price]
            known = [p for p in parts if p > 0]
            if known:
                c.overall = float(sum(known) / len(known))
        return self


class MCPMarketSnapshot(BaseModel):
    ticker: str
    last_price: Optional[float] = Field(None, gt=0)
    bid: Optional[float] = None
    ask: Optional[float] = None
    previous_close: Optional[float] = None
    realized_vol_annual: Optional[float] = Field(None, ge=0.0)
    portfolio_equity: Optional[float] = None
    buying_power: Optional[float] = None
    existing_position_qty: Optional[float] = None
    raw_quotes: Optional[Dict[str, Any]] = None
    raw_historicals: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


class ValidationDiscrepancy(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    image_value: Optional[Any] = None
    mcp_value: Optional[Any] = None


class ValidationResult(BaseModel):
    status: ValidationStatus
    overall_confidence: float = Field(0.0, ge=0.0, le=1.0)
    adjusted_confidence: float = Field(0.0, ge=0.0, le=1.0)
    discrepancies: List[ValidationDiscrepancy] = Field(default_factory=list)
    price_rel_error: Optional[float] = None
    ticker_confirmed: bool = False
    notes: List[str] = Field(default_factory=list)


class AnalyzeChartRequest(BaseModel):
    """Input for analyze_tradingview_chart tool / API."""

    image_base64: Optional[str] = None
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    mime_type: Optional[str] = None
    run_validation: bool = True
    run_monte_carlo: Optional[bool] = None
    mc_paths: Optional[int] = None
    execution_mode: Optional[str] = None
    ticker_hint: Optional[str] = None


class AnalyzeChartResponse(BaseModel):
    ok: bool
    extraction: Optional[ChartExtractionResult] = None
    validation: Optional[ValidationResult] = None
    mcp: Optional[MCPMarketSnapshot] = None
    decision: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    audit_id: Optional[str] = None
