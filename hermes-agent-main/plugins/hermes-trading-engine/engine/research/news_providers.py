"""News provider abstraction for the controlled evidence scanner.

A provider answers ``search(query, market_context) -> list[NewsEvidenceItem]``.
Modes:

* ``offline_cache``  — read previously-cached, timestamped items only (default).
* ``fixture``        — deterministic in-memory items (tests / replay fixtures).
* ``live_read_only`` — read-only HTTP fetch, *opt-in only*, never in tests/replay,
                       never sends secrets/wallet/positions/order state.

Providers are READ-ONLY: they never place, size, approve, or cancel a trade and
never receive account/wallet/position/order data — only public market context.
"""

from __future__ import annotations

from typing import Callable, Optional

from .news_schemas import NewsEvidenceItem

# Context keys a provider is ALLOWED to receive. Anything else (wallet, balances,
# positions, sizes, order state, secrets) is never forwarded to a provider.
SAFE_CONTEXT_KEYS = (
    "market_id", "question", "slug", "category", "description",
    "resolution_source", "close_ts_ms", "outcome", "asset_keywords",
)


def safe_market_context(market_ctx: dict) -> dict:
    """Project a market context down to provider-safe public fields only."""
    src = market_ctx or {}
    return {k: src.get(k) for k in SAFE_CONTEXT_KEYS if src.get(k) is not None}


class NewsProvider:
    """Abstract provider interface."""

    mode = "offline_cache"
    read_only = True

    def search(self, query: str, market_context: dict) -> list:
        raise NotImplementedError

    def health(self) -> dict:
        return {"mode": self.mode, "ok": True, "read_only": bool(self.read_only)}


class FixtureProvider(NewsProvider):
    """Deterministic provider backed by in-memory fixtures. For tests/replay.

    ``items`` may be a flat list (returned for every query) or a dict keyed by
    query string. Items may be ``NewsEvidenceItem`` or plain dicts.
    """

    mode = "fixture"
    read_only = True

    def __init__(self, items=None, *, by_query: Optional[dict] = None,
                 fail: bool = False):
        self._items = items or []
        self._by_query = by_query or {}
        self._fail = bool(fail)

    def search(self, query: str, market_context: dict) -> list:
        if self._fail:
            raise RuntimeError("fixture provider forced failure")
        raw = self._by_query.get(query, self._items)
        out = []
        for r in raw:
            out.append(_coerce_item(r, query, market_context))
        return out

    def health(self) -> dict:
        return {"mode": self.mode, "ok": not self._fail, "read_only": True}


class OfflineCacheProvider(NewsProvider):
    """Provider that only returns previously cached, timestamped items.

    ``cache`` is a mapping ``market_id -> list[NewsEvidenceItem|dict]`` (or a
    callable ``market_id -> list``). Never performs any network I/O.
    """

    mode = "offline_cache"
    read_only = True

    def __init__(self, cache=None):
        self._cache = cache if cache is not None else {}

    def _lookup(self, market_id: str) -> list:
        if callable(self._cache):
            return list(self._cache(market_id) or [])
        return list((self._cache or {}).get(market_id, []) or [])

    def search(self, query: str, market_context: dict) -> list:
        market_id = str(market_context.get("market_id") or "")
        out = []
        for r in self._lookup(market_id):
            it = _coerce_item(r, query, market_context)
            out.append(it)
        return out


class LiveReadOnlyProvider(NewsProvider):
    """Opt-in, read-only live provider.

    Disabled by default and **never** used in tests or replay. A ``fetch``
    callable (``fetch(query, safe_ctx) -> list[dict]``) must be injected
    explicitly; without it the provider returns nothing (fail-closed). It only
    ever receives provider-safe public context (no secrets/wallet/positions).
    """

    mode = "live_read_only"
    read_only = True

    def __init__(self, fetch: Optional[Callable[[str, dict], list]] = None, *,
                 enabled: bool = False):
        self._fetch = fetch
        self.enabled = bool(enabled) and fetch is not None

    def search(self, query: str, market_context: dict) -> list:
        if not self.enabled or self._fetch is None:
            return []
        from .news_providers import safe_market_context as _safe
        raw = self._fetch(query, _safe(market_context)) or []
        return [_coerce_item(r, query, market_context) for r in raw]

    def health(self) -> dict:
        return {"mode": self.mode, "ok": True, "read_only": True,
                "enabled": self.enabled}


def _coerce_item(r, query: str, market_context: dict) -> NewsEvidenceItem:
    market_id = str(market_context.get("market_id") or "")
    if isinstance(r, NewsEvidenceItem):
        if not r.market_id:
            r.market_id = market_id
        if not r.query:
            r.query = query
        return r
    d = dict(r or {})
    d.setdefault("market_id", market_id)
    d.setdefault("query", query)
    allowed = {
        "market_id", "query", "title", "snippet", "source_name", "source_url",
        "source_type", "provider", "published_ts", "fetched_ts", "direction",
        "credibility_score", "freshness_score", "relevance_score",
        "contradiction_score", "settlement_relevance_score", "ambiguity_score",
        "evidence_id", "hash",
    }
    d = {k: v for k, v in d.items() if k in allowed}
    return NewsEvidenceItem(**d)


def _parse_rss(xml_text: str, query: str, market_id: str, *, max_items: int = 10) -> list:
    """Parse an RSS/Atom feed into provider-shaped dicts (stdlib only).

    Best-effort + tolerant: returns [] on any parse error. Keeps only title,
    link, source, and published time — never full article bodies/HTML/scripts.
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    out: list = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        published_ts = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                published_ts = int(parsedate_to_datetime(pub).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                published_ts = None
        out.append({
            "market_id": market_id, "query": query, "title": title,
            "snippet": title, "source_name": src or "news", "source_url": link,
            "source_type": "news", "provider": "rss", "published_ts": published_ts,
        })
        if len(out) >= max_items:
            break
    return out


def rss_fetch(query: str, market_context: dict, *, base_url: Optional[str] = None,
              timeout: float = 8.0, max_items: int = 10) -> list:
    """Key-less, read-only news fetch via Google News RSS search.

    The BOT fetches + caches + sanitizes (not Grok). Returns [] on any error so a
    flaky network never breaks training. Sends only the public query — never
    secrets/wallet/positions/order state."""
    import urllib.parse
    base = base_url or "https://news.google.com/rss/search"
    qs = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    url = f"{base}?{qs}"
    try:
        import httpx
        resp = httpx.get(url, timeout=timeout,
                         headers={"User-Agent": "hermes-news-scanner/1.0"})
        resp.raise_for_status()
        return _parse_rss(resp.text, query, str(market_context.get("market_id") or ""),
                          max_items=max_items)
    except Exception:  # noqa: BLE001 — read-only + best-effort; never block training
        return []


def get_provider(mode: str, **kwargs) -> NewsProvider:
    m = str(mode or "offline_cache").strip().lower()
    if m == "fixture":
        return FixtureProvider(**kwargs)
    if m == "live_read_only":
        # Default to the key-less RSS fetch so live_read_only actually pulls
        # real headlines (read-only). Callers may inject their own ``fetch``.
        kwargs.setdefault("fetch", rss_fetch)
        kwargs.setdefault("enabled", True)
        return LiveReadOnlyProvider(**kwargs)
    return OfflineCacheProvider(**kwargs)
