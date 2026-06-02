"""Venue execution mappers (Phase 8) — payload mapping ONLY, never execution.

These build a *would-be* venue order payload for validation/conformance. They do
NOT sign, do NOT POST, do NOT import any wallet/signer or venue order SDK, and
mark every intent UNSIGNED_DRY_RUN_ONLY / UNSENT_DRY_RUN_ONLY.
"""

from .kalshi_mapper import map_order as map_kalshi_order
from .polymarket_mapper import map_order as map_polymarket_order

__all__ = ["map_polymarket_order", "map_kalshi_order", "get_mapper"]


def get_mapper(venue: str):
    return map_kalshi_order if str(venue).lower() == "kalshi" else map_polymarket_order
