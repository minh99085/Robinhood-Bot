"""ReplayResearchCache — read-only, deterministic estimate lookup for replay.

During replay the agent NEVER calls xAI. This cache returns the latest cached
probability estimate at or before a replay timestamp. If none exists it returns
None (the policy then skips or uses a deterministic fallback) — it never falls
back to the network.
"""

from __future__ import annotations

from typing import Optional


class ReplayResearchCache:
    def __init__(self, store):
        self.store = store

    def latest_estimate(self, *, venue: str, market_id: str, asset_id: Optional[str],
                        at_ts_ms: int) -> Optional[dict]:
        if self.store is None:
            return None
        return self.store.get_latest_estimate_before(venue, market_id, asset_id, at_ts_ms)

    def snapshot_probabilities(self, *, venue: str, pairs: list[tuple],
                               at_ts_ms: int) -> dict:
        """Build {asset_id|market_id: p_ensemble} for preloading a ReplayPolicy's
        ``cached_probabilities`` deterministically (no network). Only tradeable
        (present, not no-trade, not stale) estimates are included."""
        out: dict = {}
        for market_id, asset_id in pairs:
            est = self.latest_estimate(venue=venue, market_id=market_id,
                                       asset_id=asset_id, at_ts_ms=at_ts_ms)
            if not self.is_tradeable(est, at_ts_ms):
                continue
            try:
                p = float(est["p_ensemble"])
            except (TypeError, ValueError, KeyError):
                continue
            out[asset_id or market_id] = p
        return out

    @staticmethod
    def is_tradeable(est: Optional[dict], at_ts_ms: int) -> bool:
        """A cached estimate is usable only if present, not no-trade, and not stale
        relative to the (replay) clock."""
        if not est:
            return False
        if est.get("no_trade_reason"):
            return False
        try:
            stale_after = int(est.get("stale_after_ts_ms") or 0)
        except (TypeError, ValueError):
            return False
        if stale_after and at_ts_ms > stale_after:
            return False
        return est.get("p_ensemble") is not None
