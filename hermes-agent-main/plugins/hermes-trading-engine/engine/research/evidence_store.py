"""EvidenceStore — persist evidence + sources, linked to runs and estimates.

Stores only SHORT excerpts (never full articles). Deduplicates sources by
normalized URL / content hash via SourceCache. All persistence is best-effort
and isolated to the research_* tables.
"""

from __future__ import annotations

from typing import Optional

from .schemas import EvidenceItem
from .source_cache import SourceCache, content_hash, normalize_url


class EvidenceStore:
    def __init__(self, store, source_cache: Optional[SourceCache] = None):
        self.store = store
        self.cache = source_cache or SourceCache()

    def persist_source(self, item: EvidenceItem) -> Optional[str]:
        rec = self.cache.add_source(
            url=item.source_url, source_type=item.source_type,
            title=item.source_title, excerpt=item.short_excerpt,
            credibility=item.credibility, published_ts_ms=item.published_ts_ms)
        source_id = rec["source_id"]
        if self.store is not None:
            self.store.upsert_research_source({
                "source_id": source_id, "source_type": item.source_type,
                "normalized_url": normalize_url(item.source_url) if item.source_url else None,
                "title": item.source_title, "publisher": None, "author": None,
                "published_ts_ms": item.published_ts_ms,
                "retrieved_ts_ms": item.retrieved_ts_ms,
                "credibility": str(item.credibility),
                "content_hash": content_hash(item.short_excerpt or item.source_title or ""),
                "payload_json": {"direction": item.direction},
            })
        return source_id

    def persist_evidence(self, item: EvidenceItem, *, research_run_id: str,
                         estimate_id: Optional[str], venue: str, market_id: str,
                         asset_id: Optional[str]) -> None:
        source_id = item.source_id or self.persist_source(item)
        if self.store is None:
            return
        self.store.add_research_evidence({
            "evidence_id": item.evidence_id, "research_run_id": research_run_id,
            "estimate_id": estimate_id, "source_id": source_id, "venue": venue,
            "market_id": market_id, "asset_id": asset_id, "claim": item.claim,
            "short_excerpt": item.short_excerpt, "direction": item.direction,
            "weight": str(item.weight), "credibility": str(item.credibility),
            "freshness": str(item.freshness), "relevance": str(item.relevance),
            "payload_json": {"source_type": item.source_type, "source_url": item.source_url},
        })

    def persist_all(self, items: list[EvidenceItem], *, research_run_id: str,
                    estimate_id: Optional[str], venue: str, market_id: str,
                    asset_id: Optional[str]) -> int:
        n = 0
        for it in items or []:
            try:
                self.persist_evidence(it, research_run_id=research_run_id,
                                      estimate_id=estimate_id, venue=venue,
                                      market_id=market_id, asset_id=asset_id)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return n

    def get_cached_evidence(self, *, research_run_id: Optional[str] = None,
                            estimate_id: Optional[str] = None) -> list[dict]:
        if self.store is None:
            return []
        return self.store.get_research_evidence(
            research_run_id=research_run_id, estimate_id=estimate_id)
