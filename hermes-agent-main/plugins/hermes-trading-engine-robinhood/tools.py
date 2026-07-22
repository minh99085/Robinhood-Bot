"""Hermes agent-facing tools for the Robinhood chart vision plugin."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.pipeline import run_full_pipeline
from engine.robinhood.audit_log import AuditLog
from engine.robinhood.config import RobinhoodConfig

logger = logging.getLogger("hermes.robinhood.tools")

# Lazy client holder for the agent process (optional).
_mcp_client: Any = None
_audit: Optional[AuditLog] = None


def set_mcp_client(client: Any) -> None:
    """Inject SafeRobinhoodClient (or mock) for tool handlers."""
    global _mcp_client
    _mcp_client = client


def set_audit(audit: AuditLog) -> None:
    global _audit
    _audit = audit


def check_chart_vision_requirements() -> bool:
    cfg = ChartVisionConfig.from_env()
    return bool(cfg.enabled)


ANALYZE_TRADINGVIEW_CHART_SCHEMA: Dict[str, Any] = {
    "name": "analyze_tradingview_chart",
    "description": (
        "Analyze a TradingView chart image (PNG/JPEG via path, URL, or base64). "
        "Extracts structured ticker/timeframe/indicators/levels/bias with confidence, "
        "cross-validates against Robinhood MCP quotes when available, maps parameters "
        "into the Monte Carlo tactical engine (default 100,000 paths), and returns a "
        "risk-aware trade decision. Image data is SECONDARY to TradingView webhooks "
        "and MCP prices — never places orders by itself. Any execution must still "
        "pass SafeRobinhoodClient safety gates and review_* tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Local filesystem path to a chart PNG/JPEG.",
            },
            "image_url": {
                "type": "string",
                "description": "HTTP(S) URL or data: URL of the chart image.",
            },
            "image_base64": {
                "type": "string",
                "description": "Raw base64 (or data URL) image payload.",
            },
            "mime_type": {
                "type": "string",
                "description": "Optional MIME type (image/png, image/jpeg).",
            },
            "ticker_hint": {
                "type": "string",
                "description": "Optional symbol hint if the chart label is ambiguous.",
            },
            "run_validation": {
                "type": "boolean",
                "description": "Cross-check with Robinhood MCP (default true).",
            },
            "run_monte_carlo": {
                "type": "boolean",
                "description": "Run tactical MC after extraction (default from env).",
            },
            "mc_paths": {
                "type": "integer",
                "description": "Monte Carlo paths (default 100000).",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["log_only", "recommendation_only", "gated_execution"],
                "description": (
                    "log_only / recommendation_only (default) / gated_execution. "
                    "Does not bypass safety gates."
                ),
            },
        },
        "additionalProperties": False,
    },
}


def _run_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Nested: create a new loop in a thread is complex; use asyncio.run in thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def handle_analyze_tradingview_chart(args: Dict[str, Any] | None = None, **kwargs: Any) -> str:
    """
    Hermes tool handler. Accepts either a single args dict or keyword arguments.
    Returns a JSON string for the agent.
    """
    payload = dict(args or {})
    payload.update(kwargs)

    if not any(payload.get(k) for k in ("image_path", "image_url", "image_base64")):
        return json.dumps(
            {
                "ok": False,
                "error": "Provide image_path, image_url, or image_base64",
            }
        )

    cfg = ChartVisionConfig.from_env()
    audit = _audit
    if audit is None:
        try:
            rh = RobinhoodConfig.from_env()
            audit = AuditLog(rh.data_dir)
        except Exception:  # noqa: BLE001
            audit = None

    resp = _run_async(
        run_full_pipeline(
            image_base64=payload.get("image_base64"),
            image_url=payload.get("image_url"),
            image_path=payload.get("image_path"),
            mime_type=payload.get("mime_type"),
            ticker_hint=payload.get("ticker_hint"),
            run_validation=bool(payload.get("run_validation", True)),
            run_monte_carlo=payload.get("run_monte_carlo"),
            mc_paths=payload.get("mc_paths"),
            execution_mode=payload.get("execution_mode"),
            config=cfg,
            mcp_client=_mcp_client,
            audit=audit,
        )
    )
    return resp.model_dump_json()


# Alias used by some Hermes loaders
analyze_tradingview_chart = handle_analyze_tradingview_chart
