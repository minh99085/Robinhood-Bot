"""NewsEvidenceScanner — the controlled, read-only market-news collector.

Pipeline (all deterministic, fail-closed):

    market metadata -> deterministic queries -> provider.search (rate/budget
    limited) -> timestamp + replay-safety filter -> dedupe -> score -> rank ->
    bounded sanitized NewsPacket.

Guarantees:
* offline_cache by default; live provider only when explicitly enabled.
* Never sends secrets, wallet state, positions, order sizes, or account state
  to a provider (only provider-safe public market context).
* In replay mode it NEVER calls a live provider and only keeps evidence with a
  timestamp at/before the replay timestamp (missing/future -> dropped).
* Every kept item carries published_ts (if known) + fetched_ts.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from .news_providers import NewsProvider, OfflineCacheProvider, safe_market_context
from .news_ranker import build_packet
from .news_schemas import NewsPacket, NewsScanResult

_LIVE_MODES = ("live_read_only",)


def news_evidence_weight(relevance: float, credibility: float,
                         recency: float = 1.0, *, cap: float = 0.10) -> float:
    """Bounded, evidence-only weight for a news item in ``[0, cap]`` (pure).

    News is an EVIDENCE input only — it can nudge probability weighting but never
    select a trade or override the model/market. The returned weight is the
    product of relevance x credibility x recency (each clamped to ``[0, 1]``),
    scaled to ``cap`` so a single noisy headline can never dominate. Monotonic in
    each factor; deterministic.
    """
    def _c(x: float) -> float:
        try:
            return max(0.0, min(1.0, float(x)))
        except (TypeError, ValueError):
            return 0.0
    return round(_c(relevance) * _c(credibility) * _c(recency) * max(0.0, float(cap)), 8)


def combine_news_evidence(weights, *, cap: float = 0.10) -> float:
    """Aggregate per-item news weights into a single bounded evidence weight.

    Uses a diminishing-returns sum (never exceeds ``cap``) so adding more items
    cannot turn advisory evidence into authority. Deterministic + pure."""
    total = 0.0
    for w in weights or []:
        try:
            wv = max(0.0, float(w))
        except (TypeError, ValueError):
            continue
        total += wv * (1.0 - total / max(1e-9, cap))  # diminishing returns toward cap
    return round(max(0.0, min(float(cap), total)), 8)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _kw(text: str) -> list:
    return [t for t in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(t) > 2]


class NewsEvidenceScanner:
    def __init__(self, provider: Optional[NewsProvider] = None, *,
                 max_queries: int = 3, max_items: int = 8,
                 max_snippet_chars: int = 500, min_relevance: float = 0.0,
                 min_credibility: float = 0.0, cache_ttl_seconds: int = 1800,
                 budget_max_calls: Optional[int] = None,
                 cache: Optional[dict] = None,
                 clock: Optional[Callable[[], int]] = None,
                 now_ms: Optional[Callable[[], int]] = None,
                 replay_timestamp_safe: bool = True,
                 require_published_at: bool = False,
                 reject_unclear_date: bool = False, max_age_hours: float = 0.0):
        self.provider = provider or OfflineCacheProvider()
        self.max_queries = max(1, int(max_queries))
        self.max_items = max(1, int(max_items))
        self.max_snippet_chars = max(1, int(max_snippet_chars))
        self.min_relevance = float(min_relevance)
        self.min_credibility = float(min_credibility)
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.budget_max_calls = budget_max_calls
        self.now_ms = now_ms or clock or _now_ms
        self.replay_timestamp_safe = bool(replay_timestamp_safe)
        self.require_published_at = bool(require_published_at)
        self.reject_unclear_date = bool(reject_unclear_date)
        self.max_age_hours = float(max_age_hours)
        self._cache: dict = cache if cache is not None else {}
        self._calls_made = 0

    @property
    def provider_mode(self) -> str:
        return getattr(self.provider, "mode", "offline_cache")

    # -- query building ------------------------------------------------- #
    def build_queries(self, market_ctx: dict) -> list:
        """Deterministic search queries derived from market metadata only."""
        question = str(market_ctx.get("question") or "").strip()
        slug = str(market_ctx.get("slug") or "").strip()
        category = str(market_ctx.get("category") or "").strip()
        res = str(market_ctx.get("resolution_source") or "").strip()
        kws = [str(k) for k in (market_ctx.get("asset_keywords") or []) if k]

        queries: list = []

        def _add(q: str) -> None:
            q = " ".join(str(q or "").split())
            if q and q not in queries:
                queries.append(q)

        if question:
            _add(question)
        if kws:
            _add(" ".join(kws[:4]))
        if slug:
            _add(slug.replace("-", " "))
        if res and question:
            _add(f"{' '.join(_kw(question)[:4])} {res}")
        if category and (kws or question):
            anchor = " ".join(kws[:2]) or " ".join(_kw(question)[:3])
            _add(f"{anchor} {category}")
        if not queries and slug:
            _add(slug.replace("-", " "))
        return queries[: self.max_queries]

    # -- scan ----------------------------------------------------------- #
    def scan(self, market_ctx: dict, *, now_ms: Optional[int] = None,
             replay_ts_ms: Optional[int] = None) -> NewsScanResult:
        ts = int(now_ms) if now_ms is not None else self.now_ms()
        market_id = str(market_ctx.get("market_id") or "")
        queries = self.build_queries(market_ctx)
        provider_mode = self.provider_mode

        # Replay safety: a live provider can NEVER be used in replay.
        if replay_ts_ms is not None and provider_mode in _LIVE_MODES:
            return self._empty_result(
                market_id, queries, provider_mode, ok=False,
                error="live_provider_forbidden_in_replay", replay_ts_ms=replay_ts_ms)

        # Serve from cache if fresh (and cache key matches market).
        cached = self._cache_get(market_id, ts)
        raw_items: list = []
        fetched = 0
        provider_ok = True
        provider_error: Optional[str] = None

        if cached is not None and replay_ts_ms is None:
            raw_items = list(cached)
            fetched = len(raw_items)
        else:
            for q in queries:
                if self.budget_max_calls is not None and \
                        self._calls_made >= self.budget_max_calls:
                    break
                try:
                    self._calls_made += 1
                    results = self.provider.search(q, safe_market_context(market_ctx)) or []
                except Exception as e:  # noqa: BLE001 — fail closed, no leak
                    provider_ok = False
                    provider_error = type(e).__name__
                    continue
                for it in results:
                    # Live/online fetch stamps fetched_ts. In replay we must NOT
                    # invent a timestamp — missing ts has to fail closed below.
                    if not it.fetched_ts and replay_ts_ms is None:
                        it.fetched_ts = ts
                    if not it.market_id:
                        it.market_id = market_id
                    raw_items.append(it)
                    fetched += 1
            if replay_ts_ms is None and provider_ok:
                self._cache_put(market_id, raw_items, ts)

        # Replay timestamp safety filter (fail-closed on missing/future ts).
        usable = raw_items
        replay_dropped = 0
        if replay_ts_ms is not None:
            usable, replay_dropped = self._filter_replay(raw_items, int(replay_ts_ms))

        packet = build_packet(
            usable, market_ctx=market_ctx, now_ms=ts, max_items=self.max_items,
            max_snippet_chars=self.max_snippet_chars,
            min_relevance=self.min_relevance, min_credibility=self.min_credibility,
            queries=queries, provider_mode=provider_mode,
            require_published_at=self.require_published_at,
            reject_unclear_date=self.reject_unclear_date, max_age_hours=self.max_age_hours)

        return NewsScanResult(
            packet=packet, provider_mode=provider_mode, queries=queries,
            fetched=fetched, used=packet.used,
            rejected=packet.rejected + replay_dropped,
            stale_count=packet.stale_count + replay_dropped,
            contradiction_count=packet.contradiction_count,
            ambiguity_count=packet.ambiguity_count, provider_ok=provider_ok,
            provider_error=provider_error, replay_ts_ms=replay_ts_ms)

    # -- replay safety -------------------------------------------------- #
    def _filter_replay(self, items, replay_ts_ms: int):
        """Keep only evidence with a timestamp at/before the replay timestamp.
        Missing or future-dated timestamps are dropped (fail closed)."""
        kept = []
        dropped = 0
        for it in items:
            ts = it.published_ts if it.published_ts is not None else it.fetched_ts
            if self.replay_timestamp_safe:
                if ts is None:
                    dropped += 1
                    continue
                if int(ts) > replay_ts_ms:
                    dropped += 1
                    continue
            kept.append(it)
        return kept, dropped

    # -- cache ---------------------------------------------------------- #
    def _cache_get(self, market_id: str, ts: int):
        entry = self._cache.get(market_id)
        if not entry:
            return None
        stored_ts, items = entry
        if self.cache_ttl_seconds and (ts - stored_ts) > self.cache_ttl_seconds * 1000:
            return None
        return items

    def _cache_put(self, market_id: str, items, ts: int) -> None:
        self._cache[market_id] = (ts, list(items))

    def _empty_result(self, market_id, queries, provider_mode, *, ok, error,
                      replay_ts_ms=None) -> NewsScanResult:
        packet = NewsPacket(market_id=market_id, items=[],
                            provider_mode=provider_mode, queries=list(queries))
        return NewsScanResult(
            packet=packet, provider_mode=provider_mode, queries=list(queries),
            fetched=0, used=0, rejected=0, stale_count=0, provider_ok=ok,
            provider_error=error, replay_ts_ms=replay_ts_ms)

    # -- factory -------------------------------------------------------- #
    @classmethod
    def from_config(cls, cfg, *, provider: Optional[NewsProvider] = None,
                    cache: Optional[dict] = None,
                    clock: Optional[Callable[[], int]] = None) -> "NewsEvidenceScanner":
        mode = getattr(cfg, "news_provider_mode", "offline_cache")
        if provider is None:
            from .news_providers import get_provider
            provider = get_provider(mode)
        return cls(
            provider=provider,
            max_queries=getattr(cfg, "news_max_queries_per_market", 3),
            max_items=getattr(cfg, "news_max_items_per_market", 8),
            max_snippet_chars=getattr(cfg, "news_max_snippet_chars", 500),
            min_relevance=getattr(cfg, "news_min_relevance_score", 0.0),
            min_credibility=getattr(cfg, "news_min_source_credibility", 0.0),
            cache_ttl_seconds=getattr(cfg, "news_cache_ttl_seconds", 1800),
            replay_timestamp_safe=getattr(cfg, "news_replay_timestamp_safe", True),
            require_published_at=getattr(cfg, "news_require_published_at", False),
            reject_unclear_date=getattr(cfg, "news_reject_unclear_date", False),
            max_age_hours=getattr(cfg, "news_max_age_hours", 0.0),
            cache=cache, clock=clock)
