"""Algorithmic feature builders for the Hermes Trading Engine (PAPER ONLY).

This package holds *pure* feature transforms over already-collected market data.
It never performs I/O, never trades, and never touches a wallet or a live order
path. Feed acquisition (Chainlink oracle, Coinbase fast price) lives in
``engine.training.chainlink_oracle`` and ``engine.feeds.btc_fast_price`` and is
intentionally NOT modified here — this layer only consumes their read-only
outputs and derives features.
"""

from __future__ import annotations
