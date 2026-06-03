"""Training-package alias for the Chainlink scanner.

Quant responsibilities — *Data Acquisition & Ingestion*, *Signal Generation*,
*Risk Management* (no-trade flags), *Backtesting* (replay-safe snapshots). The
canonical implementation lives in :mod:`engine.chainlink_scanner`; this module
re-exports it under the training-package path so callers can use
``engine.training.chainlink_scanner`` without duplicating logic. Chainlink is
read-only, advisory-only, and can never directly trigger a trade.
"""

from __future__ import annotations

from engine.chainlink_scanner import (ChainlinkScanner, ChainlinkSignal,
                                      ChainlinkScanSnapshot, LINK_MIN_RELEVANCE,
                                      MAX_PROB_ADJUSTMENT)

# Chainlink scanning is READ-ONLY + advisory-only (never triggers a trade).
READ_ONLY = True

__all__ = ["ChainlinkScanner", "ChainlinkSignal", "ChainlinkScanSnapshot",
           "LINK_MIN_RELEVANCE", "MAX_PROB_ADJUSTMENT", "READ_ONLY"]
