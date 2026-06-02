"""Venue-neutral resolution-rule construction + ambiguity scoring.

Turns VenueMarketMetadata (+ optional series) into a ResolutionRuleSet that the
Phase 5 research engine consumes. Reuses the research AmbiguityScorer so Kalshi
and Polymarket are scored identically.
"""

from __future__ import annotations

import time
from typing import Optional

from .metadata import (
    ResolutionRuleSet,
    VenueMarketMetadata,
    VenueSeriesMetadata,
    payload_hash,
)

try:
    from ..research.ambiguity import AmbiguityScorer
except Exception:  # noqa: BLE001 — research package optional at import time
    AmbiguityScorer = None  # type: ignore


def build_resolution_ruleset(meta: VenueMarketMetadata,
                             series: Optional[VenueSeriesMetadata] = None,
                             outcome: Optional[str] = None) -> ResolutionRuleSet:
    sources = list(meta.settlement_sources)
    if series is not None:
        sources = sources + list(series.settlement_sources)
    rules_primary = meta.fee_metadata.get("rules_primary") if isinstance(meta.fee_metadata, dict) else None
    rules_secondary = meta.fee_metadata.get("rules_secondary") if isinstance(meta.fee_metadata, dict) else None
    text = " ".join(filter(None, [meta.question, meta.title, rules_primary, rules_secondary]))

    score, categories = 0.0, []
    if AmbiguityScorer is not None:
        score, categories = AmbiguityScorer().score(text, {
            "resolution_source": (sources[0].name if sources else None),
            "close_ts_ms": meta.close_ts_ms,
            "resolution_deadline_ts_ms": meta.latest_expiration_ts_ms,
        })
    if not sources:
        # No settlement source at all is itself ambiguous.
        if "unclear_resolution_source" not in categories:
            categories = sorted(set(categories + ["unclear_resolution_source"]))
        score = min(1.0, score + 0.2)

    return ResolutionRuleSet(
        venue=meta.venue, market_id=meta.market_id, market_ticker=meta.market_ticker,
        asset_id=meta.asset_id, event_ticker=meta.event_ticker,
        series_ticker=meta.series_ticker, question=meta.question, outcome=outcome,
        rules_primary=rules_primary, rules_secondary=rules_secondary,
        settlement_sources=sources, contract_url=meta.contract_url,
        contract_terms_url=meta.contract_terms_url, close_ts_ms=meta.close_ts_ms,
        latest_expiration_ts_ms=meta.latest_expiration_ts_ms, can_close_early=meta.can_close_early,
        ambiguity_categories=categories, ambiguity_score=round(score, 4),
        parsed_ts_ms=int(time.time() * 1000),
        raw_text_hash=payload_hash(text) if text else None)
