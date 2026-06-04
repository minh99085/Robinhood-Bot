"""Market-news scanner wired into the training loop (PAPER ONLY, advisory)."""

from __future__ import annotations

import types

from engine.research.news_providers import FixtureProvider, _parse_rss, get_provider
from engine.research.news_scanner import NewsEvidenceScanner
from engine.training import PolymarketPaperTrainer, TrainingConfig

_NOW = 1_700_000_000

_SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Bitcoin rallies above 100k on Coinbase</title>
    <link>https://news.example/btc-100k</link>
    <pubDate>Wed, 03 Jun 2026 12:00:00 GMT</pubDate>
    <source url="https://reuters.com">Reuters</source>
  </item>
  <item>
    <title>BTC dips after record high</title>
    <link>https://news.example/btc-dip</link>
    <pubDate>Wed, 03 Jun 2026 13:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_parse_rss_extracts_items():
    items = _parse_rss(_SAMPLE_RSS, query="btc", market_id="m1")
    assert len(items) == 2
    assert items[0]["title"].startswith("Bitcoin rallies")
    assert items[0]["source_url"] == "https://news.example/btc-100k"
    assert items[0]["published_ts"] is not None
    assert items[0]["source_type"] == "news"


def test_parse_rss_bad_xml_returns_empty():
    assert _parse_rss("not xml at all", query="q", market_id="m1") == []


def test_live_read_only_provider_defaults_to_rss_fetch():
    prov = get_provider("live_read_only")
    assert prov.mode == "live_read_only"
    assert prov.enabled is True            # key-less RSS fetch wired by default


def test_news_off_by_default(tmp_path):
    t = PolymarketPaperTrainer(TrainingConfig(mode="observe_only"), data_dir=tmp_path)
    assert t.news_scanner is None
    st = t.status()
    assert st["news"]["news_scanner_enabled"] is False


def test_news_scanner_runs_in_loop_and_aggregates(tmp_path):
    cfg = TrainingConfig(mode="observe_only", news_scanner_enabled=True)
    t = PolymarketPaperTrainer(cfg, data_dir=tmp_path)
    assert t.news_scanner is not None
    # swap in a deterministic fixture-backed scanner (no network)
    item = {"title": "BTC rallies above 100k on Coinbase",
            "snippet": "Bitcoin climbed above 100000 per Coinbase data.",
            "source_url": "https://r.com/a", "source_type": "wire",
            "published_ts": _NOW * 1000 - 3600_000}
    t.news_scanner = NewsEvidenceScanner(FixtureProvider([item]), min_relevance=0.0,
                                         min_credibility=0.0)
    watch = [types.SimpleNamespace(market_id="0xabc",
                                   question="Will BTC close above 100k?",
                                   category="crypto")]
    t._news_scan(watch, now=float(_NOW))
    nw = t.news_status()
    assert nw["news_scanner_enabled"] is True
    assert nw["news_markets_scanned"] >= 1
    assert nw["news_queries"] >= 1
    assert nw["news_items_used"] >= 1
    assert nw["news_last_packet_sample"]              # a sanitized headline is shown


def test_news_scan_failure_never_raises(tmp_path):
    cfg = TrainingConfig(mode="observe_only", news_scanner_enabled=True)
    t = PolymarketPaperTrainer(cfg, data_dir=tmp_path)

    class _Boom:
        def scan(self, *a, **k):
            raise RuntimeError("provider boom")

    t.news_scanner = _Boom()
    # run_tick must still succeed even if the news scan throws
    out = t.run_tick([])
    assert out["tick"] >= 1
