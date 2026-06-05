"""Algorithmic feature builders for the Hermes Trading Engine (PAPER ONLY).

This package holds *pure* feature transforms over already-collected market data.
It never performs I/O, never trades, and never touches a wallet or a live order
path. Feed acquisition (Chainlink oracle, Coinbase fast price) lives in
``engine.training.chainlink_oracle`` and ``engine.feeds.btc_fast_price`` and is
intentionally NOT modified here — this layer only consumes their read-only
outputs and derives features.

Backwards-compatible re-exports: the legacy pulse microstructure features and
the online logistic learner used to live in ``engine/features.py``. That module
was merged into this package as :mod:`engine.features._pulse` and re-exported
here so existing imports (``from engine.features import N_FEATURES, ...``) keep
working alongside the newer :mod:`engine.features.oracle_features`.
"""

from __future__ import annotations

from ._pulse import N_FEATURES, OnlineLogistic, pulse_features

__all__ = ["N_FEATURES", "OnlineLogistic", "pulse_features"]
