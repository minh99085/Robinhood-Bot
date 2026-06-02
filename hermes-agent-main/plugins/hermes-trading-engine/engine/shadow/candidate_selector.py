"""ShadowCandidateSelector — pick tradable markets from venue-neutral metadata.

Pure filtering; it never trades. Returns CandidateMarket objects with a
`selected` flag and `rejection_reason` so every consideration is auditable.

Quant scope — *Data Acquisition & Ingestion* + *Compliance*: candidate filtering
feeds the priority hierarchy (Bregman P1 > statistical P2 > directional P3). Each
rejected candidate carries a reason, so the no-trade diagnostics chain is
complete from selection through resolution.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from .config import ShadowConfig
from .schemas import CandidateMarket


def _dec(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


class ShadowCandidateSelector:
    def __init__(self, config: ShadowConfig):
        self.cfg = config

    def evaluate(self, *, venue: str, market_id: Optional[str] = None,
                 market_ticker: Optional[str] = None, asset_id: Optional[str] = None,
                 outcome: str = "YES", question: str = "", category: Optional[str] = None,
                 close_ts_ms: Optional[int] = None, spread: Optional[float] = None,
                 volume: Optional[float] = None, open_interest: Optional[float] = None,
                 ambiguity_score: Optional[float] = None, liquidity_score: Optional[float] = None,
                 metadata_complete: bool = True, data_fresh: bool = True,
                 resolution_present: bool = True, venue_enabled: bool = True,
                 tradable: bool = True, now_ms: Optional[int] = None) -> CandidateMarket:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        cand = CandidateMarket(
            shadow_session_id="", ts_ms=now, venue=venue, market_id=market_id,
            market_ticker=market_ticker, asset_id=asset_id, outcome=outcome, question=question,
            category=category, close_ts_ms=close_ts_ms, spread=_dec(spread), volume=_dec(volume),
            open_interest=_dec(open_interest), ambiguity_score=ambiguity_score,
            liquidity_score=liquidity_score, metadata_complete=metadata_complete,
            data_fresh=data_fresh)
        reason = self._reject_reason(cand, venue_enabled, tradable, resolution_present, now)
        cand.rejection_reason = reason
        cand.selected = reason is None
        return cand

    def _reject_reason(self, c: CandidateMarket, venue_enabled: bool, tradable: bool,
                       resolution_present: bool, now_ms: int) -> Optional[str]:
        cfg = self.cfg
        if not venue_enabled:
            return "venue_disabled"
        if c.venue not in cfg.venues:
            return "venue_not_in_scope"
        if not tradable:
            return "market_not_tradable"
        if not c.data_fresh:
            return "market_data_stale"
        if not c.metadata_complete:
            return "metadata_incomplete"
        if cfg.require_resolution_rules and not resolution_present:
            return "resolution_rules_missing"
        if c.ambiguity_score is not None and c.ambiguity_score > cfg.max_ambiguity_score:
            return "high_ambiguity"
        if c.close_ts_ms is not None and (c.close_ts_ms - now_ms) < cfg.min_time_to_close_seconds * 1000:
            return "close_too_near"
        if c.spread is not None and float(c.spread) > cfg.max_spread:
            return "excessive_spread"
        if c.liquidity_score is not None and c.liquidity_score < cfg.min_liquidity_score:
            return "insufficient_liquidity"
        if c.volume is not None and c.volume < cfg.min_volume:
            return "insufficient_volume"
        if c.open_interest is not None and c.open_interest < cfg.min_open_interest:
            return "insufficient_open_interest"
        if cfg.category_whitelist and (c.category or "") not in cfg.category_whitelist:
            return "category_not_whitelisted"
        if c.category and c.category in cfg.category_blacklist:
            return "category_blacklisted"
        return None

    def select(self, items: list[dict], now_ms: Optional[int] = None) -> list[CandidateMarket]:
        out: list[CandidateMarket] = []
        per_venue: dict[str, int] = {}
        for it in items:
            cand = self.evaluate(now_ms=now_ms, **it)
            if cand.selected:
                v = cand.venue
                if per_venue.get(v, 0) >= self.cfg.max_candidates_per_venue:
                    cand.selected = False
                    cand.rejection_reason = "venue_candidate_cap"
                else:
                    per_venue[v] = per_venue.get(v, 0) + 1
            out.append(cand)
            if sum(1 for c in out if c.selected) >= self.cfg.max_candidates_per_cycle:
                break
        return out
