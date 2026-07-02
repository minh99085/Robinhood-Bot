"""Options watchlist scan loop — manual bias, review-before-place."""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import OPTIONS_TOOLS
from engine.robinhood.options_ledger import append_event, write_status
from engine.robinhood.options_market import load_symbol_snapshot
from engine.robinhood.options_strategy import OrderIntent, decide_order

logger = logging.getLogger("hermes.robinhood.options_loop")


def _intent_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "option_type": intent.option_type,
        "instrument_id": intent.instrument_id,
        "quantity": intent.quantity,
        "limit_price": intent.limit_price,
        "premium_usd": intent.premium_usd,
        "expiration_date": intent.expiration_date,
        "strike": intent.strike,
        "bias": intent.bias,
        "reason": intent.reason,
    }


def _active_symbols(config: RobinhoodConfig) -> list[tuple[str, str]]:
    """Symbols with a non-none manual bias (global or per-symbol)."""
    out: list[tuple[str, str]] = []
    for sym in config.options_watchlist:
        bias = config.bias_for(sym)
        if bias != "none":
            out.append((sym.upper(), bias))
    return out


async def run_options_tick(client: Any, config: RobinhoodConfig) -> dict[str, Any]:
    """One scan pass across biased watchlist symbols. Returns status summary."""
    started = time.time()
    funnel: Counter[str] = Counter()
    results: list[dict[str, Any]] = []
    tools = set(await client.list_tools())
    missing = sorted(OPTIONS_TOOLS - tools)
    if missing:
        payload = {
            "loop_enabled": config.options_loop_enabled,
            "available": False,
            "reason": "options_tools_missing",
            "missing_tools": missing,
            "tool_count": len(tools),
            "active_symbols": [],
            "results": [],
            "funnel": dict(funnel),
            "duration_s": round(time.time() - started, 2),
        }
        write_status(config.data_dir, payload)
        append_event(
            config.data_dir,
            {"type": "scan_blocked", "reason": "options_tools_missing", "missing": missing},
        )
        return payload

    active = _active_symbols(config)
    if not active:
        payload = {
            "loop_enabled": config.options_loop_enabled,
            "available": True,
            "reason": "no_active_bias",
            "hint": "Set RH_OPTIONS_BIAS=call|put or RH_OPTIONS_BIAS_SPY=call",
            "watchlist_size": len(config.options_watchlist),
            "active_symbols": [],
            "results": [],
            "funnel": {"no_active_bias": 1},
            "duration_s": round(time.time() - started, 2),
        }
        write_status(config.data_dir, payload)
        return payload

    for symbol, bias in active:
        row: dict[str, Any] = {"symbol": symbol, "bias": bias}
        try:
            snapshot = await load_symbol_snapshot(client, symbol, config, bias)
            if not snapshot:
                funnel["no_market_data"] += 1
                row["stage"] = "no_market_data"
                results.append(row)
                continue
            intent, stage = decide_order(snapshot, config, bias)
            funnel[stage] += 1
            row["stage"] = stage
            row["spot"] = snapshot.spot.last_price
            if not intent:
                results.append(row)
                continue
            row["intent"] = _intent_dict(intent)
            args = intent.to_mcp_args()
            if not config.live_trading_enabled:
                row["action"] = "paper_logged"
                append_event(
                    config.data_dir,
                    {"type": "paper_intent", **_intent_dict(intent)},
                )
                results.append(row)
                continue
            try:
                await client.call_tool("place_option_order", args)
                row["action"] = "placed"
                append_event(
                    config.data_dir,
                    {"type": "order_placed", **_intent_dict(intent)},
                )
            except PermissionError as exc:
                row["action"] = "safety_blocked"
                row["error"] = str(exc)
                funnel["safety_blocked"] += 1
                append_event(
                    config.data_dir,
                    {
                        "type": "safety_blocked",
                        "error": str(exc),
                        **_intent_dict(intent),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                row["action"] = "place_failed"
                row["error"] = str(exc)
                funnel["place_failed"] += 1
                append_event(
                    config.data_dir,
                    {"type": "place_failed", "error": str(exc), **_intent_dict(intent)},
                )
        except Exception as exc:  # noqa: BLE001
            funnel["symbol_error"] += 1
            row["stage"] = "symbol_error"
            row["error"] = str(exc)
            logger.warning("options scan %s failed: %s", symbol, exc)
        results.append(row)

    payload = {
        "loop_enabled": config.options_loop_enabled,
        "available": True,
        "live_trading_enabled": config.live_trading_enabled,
        "watchlist_size": len(config.options_watchlist),
        "active_symbols": [{"symbol": s, "bias": b} for s, b in active],
        "results": results,
        "funnel": dict(funnel),
        "duration_s": round(time.time() - started, 2),
    }
    write_status(config.data_dir, payload)
    append_event(
        config.data_dir,
        {
            "type": "scan_complete",
            "active_count": len(active),
            "funnel": dict(funnel),
            "duration_s": payload["duration_s"],
        },
    )
    return payload
