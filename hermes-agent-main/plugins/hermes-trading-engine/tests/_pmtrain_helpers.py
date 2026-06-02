"""Shared helpers for Polymarket PAPER training tests."""

from __future__ import annotations

import time
from types import SimpleNamespace

from engine.campaigns.signal_models import SignalResult


FORBIDDEN = ("MICRO_LIVE_ENABLED", "KALSHI_MICRO_LIVE_ENABLED",
             "POLYMARKET_MICRO_LIVE_ENABLED", "MICRO_LIVE_ALLOW_PRODUCTION",
             "GUARDED_LIVE_ENABLED", "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
             "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE",
             "PRODUCTION_REVIEW_ALLOW_DASHBOARD_SUBMIT",
             "PRODUCTION_REVIEW_ALLOW_API_SUBMIT", "ARB_EXECUTION_ENABLED",
             "MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK")


def clean_live_env(monkeypatch, tmp_path):
    for k in FORBIDDEN:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HTE_MODE", "paper")


def market(i=0, *, bid=0.28, ask=0.30, liq=20000, vol=8000, depth=1000,
           ambiguity=None, fresh=True, desc=True, active=True, closed=False,
           category="politics", group=None, now=None):
    now = now or time.time()
    raw = {
        "id": f"m{i}", "question": f"Will event {i} resolve YES?",
        "active": active, "closed": closed, "archived": False,
        "enableOrderBook": True, "acceptingOrders": True,
        "clobTokenIds": [f"tok{i}a", f"tok{i}b"],
        "outcomePrices": [str((bid + ask) / 2), str(1 - (bid + ask) / 2)],
        "liquidityNum": liq, "volume24hr": vol, "volumeNum": vol * 5,
        "topDepthUsd": depth, "endDate": "2030-01-01T00:00:00Z",
        "category": category,
    }
    if desc:
        raw["description"] = "Resolves YES per official sources by end date. " * 6
    if fresh:
        raw["bestBid"] = bid
        raw["bestAsk"] = ask
        raw["spread"] = round(ask - bid, 4)
        raw["bookUpdatedTs"] = now
    if ambiguity is not None:
        raw["ambiguity"] = ambiguity
    if group is not None:
        raw["groupItemTitle"] = group
        raw["eventId"] = group
    return raw


def catalog(n=10, **kw):
    return [market(i, **kw) for i in range(n)]


class FakeResearch:
    """Deterministic research signal (stands in for a cached Grok estimate).
    Research-only: it can ONLY estimate a probability — no place/cancel/size."""

    name = "research"

    def __init__(self, fair=0.80, conf=0.9, source="grok_cache"):
        self.fair, self.conf, self.source = fair, conf, source

    def evaluate(self, rec):
        return SignalResult(self.fair, self.conf, self.source, "est-1")

    def status(self):
        return {"name": "research", "grok_enabled": False, "grok_source": "offline_cache",
                "research_mode": "offline_cache"}


def fake_rec(**kw):
    base = dict(market_id="m0", group_key="g0", category="politics",
                top_depth_usd=1000.0, clob_token_ids=["t0"], spread=0.02,
                liquidity_usd=20000.0, raw={"bestBid": 0.28, "bestAsk": 0.30})
    base.update(kw)
    return SimpleNamespace(**base)
