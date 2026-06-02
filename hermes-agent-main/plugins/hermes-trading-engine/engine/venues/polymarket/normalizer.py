"""Normalize Polymarket metadata dicts into venue-neutral schemas (Phase 6).

Thin wrapper — does NOT touch the existing engine/market_data/polymarket_ws.py
read-only feed. It only maps already-fetched metadata into VenueMarketMetadata.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..metadata import SettlementSource, VenueMarketMetadata, payload_hash


def _d(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def normalize_market(raw: dict) -> VenueMarketMetadata:
    raw = raw or {}
    sources = []
    if raw.get("resolution_source"):
        sources.append(SettlementSource(name=str(raw.get("resolution_source")),
                                        source_type="market_resolution_source"))
    return VenueMarketMetadata(
        venue="polymarket", market_id=str(raw.get("market_id") or raw.get("id") or ""),
        asset_id=raw.get("asset_id") or raw.get("token_id"),
        question=raw.get("question") or raw.get("title") or "",
        title=raw.get("title"), outcomes=list(raw.get("outcomes") or ["YES", "NO"]),
        category=raw.get("category"), tags=list(raw.get("tags") or []),
        status=raw.get("status") or ("open" if raw.get("active") else "unknown"),
        close_ts_ms=raw.get("close_ts_ms"), last_price=_d(raw.get("last_price")),
        min_tick_size=_d(raw.get("min_tick_size")),
        settlement_sources=sources, contract_url=raw.get("market_page") or raw.get("url"),
        fee_metadata={k: raw[k] for k in ("rules_primary", "description") if raw.get(k)},
        raw_payload_hash=payload_hash(raw))
