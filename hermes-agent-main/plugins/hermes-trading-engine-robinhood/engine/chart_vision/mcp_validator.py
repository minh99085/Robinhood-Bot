"""Cross-validate vision extraction against Robinhood MCP market data."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.models import (
    ChartExtractionResult,
    MCPMarketSnapshot,
    ValidationDiscrepancy,
    ValidationResult,
    ValidationStatus,
)

logger = logging.getLogger("hermes.robinhood.chart_vision.validate")


class MCPClientProto(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig_price(payload: Any) -> Optional[float]:
    """Best-effort price extraction from heterogeneous MCP payloads."""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, str):
        return _as_float(payload)
    if isinstance(payload, list):
        for item in payload:
            p = _dig_price(item)
            if p is not None:
                return p
        return None
    if not isinstance(payload, dict):
        return None

    # Common keys
    for key in (
        "last_trade_price",
        "last_price",
        "mark_price",
        "price",
        "ask_price",
        "bid_price",
        "close",
        "previous_close",
    ):
        if key in payload:
            p = _as_float(payload[key])
            if p is not None and p > 0:
                return p

    # Nested structures
    for key in ("quote", "quotes", "results", "data", "equity", "instrument"):
        if key in payload:
            p = _dig_price(payload[key])
            if p is not None:
                return p

    # text content blocks from MCP
    if "text" in payload and isinstance(payload["text"], str):
        try:
            import json

            return _dig_price(json.loads(payload["text"]))
        except Exception:  # noqa: BLE001
            pass
    return None


def _dig_symbol_match(payload: Any, ticker: str) -> bool:
    t = ticker.upper()
    if payload is None:
        return False
    if isinstance(payload, str):
        return t in payload.upper()
    if isinstance(payload, list):
        return any(_dig_symbol_match(x, t) for x in payload)
    if isinstance(payload, dict):
        for key in ("symbol", "ticker", "instrument", "name"):
            val = payload.get(key)
            if val is not None and t == str(val).upper().split(":")[-1]:
                return True
        return any(_dig_symbol_match(v, t) for v in payload.values())
    return False


def realized_vol_from_closes(closes: List[float], *, trading_days: int = 252) -> Optional[float]:
    """Annualized stdev of log returns from close series."""
    if len(closes) < 5:
        return None
    rets: List[float] = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 3:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * trading_days)


def _closes_from_historicals(payload: Any) -> List[float]:
    closes: List[float] = []
    if payload is None:
        return closes
    # Unwrap MCP text blocks
    if isinstance(payload, dict) and "text" in payload:
        try:
            import json

            payload = json.loads(payload["text"])
        except Exception:  # noqa: BLE001
            pass
    rows = payload
    if isinstance(payload, dict):
        for key in ("historicals", "data", "results", "candles", "bars"):
            if key in payload and isinstance(payload[key], list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return closes
    for row in rows:
        if isinstance(row, (int, float)):
            closes.append(float(row))
            continue
        if not isinstance(row, dict):
            continue
        for k in ("close_price", "close", "c", "price"):
            if k in row:
                p = _as_float(row[k])
                if p is not None:
                    closes.append(p)
                    break
    return closes


def _dig_account_number(payload: Any) -> Optional[str]:
    """Pull the first brokerage account_number out of a get_accounts reply."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        if payload.get("account_number"):
            return str(payload["account_number"])
        for key in ("accounts", "results", "data"):
            if isinstance(payload.get(key), list):
                return _dig_account_number(payload[key])
        if isinstance(payload.get("text"), str):
            try:
                import json

                return _dig_account_number(json.loads(payload["text"]))
            except Exception:  # noqa: BLE001
                return None
        return None
    if isinstance(payload, list):
        for item in payload:
            acc = _dig_account_number(item)
            if acc:
                return acc
    return None


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period  # seed with SMA
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def indicators_from_closes(closes: List[float]) -> Dict[str, Any]:
    """Compute the battery's indicators from REAL closing prices.

    This is the ground truth the vision read is checked against: RSI(14,
    Wilder), EMA(9)/EMA(21) + cross direction, MACD(12,26,9) histogram.
    Deterministic, dependency-free. Returns {} when history is too short.
    """
    out: Dict[str, Any] = {}
    if len(closes) < 30:
        return out

    # RSI(14), Wilder smoothing
    period = 14
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0.0:
        rsi = 50.0 if avg_gain == 0.0 else 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    out["rsi14"] = round(rsi, 2)

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    if ema9 is not None and ema21 is not None:
        out["ema9"] = round(ema9, 4)
        out["ema21"] = round(ema21, 4)
        out["ema_cross"] = "bullish" if ema9 > ema21 else "bearish"

    # MACD(12,26,9) histogram: signal line needs the MACD series, so walk
    # the last 9 windows.
    if len(closes) >= 26 + 9:
        macd_series = []
        for i in range(9, 0, -1):
            window = closes[: len(closes) - i + 1]
            e12, e26 = _ema(window, 12), _ema(window, 26)
            if e12 is not None and e26 is not None:
                macd_series.append(e12 - e26)
        if len(macd_series) == 9:
            signal = sum(macd_series) / 9.0
            out["macd_hist"] = round(macd_series[-1] - signal, 4)

    return out


async def fetch_mcp_snapshot(
    client: MCPClientProto,
    ticker: str,
) -> MCPMarketSnapshot:
    """Pull quotes, historicals, and portfolio context for ``ticker``."""
    errors: List[str] = []
    quotes: Any = None
    hist: Any = None
    portfolio: Any = None

    # Robinhood's MCP validates arguments strictly and rejects any unknown
    # property, so each tool gets ONLY the keys its schema declares — no
    # belt-and-suspenders "symbol"+"symbols" (that fails with
    # 'unexpected additional properties ["symbol"]').
    for tool, args in (
        ("get_equity_quotes", {"symbols": [ticker]}),
        ("get_quotes", {"symbols": [ticker]}),
    ):
        try:
            quotes = await client.call_tool(tool, args)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tool}: {exc}")

    # Schema (from the live catalog): symbols[] + REQUIRED start_time
    # (RFC3339) + optional interval/end_time. 150 calendar days ≈ 100 daily
    # bars — enough for RSI(14), EMA(21), MACD(26,9) and the 60-day vol.
    start_time = (
        datetime.now(timezone.utc) - timedelta(days=150)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for tool in ("get_equity_historicals", "get_historicals"):
        try:
            hist = await client.call_tool(
                tool,
                {"symbols": [ticker], "start_time": start_time,
                 "interval": "day"},
            )
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tool}: {exc}")

    # get_portfolio requires an account_number (from get_accounts).
    try:
        accounts = await client.call_tool("get_accounts", {})
        account_number = _dig_account_number(accounts)
        if account_number:
            portfolio = await client.call_tool(
                "get_portfolio", {"account_number": account_number})
        else:
            errors.append("get_accounts: no account_number found in response")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"get_portfolio: {exc}")

    last_price = _dig_price(quotes)
    if last_price is None:
        closes = _closes_from_historicals(hist)
        if closes:
            last_price = closes[-1]

    closes = _closes_from_historicals(hist)
    rvol = realized_vol_from_closes(closes) if closes else None
    computed = indicators_from_closes(closes) if closes else {}

    equity = buying_power = None
    if isinstance(portfolio, dict):
        equity = _as_float(
            portfolio.get("equity")
            or portfolio.get("total_equity")
            or portfolio.get("market_value")
        )
        buying_power = _as_float(
            portfolio.get("buying_power")
            or portfolio.get("cash")
            or portfolio.get("withdrawable_amount")
        )
        # unwrap text
        if equity is None and "text" in portfolio:
            try:
                import json

                p2 = json.loads(portfolio["text"])
                if isinstance(p2, dict):
                    equity = _as_float(p2.get("equity") or p2.get("total_equity"))
                    buying_power = _as_float(p2.get("buying_power") or p2.get("cash"))
            except Exception:  # noqa: BLE001
                pass

    return MCPMarketSnapshot(
        ticker=ticker.upper(),
        last_price=last_price,
        realized_vol_annual=rvol,
        portfolio_equity=equity,
        buying_power=buying_power,
        computed_indicators=computed or None,
        raw_quotes=quotes if isinstance(quotes, dict) else {"raw": quotes},
        raw_historicals=hist if isinstance(hist, dict) else {"raw": hist},
        errors=errors,
    )


def validate_extraction(
    extraction: ChartExtractionResult,
    mcp: Optional[MCPMarketSnapshot],
    config: ChartVisionConfig,
    *,
    mcp_available: bool = True,
) -> ValidationResult:
    """
    Compare vision result to MCP snapshot; reject / down-weight / pass.

    Logs discrepancies via return value (caller should audit.record).
    """
    discs: List[ValidationDiscrepancy] = []
    notes: List[str] = []
    overall = float(extraction.confidence.overall)
    adj = overall
    ticker_confirmed = False
    price_rel_error: Optional[float] = None

    if not mcp_available or mcp is None:
        notes.append("MCP snapshot unavailable")
        if config.require_mcp:
            discs.append(
                ValidationDiscrepancy(
                    code="mcp_required",
                    message="MCP required but unavailable",
                    severity="error",
                )
            )
            return ValidationResult(
                status=ValidationStatus.REJECTED,
                overall_confidence=overall,
                adjusted_confidence=0.0,
                discrepancies=discs,
                ticker_confirmed=False,
                notes=notes,
            )
        return ValidationResult(
            status=ValidationStatus.SKIPPED,
            overall_confidence=overall,
            adjusted_confidence=overall * 0.8,
            discrepancies=discs,
            ticker_confirmed=False,
            notes=notes + ["validation skipped; confidence slightly reduced"],
        )

    # Ticker confirmation: quote payload mentions symbol OR we got a price back
    if mcp.last_price is not None:
        ticker_confirmed = True
        notes.append("ticker confirmed via MCP price presence")
    elif _dig_symbol_match(mcp.raw_quotes, extraction.ticker):
        ticker_confirmed = True
        notes.append("ticker confirmed via MCP quote symbol")
    else:
        discs.append(
            ValidationDiscrepancy(
                code="ticker_unconfirmed",
                message=f"Could not confirm ticker {extraction.ticker} via MCP",
                severity="error",
                image_value=extraction.ticker,
            )
        )

    # Price cross-check
    if (
        extraction.image_last_price is not None
        and mcp.last_price is not None
        and mcp.last_price > 0
    ):
        price_rel_error = abs(extraction.image_last_price - mcp.last_price) / mcp.last_price
        if price_rel_error > config.max_price_rel_error:
            discs.append(
                ValidationDiscrepancy(
                    code="price_mismatch",
                    message=(
                        f"image price {extraction.image_last_price} vs MCP "
                        f"{mcp.last_price} (rel err {price_rel_error:.2%}) "
                        f"> max {config.max_price_rel_error:.2%}"
                    ),
                    severity="error",
                    image_value=extraction.image_last_price,
                    mcp_value=mcp.last_price,
                )
            )
            adj *= 0.4
        elif price_rel_error > config.max_price_rel_error * 0.5:
            discs.append(
                ValidationDiscrepancy(
                    code="price_soft_mismatch",
                    message=f"moderate price discrepancy rel_err={price_rel_error:.2%}",
                    severity="warning",
                    image_value=extraction.image_last_price,
                    mcp_value=mcp.last_price,
                )
            )
            adj *= 0.75
        else:
            notes.append(f"price check OK rel_err={price_rel_error:.4%}")
            # Image price ok → slight boost to price confidence contribution
            adj = min(1.0, adj * 1.02)
    elif extraction.image_last_price is None:
        notes.append("no image_last_price to cross-check; MCP price authoritative")
        adj *= 0.95
    elif mcp.last_price is None:
        discs.append(
            ValidationDiscrepancy(
                code="mcp_price_missing",
                message="MCP returned no last price",
                severity="warning",
            )
        )
        adj *= 0.7

    # Indicator cross-check: Grok's RSI read vs RSI computed from real
    # closing prices. A vision misread cannot survive this — the computed
    # value is authoritative downstream regardless.
    computed_rsi = (mcp.computed_indicators or {}).get("rsi14")
    image_rsi = extraction.indicators.rsi.value
    if computed_rsi is not None and image_rsi is not None:
        rsi_diff = abs(float(image_rsi) - float(computed_rsi))
        if rsi_diff > 10.0:
            discs.append(
                ValidationDiscrepancy(
                    code="rsi_mismatch",
                    message=(f"image RSI {image_rsi:.0f} vs computed "
                             f"{computed_rsi:.0f} (diff {rsi_diff:.0f} > 10)"),
                    severity="warning",
                    image_value=image_rsi,
                    mcp_value=computed_rsi,
                )
            )
            adj *= 0.7
        else:
            notes.append(
                f"RSI check OK (image {image_rsi:.0f} ≈ computed "
                f"{computed_rsi:.0f})")

    # Confidence floors
    status = ValidationStatus.PASSED
    if not ticker_confirmed:
        status = ValidationStatus.REJECTED
        adj = 0.0
        notes.append("REJECTED: ticker not confirmed")
    elif any(d.code == "price_mismatch" for d in discs):
        status = ValidationStatus.REJECTED
        notes.append("REJECTED: material price mismatch")
    elif overall < config.min_overall_confidence:
        status = ValidationStatus.REJECTED
        adj = min(adj, overall)
        notes.append(
            f"REJECTED: overall confidence {overall:.2f} < "
            f"min {config.min_overall_confidence:.2f}"
        )
    elif overall < config.downweight_confidence or any(
        d.severity == "warning" for d in discs
    ):
        status = ValidationStatus.DOWNWEIGHTED
        adj = min(adj, overall * 0.7)
        notes.append("DOWNWEIGHTED: mid confidence or soft discrepancies")
    else:
        notes.append("PASSED validation")

    # Never let adjusted exceed overall after penalties (except tiny boost)
    adj = max(0.0, min(1.0, adj))

    for d in discs:
        logger.info(
            "chart_vision discrepancy %s: %s (image=%s mcp=%s)",
            d.code,
            d.message,
            d.image_value,
            d.mcp_value,
        )

    return ValidationResult(
        status=status,
        overall_confidence=overall,
        adjusted_confidence=adj,
        discrepancies=discs,
        price_rel_error=price_rel_error,
        ticker_confirmed=ticker_confirmed,
        notes=notes,
    )
