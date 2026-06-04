"""News quality date filters: reject unclear / stale published dates."""

from __future__ import annotations

from engine.research.news_ranker import build_packet
from engine.research.news_schemas import NewsEvidenceItem

_NOW = 1_700_000_000_000
_HOUR = 3_600_000

_CTX = {"market_id": "m1", "question": "Will BTC close above 100k?",
        "asset_keywords": ["btc", "bitcoin"]}


def _item(title, published_ts, url):
    return NewsEvidenceItem(
        market_id="m1", query="btc", title=title,
        snippet="bitcoin moved above 100000 per coinbase data " + title,
        source_name="Wire", source_url=url, source_type="wire",
        published_ts=published_ts, direction="supports_yes")


def test_unclear_date_rejected():
    items = [_item("clear", _NOW - _HOUR, "https://w/1"),
             _item("unclear", None, "https://w/2")]
    pkt = build_packet(items, market_ctx=_CTX, now_ms=_NOW, min_relevance=0.0,
                       require_published_at=True, reject_unclear_date=True)
    urls = {it.source_url for it in pkt.items}
    assert "https://w/1" in urls
    assert "https://w/2" not in urls
    assert pkt.rejected_reasons.get("no_published_date", 0) >= 1


def test_too_old_rejected():
    items = [_item("fresh", _NOW - _HOUR, "https://w/fresh"),
             _item("old", _NOW - 100 * _HOUR, "https://w/old")]
    pkt = build_packet(items, market_ctx=_CTX, now_ms=_NOW, min_relevance=0.0,
                       max_age_hours=48)
    urls = {it.source_url for it in pkt.items}
    assert "https://w/fresh" in urls
    assert "https://w/old" not in urls
    assert pkt.rejected_reasons.get("too_old", 0) >= 1


def test_no_filters_keeps_items():
    items = [_item("a", None, "https://w/a"),
             _item("b", _NOW - 100 * _HOUR, "https://w/b")]
    pkt = build_packet(items, market_ctx=_CTX, now_ms=_NOW, min_relevance=0.0)
    assert pkt.used == 2          # default: no date filtering
