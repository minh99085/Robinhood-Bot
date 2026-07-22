"""Tests for chart vision extraction, MCP validation, and pipeline wiring."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.extractor import analyze_tradingview_chart
from engine.chart_vision.image_utils import load_image_bytes, to_base64
from engine.chart_vision.mcp_validator import (
    realized_vol_from_closes,
    validate_extraction,
)
from engine.chart_vision.models import (
    Bias,
    ChartExtractionResult,
    FieldConfidence,
    IndicatorBundle,
    LevelKind,
    MACDState,
    MCPMarketSnapshot,
    PriceLevel,
    RSIState,
    ValidationStatus,
)
from engine.chart_vision.pipeline import run_full_pipeline
from engine.chart_vision.vision_backends import MockVisionBackend, extract_json_object
from engine.robinhood.audit_log import AuditLog


# Minimal 1x1 PNG
_MIN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def mock_cfg(tmp_path, monkeypatch) -> ChartVisionConfig:
    monkeypatch.setenv("CHART_VISION_PROVIDER", "mock")
    monkeypatch.setenv("CHART_VISION_ENABLED", "1")
    monkeypatch.setenv("CHART_VISION_REQUIRE_MCP", "0")
    monkeypatch.setenv("CHART_VISION_RUN_MC", "1")
    monkeypatch.setenv("CHART_VISION_MC_PATHS", "800")
    monkeypatch.setenv("CHART_VISION_EXECUTION_MODE", "recommendation_only")
    mc = Path(r"C:\Users\tieut\Monte-Carlo-Sim")
    if mc.is_dir():
        monkeypatch.setenv("MONTE_CARLO_SIM_PATH", str(mc))
    else:
        monkeypatch.setenv("MONTE_CARLO_SIM_PATH", str(tmp_path / "missing_mc"))
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    return ChartVisionConfig.from_env()


def test_extract_json_object_fences():
    text = 'Here you go:\n```json\n{"ticker": "AAPL", "x": 1}\n```\n'
    obj = extract_json_object(text)
    assert obj["ticker"] == "AAPL"


def test_load_image_base64():
    b64 = to_base64(_MIN_PNG)
    data, mime = load_image_bytes(image_base64=b64)
    assert data == _MIN_PNG
    assert "image" in mime


def test_mock_extraction(mock_cfg):
    result = analyze_tradingview_chart(
        image_base64=to_base64(_MIN_PNG),
        ticker_hint="MSFT",
        config=mock_cfg,
        backend=MockVisionBackend(),
    )
    assert result.ticker == "MSFT"
    assert result.bias in (Bias.BULLISH, Bias.BEARISH, Bias.NEUTRAL, Bias.UNCLEAR)
    assert 0.0 <= result.confidence.overall <= 1.0
    assert result.provider == "mock"


def test_realized_vol():
    # Synthetic path with non-constant returns (constant growth → zero variance)
    closes = [100.0]
    mults = [1.01, 0.99, 1.02, 0.98, 1.015, 0.995, 1.03, 0.97]
    for i in range(32):
        closes.append(closes[-1] * mults[i % len(mults)])
    vol = realized_vol_from_closes(closes)
    assert vol is not None
    assert vol > 0


def test_validate_price_mismatch(mock_cfg):
    extraction = ChartExtractionResult(
        ticker="AAPL",
        timeframe="1H",
        indicators=IndicatorBundle(
            rsi=RSIState(value=50, zone="neutral", confidence=0.8),
            macd=MACDState(cross="none", confidence=0.5),
        ),
        levels=[],
        bias=Bias.BULLISH,
        confidence=FieldConfidence(overall=0.8, ticker=0.9, price=0.7),
        image_last_price=200.0,
    )
    mcp = MCPMarketSnapshot(ticker="AAPL", last_price=100.0)
    val = validate_extraction(extraction, mcp, mock_cfg, mcp_available=True)
    assert val.status == ValidationStatus.REJECTED
    assert any(d.code == "price_mismatch" for d in val.discrepancies)


def test_validate_pass(mock_cfg):
    extraction = ChartExtractionResult(
        ticker="AAPL",
        timeframe="1H",
        indicators=IndicatorBundle(),
        levels=[PriceLevel(price=188.0, kind=LevelKind.SUPPORT, strength=0.6)],
        bias=Bias.BULLISH,
        confidence=FieldConfidence(overall=0.75, ticker=0.9, price=0.7),
        image_last_price=190.0,
    )
    mcp = MCPMarketSnapshot(
        ticker="AAPL",
        last_price=190.5,
        realized_vol_annual=0.25,
        portfolio_equity=10_000,
    )
    val = validate_extraction(extraction, mcp, mock_cfg, mcp_available=True)
    assert val.status in (ValidationStatus.PASSED, ValidationStatus.DOWNWEIGHTED)
    assert val.ticker_confirmed


class _MockMCP:
    async def call_tool(self, name: str, arguments=None):
        if "quote" in name:
            return {"symbol": "AAPL", "last_trade_price": 190.0}
        if "historical" in name:
            return {
                "historicals": [
                    {"close_price": 180 + i} for i in range(40)
                ]
            }
        if name == "get_portfolio":
            return {"equity": 10000.0, "buying_power": 5000.0}
        raise RuntimeError(f"unknown tool {name}")


@pytest.mark.asyncio
async def test_full_pipeline_with_mock_mcp(mock_cfg, tmp_path):
    audit = AuditLog(tmp_path)
    resp = await run_full_pipeline(
        image_base64=to_base64(_MIN_PNG),
        ticker_hint="AAPL",
        config=mock_cfg,
        backend=MockVisionBackend(),
        mcp_client=_MockMCP(),
        audit=audit,
        run_monte_carlo=True,
        mc_paths=500,
    )
    assert resp.ok
    assert resp.extraction is not None
    assert resp.extraction.ticker == "AAPL"
    assert resp.validation is not None
    assert resp.audit_id
    # MC may succeed if Monte-Carlo-Sim is on disk
    mc_path = Path(mock_cfg.monte_carlo_sim_path)
    if mc_path.is_dir() and (mc_path / "chart_vision_pipeline.py").is_file():
        assert resp.decision is not None
        assert resp.decision.get("ticker") == "AAPL"
        assert resp.decision.get("risk", {}).get("n_paths") == 500
        assert "action" in resp.decision
    else:
        # Graceful degradation
        assert resp.decision is None or resp.warnings

    # Audit log written
    log = tmp_path / "robinhood_audit.jsonl"
    assert log.is_file()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert any("chart_vision" in ln for ln in lines)


def test_tool_handler_mock(mock_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    from tools import handle_analyze_tradingview_chart

    # Force mock backend via env already set; tool uses full pipeline
    out = handle_analyze_tradingview_chart(
        {
            "image_base64": to_base64(_MIN_PNG),
            "ticker_hint": "AAPL",
            "run_monte_carlo": False,
            "run_validation": False,
        }
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["extraction"]["ticker"] == "AAPL"
