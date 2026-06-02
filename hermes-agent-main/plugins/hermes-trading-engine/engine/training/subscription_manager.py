"""CLOB subscription manager (selection-only).

Decides which Polymarket assets to keep subscribed (top `live_watch_limit`),
limits subscription churn, and tracks CLOB health. It NEVER places orders and
NEVER signs anything — it only manages read-only market-data subscriptions
through the existing CLOB layer (gated by ``POLYMARKET_CLOB_ENABLED``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubscriptionHealth:
    subscribed_assets: int = 0
    desired_assets: int = 0
    added_assets: int = 0
    removed_assets: int = 0
    churn_count: int = 0
    stale_books: int = 0
    missing_bbo_count: int = 0
    avg_bbo_age_ms: float = 0.0
    avg_spread: float = 0.0
    avg_depth_near_touch: float = 0.0
    reconnect_count: int = 0
    parse_errors: int = 0
    messages_received: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 3)
        return d


class SubscriptionManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.subscribed: set = set()
        self.asset_to_market: dict = {}
        self.health = SubscriptionHealth()

    def reconcile(self, desired_records) -> SubscriptionHealth:
        """desired_records = ranked MarketRecords (best first). Subscribe to the
        top `live_watch_limit` assets, capping churn per refresh."""
        limit = int(self.cfg.live_watch_limit)
        max_churn = int(self.cfg.max_subscription_churn)
        desired: list = []
        amap = {}
        for rec in desired_records:
            for tok in rec.clob_token_ids:
                desired.append(tok)
                amap[tok] = rec.market_id
            if len(desired) >= limit:
                break
        desired_set = set(desired[:limit])

        to_add = list(desired_set - self.subscribed)
        to_remove = list(self.subscribed - desired_set)
        # cap churn to avoid thrash
        budget = max_churn
        to_add = to_add[:budget]
        to_remove = to_remove[:max(0, budget - len(to_add))]

        for tok in to_add:
            self.subscribed.add(tok)
            self.asset_to_market[tok] = amap.get(tok)
        for tok in to_remove:
            self.subscribed.discard(tok)
            self.asset_to_market.pop(tok, None)

        h = self.health
        h.desired_assets = len(desired_set)
        h.subscribed_assets = len(self.subscribed)
        h.added_assets = len(to_add)
        h.removed_assets = len(to_remove)
        h.churn_count = len(to_add) + len(to_remove)
        # book-quality aggregates over the desired records
        recs = list(desired_records)[: max(1, limit)]
        if recs:
            spreads = [r.spread for r in recs]
            depths = [r.top_depth_usd for r in recs]
            ages = [r.book_age_s for r in recs if r.book_age_s is not None]
            stale = [r for r in recs if (r.book_age_s or 0) * 1000.0 > self.cfg.clob_stale_ms]
            missing = [r for r in recs if r.spread <= 0]
            h.avg_spread = sum(spreads) / len(spreads)
            h.avg_depth_near_touch = sum(depths) / len(depths)
            h.avg_bbo_age_ms = (sum(ages) / len(ages) * 1000.0) if ages else 0.0
            h.stale_books = len(stale)
            h.missing_bbo_count = len(missing)
        return h
