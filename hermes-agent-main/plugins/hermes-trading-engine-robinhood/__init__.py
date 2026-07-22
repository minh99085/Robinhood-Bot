"""Hermes plugin entry — register chart vision tools with the agent runtime.

Also exposes HTTP tools on the Robinhood API service (port 8810):
  POST /api/chart/analyze
  POST /api/chart/extract
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure plugin root is importable when Hermes loads this package by path.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def register(ctx) -> None:
    """Called by Hermes plugin loader when enabled in config."""
    from tools import (
        ANALYZE_TRADINGVIEW_CHART_SCHEMA,
        check_chart_vision_requirements,
        handle_analyze_tradingview_chart,
    )

    ctx.register_tool(
        name="analyze_tradingview_chart",
        toolset="robinhood_chart_vision",
        schema=ANALYZE_TRADINGVIEW_CHART_SCHEMA,
        handler=handle_analyze_tradingview_chart,
        check_fn=check_chart_vision_requirements,
        emoji="📊",
    )
    logger.info("Registered analyze_tradingview_chart tool")
