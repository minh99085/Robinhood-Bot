"""Adaptive market-universe selection for prediction-market venues.

This package scans a large catalog of markets, filters out untradable ones,
scores the rest, and produces tiers (A=trade candidates, B=live watchlist,
C=periodic refresh, D=ignored). It is a *selection* layer only — it never
places, cancels, or sizes an order. The existing RiskEngine + paper OMS remain
the only execution path, and live order-book subscription stays gated behind
``POLYMARKET_CLOB_ENABLED`` (disabled by default).
"""

from .universe_manager import (  # noqa: F401
    UniverseConfig,
    UniverseManager,
    MarketRecord,
    build_universe,
    passes_filters,
    score_market,
)
