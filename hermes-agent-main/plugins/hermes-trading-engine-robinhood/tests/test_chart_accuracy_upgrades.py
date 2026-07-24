"""Accuracy upgrades: schema-correct data pulls, computed-indicator ground
truth, RSI cross-check, and radiology-style double reading."""

from __future__ import annotations

import pytest

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.extractor import merge_reads
from engine.chart_vision.mcp_validator import (
    fetch_mcp_snapshot,
    indicators_from_closes,
    validate_extraction,
)
from engine.chart_vision.models import (
    Bias,
    ChartExtractionResult,
    MCPMarketSnapshot,
)


# ---------------------------------------------------------------------------
# Schema-correct MCP pulls (from the live catalog)
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name == "get_equity_quotes":
            return {"results": [{"symbol": "NVDA", "last_trade_price": "180.0"}]}
        if name == "get_equity_historicals":
            return {"historicals": [{"close_price": 100.0 + i * 0.5}
                                    for i in range(80)]}
        if name == "get_accounts":
            return {"accounts": [{"account_number": "ACC123"}]}
        if name == "get_portfolio":
            assert arguments == {"account_number": "ACC123"}
            return {"equity": 10000.0, "buying_power": 5000.0}
        raise RuntimeError(f"unknown tool {name}")


@pytest.mark.asyncio
async def test_historicals_and_portfolio_use_catalog_schemas():
    rec = Recorder()
    snap = await fetch_mcp_snapshot(rec, "NVDA")

    hist_calls = [a for (n, a) in rec.calls if n == "get_equity_historicals"]
    assert len(hist_calls) == 1
    args = hist_calls[0]
    assert args["symbols"] == ["NVDA"]
    assert args["interval"] == "day"
    assert args["start_time"].endswith("Z")     # RFC3339 UTC, required key
    assert "span" not in args and "symbol" not in args

    # portfolio flow: get_accounts first, then account_number-scoped call
    names = [n for (n, _) in rec.calls]
    assert names.index("get_accounts") < names.index("get_portfolio")
    assert snap.buying_power == 5000.0
    # rising series → indicators computed and attached
    assert snap.computed_indicators is not None
    assert snap.realized_vol_annual is not None


# ---------------------------------------------------------------------------
# Indicator math ground truth
# ---------------------------------------------------------------------------


def test_indicators_rising_series_reads_bullish():
    closes = [100.0 + i for i in range(60)]     # steady climb
    ind = indicators_from_closes(closes)
    assert ind["rsi14"] == 100.0                # all gains, no losses
    assert ind["ema9"] > ind["ema21"]
    assert ind["ema_cross"] == "bullish"
    assert ind["macd_hist"] is not None


def test_indicators_falling_series_reads_bearish():
    closes = [200.0 - i for i in range(60)]
    ind = indicators_from_closes(closes)
    assert ind["rsi14"] < 5.0
    assert ind["ema_cross"] == "bearish"


def test_indicators_flat_series_is_neutral_and_short_history_empty():
    flat = [100.0] * 60
    assert indicators_from_closes(flat)["rsi14"] == 50.0
    assert indicators_from_closes([100.0] * 10) == {}


# ---------------------------------------------------------------------------
# RSI cross-check in validation
# ---------------------------------------------------------------------------


def _extraction(rsi=None, conf=0.8):
    return ChartExtractionResult.model_validate({
        "ticker": "NVDA",
        "timeframe": "1D",
        "bias": "bullish",
        "confidence": {"overall": conf},
        "indicators": {"rsi": {"value": rsi}} if rsi is not None else {},
        "image_last_price": 100.0,
    })


def _cfg():
    return ChartVisionConfig.from_env()


def test_rsi_mismatch_downweights():
    mcp = MCPMarketSnapshot(ticker="NVDA", last_price=100.0,
                            computed_indicators={"rsi14": 30.0})
    ok = validate_extraction(_extraction(rsi=32.0), mcp, _cfg())
    bad = validate_extraction(_extraction(rsi=70.0), mcp, _cfg())
    assert not any(d.code == "rsi_mismatch" for d in ok.discrepancies)
    assert any(d.code == "rsi_mismatch" for d in bad.discrepancies)
    assert bad.adjusted_confidence < ok.adjusted_confidence


# ---------------------------------------------------------------------------
# Double reading (ensemble merge)
# ---------------------------------------------------------------------------


def _read(bias="bullish", rsi=55.0, price=100.0, conf=0.8, ticker="NVDA"):
    return ChartExtractionResult.model_validate({
        "ticker": ticker,
        "timeframe": "1D",
        "bias": bias,
        "confidence": {"overall": conf},
        "indicators": {"rsi": {"value": rsi, "confidence": 0.8}},
        "image_last_price": price,
    })


def test_agreeing_reads_average_and_keep_confidence():
    merged = merge_reads([_read(rsi=54.0), _read(rsi=56.0, conf=0.7)])
    assert merged.bias == Bias.BULLISH
    assert merged.indicators.rsi.value == 55.0     # noise cancelled
    assert merged.confidence.overall == 0.7        # min of reads, no penalty


def test_bias_disagreement_goes_neutral_and_downweights():
    merged = merge_reads([_read(bias="bullish"), _read(bias="bearish")])
    assert merged.bias == Bias.NEUTRAL
    assert merged.confidence.overall == pytest.approx(0.8 * 0.7)
    assert any("bias_disagreement" in w for w in merged.extraction_warnings)


def test_rsi_and_ticker_disagreement_stack_penalties():
    merged = merge_reads([
        _read(rsi=40.0),
        _read(rsi=60.0, ticker="AAPL"),
    ])
    # ticker split (×0.5) + rsi spread 20 (×0.7)
    assert merged.confidence.overall == pytest.approx(0.8 * 0.5 * 0.7)
    assert merged.indicators.rsi.value == 50.0


# ---------------------------------------------------------------------------
# Real Robinhood historicals shape: data.results[].bars[].close_price
# ---------------------------------------------------------------------------


def _rh_reply(symbols_bars):
    """Build a reply in Robinhood's real shape."""
    return {"data": {"results": [
        {"symbol": s, "interval": "day",
         "bars": [{"begins_at": f"2026-07-{i+1:02d}T00:00:00Z",
                   "open_price": f"{c-1:.4f}", "close_price": f"{c:.4f}",
                   "high_price": f"{c+1:.4f}", "low_price": f"{c-2:.4f}",
                   "volume": 1_000_000 + i, "session": "reg"}
                  for i, c in enumerate(bars)]}
        for s, bars in symbols_bars.items()]},
        "guide": {}}


def test_bars_parse_from_real_robinhood_shape():
    from engine.chart_vision.mcp_validator import (
        _closes_from_historicals, bars_from_historicals)

    reply = _rh_reply({"AAPL": [100.0, 101.0, 102.5]})
    bars = bars_from_historicals(reply)
    assert len(bars) == 3
    closes = _closes_from_historicals(reply)
    assert closes == [100.0, 101.0, 102.5]   # string close_price coerced


def test_scout_splits_and_series_from_real_shape():
    from engine.robinhood.scout import _series_from_historicals, _split_by_symbol

    reply = _rh_reply({"AAPL": [10.0, 11.0], "MSFT": [20.0, 22.0]})
    parts = _split_by_symbol(reply, ["AAPL", "MSFT"])
    assert set(parts) == {"AAPL", "MSFT"}
    series = _series_from_historicals(parts["MSFT"])
    assert series["closes"] == [20.0, 22.0]
    assert len(series["dollar_volume"]) == 2
    assert series["dollar_volume"][0] == 20.0 * 1_000_000       # close × volume


@pytest.mark.asyncio
async def test_run_scout_end_to_end_real_shape():
    """The whole scout path over the real reply shape finds a candidate."""
    from engine.robinhood.scout import run_scout

    up = [100.0 * (1.01 ** i) for i in range(130)]
    flat = [100.0 for _ in range(130)]

    class FakeClient:
        async def call_tool(self, name, arguments=None):
            syms = arguments["symbols"]
            return _rh_reply({s: (up if s == "NVDA" else flat) for s in syms})

    out = await run_scout(FakeClient(), universe=["NVDA", "KO", "PG"])
    assert out["scanned"] == 3          # not zero — the real bug's fingerprint
    assert any(r["symbol"] == "NVDA" for r in out["suggest"])
