"""SourceCache — TTL cache of source metadata + small excerpts (no full articles).

Deduplicates by normalized URL / content hash. Tracks credibility, source type,
and freshness. Pure in-memory; no network.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_CRED = {
    "official": 0.95, "government": 0.95, "exchange": 0.9,
    "market_resolution_source": 0.9, "academic": 0.85, "news": 0.6,
    "market_page": 0.5, "social_x": 0.3, "unknown": 0.3,
}


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parts.path.rstrip("/")
        return urlunsplit((scheme, netloc, path, "", ""))  # drop query + fragment
    except Exception:  # noqa: BLE001
        return url.strip().lower()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:24]


class SourceCache:
    def __init__(self, ttl_seconds: int = 900, no_network: bool = True):
        self.ttl_ms = int(ttl_seconds * 1000)
        self.no_network = no_network
        self._by_url: dict[str, dict] = {}
        self._by_hash: dict[str, dict] = {}

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def add_source(self, *, url: str | None = None, source_type: str = "unknown",
                   title: str | None = None, excerpt: str | None = None,
                   credibility: float | None = None,
                   published_ts_ms: int | None = None) -> dict:
        nurl = normalize_url(url) if url else ""
        chash = content_hash(excerpt or title or nurl)
        existing = (self._by_url.get(nurl) if nurl else None) or self._by_hash.get(chash)
        if existing is not None:
            return existing  # dedup
        rec = {
            "source_id": "src-" + uuid.uuid4().hex[:16],
            "source_type": source_type,
            "normalized_url": nurl or None,
            "title": (title or None),
            "credibility": credibility if credibility is not None else _DEFAULT_CRED.get(source_type, 0.3),
            "content_hash": chash,
            "short_excerpt": (excerpt or None) if excerpt is None else str(excerpt)[:500],
            "retrieved_ts_ms": self._now_ms(),
            "published_ts_ms": published_ts_ms,
        }
        if nurl:
            self._by_url[nurl] = rec
        self._by_hash[chash] = rec
        return rec

    def get(self, url: str) -> dict | None:
        rec = self._by_url.get(normalize_url(url))
        if rec is None:
            return None
        if (self._now_ms() - rec["retrieved_ts_ms"]) > self.ttl_ms:
            return None  # expired
        return rec

    def freshness(self, rec: dict) -> float:
        age = self._now_ms() - rec.get("retrieved_ts_ms", 0)
        if self.ttl_ms <= 0:
            return 1.0
        return max(0.0, 1.0 - age / self.ttl_ms)

    def size(self) -> int:
        return len(self._by_hash)
