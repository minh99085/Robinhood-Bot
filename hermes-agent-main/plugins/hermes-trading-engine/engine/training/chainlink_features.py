"""Training-package alias for Chainlink feature engineering.

Quant responsibility — *Data Preprocessing & Feature Engineering*. The canonical
implementation lives in :mod:`engine.features_chainlink`; this module re-exports
it under the training-package path so callers can use
``engine.training.chainlink_features`` without a second, divergent implementation.
"""

from __future__ import annotations

from engine.features_chainlink import ChainlinkFeatures, compute_features

__all__ = ["ChainlinkFeatures", "compute_features"]
