"""Phase 6 tests: read-only Kalshi + venue-neutral prediction-market layer.

All HTTP/WebSocket interactions are mocked. No network. No real keys. These
verify the read-only contract (no order placement / cancellation / private
channels), correct YES/NO book normalization, sequence-gap handling, RiskEngine
venue gates, PaperBroker binary fills, and storage/replay integration.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engine.execution.paper_broker import KalshiBookView, PaperBroker
from engine.execution.types import OrderRequest, OrderSide, OrderStatus, OrderType
from engine.risk import RiskContext, RiskEngine, RiskLimits, RiskCode, VenueSnapshot
from engine.schemas import TradeProposal
from engine.storage import Store
from engine.venues import MarketRef, build_default_registry
from engine.venues.kalshi import auth as kauth
from engine.venues.kalshi.lifecycle import parse_lifecycle, parse_resolution
from engine.venues.kalshi.normalizer import normalize_market, normalize_series
from engine.venues.kalshi.orderbook import KalshiBinaryOrderbook
from engine.venues.kalshi.replay import reconstruct, venue_breakdown
from engine.venues.kalshi.rest import KalshiRestClient
from engine.venues.kalshi.ws import FORBIDDEN_CHANNELS, KalshiWSClient
from engine.venues.resolution import build_resolution_ruleset

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_FIXTURE = _ROOT / "tests" / "fixtures" / "sample_kalshi_replay.jsonl"
D = Decimal


def _snap_book(yes=((D("0.42"), D("100")),), no=((D("0.37"), D("150")),), seq=1):
    b = KalshiBinaryOrderbook("FED-T3")
    b.apply_snapshot(list(yes), list(no), seq=seq)
    return b


def _ok_proposal() -> TradeProposal:
    return TradeProposal(strategy="t", market="polymarket", symbol="FED-T3", side="YES",
                         notional=10.0, price=0.5, edge_after_costs=0.0, spread=0.0,
                         ambiguity_score=0.0, allow_duplicate=False, mode="paper",
                         rationale="t", meta={})


def _venue_ctx(**kw) -> RiskContext:
    ctx = RiskContext(equity=1000.0)
    ctx.venue = VenueSnapshot(required=True, venue="kalshi", **kw)
    return ctx


def _order(side, limit, outcome="YES") -> OrderRequest:
    return OrderRequest(venue="kalshi", market_id="FED-T3", outcome=outcome, side=side,
                        order_type=OrderType.MARKETABLE_LIMIT, limit_price=D(str(limit)),
                        quantity=D("10"), venue_kind="kalshi")


def _clear_kalshi_env(mp):
    mp.setenv("KALSHI_ENABLED", "1")
    for k in ("KALSHI_ACCESS_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_PRIVATE_KEY_PEM"):
        mp.delenv(k, raising=False)


# 1
def test_kalshi_auth_missing_credentials_disables_adapter(monkeypatch):
    _clear_kalshi_env(monkeypatch)
    _, signer, status = kauth.load_kalshi_auth()
    assert status == kauth.DISABLED_MISSING_CREDENTIALS and signer is None


# 2
def test_kalshi_auth_headers_redact_secrets():
    signer = kauth.ReadOnlyKalshiSigner("ak-SECRET-123", object(), "demo")
    text = repr(signer)
    assert "ak-SECRET-123" not in text and "[REDACTED]" in text


# 3
def test_kalshi_ws_signing_message_path():
    msg = kauth.ReadOnlyKalshiSigner.ws_signing_message("1700000000000")
    assert msg == "1700000000000GET/trade-api/ws/v2"
    assert "GET" in msg and "/trade-api/ws/v2" in msg


# 4
def test_kalshi_rest_never_exposes_order_endpoints():
    c = KalshiRestClient("https://demo-api.kalshi.co/trade-api/v2")
    for forbidden in ("place_order", "create_order", "submit_order", "cancel_order"):
        assert not hasattr(c, forbidden)
    assert c.READ_ONLY is True


# 5
def test_kalshi_market_metadata_normalization():
    m = normalize_market({"ticker": "FED-T3", "title": "Will the Fed cut?", "status": "open",
                          "yes_bid": 42, "no_bid": 37, "close_time": "2026-12-31T00:00:00Z",
                          "rules_primary": "Resolves YES if ...", "event_ticker": "FED-23DEC"})
    assert m.venue == "kalshi" and m.market_ticker == "FED-T3"
    assert m.yes_bid == D("0.42") and m.status == "open"
    assert m.fee_metadata.get("rules_primary")


# 6
def test_kalshi_series_metadata_normalization():
    s = normalize_series({"ticker": "FED", "title": "Fed decisions", "category": "Econ",
                          "settlement_sources": [{"name": "Federal Reserve", "url": "https://fed.gov"}],
                          "contract_terms_url": "https://kalshi.com/rules/FED"})
    assert s.series_ticker == "FED" and len(s.settlement_sources) == 1
    assert s.contract_terms_url == "https://kalshi.com/rules/FED"


# 7
def test_kalshi_resolution_rules_from_metadata():
    m = normalize_market({"ticker": "FED-T3", "title": "Will the Fed cut at the discretion of?",
                          "status": "open",
                          "settlement_sources": [{"name": "Fed", "url": "https://fed.gov"}]})
    rr = build_resolution_ruleset(m)
    assert rr.venue == "kalshi" and rr.market_ticker == "FED-T3"
    assert len(rr.settlement_sources) == 1
    assert 0.0 <= rr.ambiguity_score <= 1.0


# 8
def test_kalshi_yes_no_book_derives_yes_asks_from_no_bids():
    nb = _snap_book(no=((D("0.37"), D("150")),)).normalized("YES")
    assert nb.best_ask == D("0.63")


# 9
def test_kalshi_yes_no_book_derives_no_asks_from_yes_bids():
    nb = _snap_book(yes=((D("0.42"), D("100")),)).normalized("NO")
    assert nb.best_ask == D("0.58")


# 10
def test_kalshi_snapshot_replaces_book():
    b = _snap_book()
    b.apply_snapshot([(D("0.5"), D("5"))], [(D("0.4"), D("9"))], seq=2)
    assert set(b.yes_bids.keys()) == {D("0.5")} and set(b.no_bids.keys()) == {D("0.4")}


# 11
def test_kalshi_delta_updates_yes_bid():
    b = _snap_book()
    b.apply_delta("yes", D("0.42"), D("25"), seq=2)
    assert b.yes_bids[D("0.42")] == D("125")


# 12
def test_kalshi_delta_updates_no_bid():
    b = _snap_book()
    b.apply_delta("no", D("0.37"), D("10"), seq=2)
    assert b.no_bids[D("0.37")] == D("160")


# 13
def test_kalshi_delta_removes_level_when_size_nonpositive():
    b = _snap_book()
    b.apply_delta("yes", D("0.42"), D("-100"), seq=2)
    assert D("0.42") not in b.yes_bids


# 14
def test_kalshi_sequence_gap_marks_book_unreliable():
    b = _snap_book(seq=1)
    b.apply_delta("yes", D("0.42"), D("1"), seq=3)  # skips seq 2
    assert b.gap_detected and b.needs_snapshot


# 15
def test_kalshi_snapshot_clears_sequence_gap():
    b = _snap_book(seq=1)
    b.apply_delta("yes", D("0.42"), D("1"), seq=3)
    assert b.needs_snapshot
    b.apply_snapshot([(D("0.5"), D("5"))], [(D("0.4"), D("5"))], seq=4)
    assert not b.needs_snapshot and not b.gap_detected


# 16
def test_kalshi_orderbook_crossed_state_invalid():
    b = _snap_book(yes=((D("0.5"), D("5")),), no=((D("0.5"), D("5")),))
    nb = b.normalized("YES")
    assert nb.crossed and not nb.valid


# 17
def test_kalshi_ticker_updates_bbo():
    c = KalshiWSClient("wss://x")
    c.process_message({"type": "ticker", "market_ticker": "FED-T3", "yes_bid": 42,
                       "yes_ask": 63, "no_bid": 37, "no_ask": 58})
    bbo = c.get_bbo("FED-T3", "YES")
    assert bbo.best_bid == D("0.42") and bbo.best_ask == D("0.63")


# 18
def test_kalshi_trade_persisted_as_trade_print():
    c = KalshiWSClient("wss://x")
    c.process_message({"type": "trade", "market_ticker": "FED-T3", "yes_price": 43, "count": 10})
    assert c.trades and c.trades[-1]["price"] == D("0.43")


# 19
def test_kalshi_lifecycle_closed_blocks_risk():
    status = parse_lifecycle({"market_ticker": "FED-T3", "status": "settled"})
    assert status.is_terminal()
    d = RiskEngine(RiskLimits()).evaluate(_ok_proposal(), _venue_ctx(settled=True))
    assert not d.approved and d.code == RiskCode.MARKET_SETTLED


# 20
def test_kalshi_seq_gap_blocks_risk():
    d = RiskEngine(RiskLimits()).evaluate(_ok_proposal(),
                                          _venue_ctx(seq_gap=True, needs_snapshot=True))
    assert not d.approved and d.code == RiskCode.SEQUENCE_GAP_REQUIRES_SNAPSHOT


# 21
def test_kalshi_missing_resolution_rules_blocks_when_required():
    lim = RiskLimits(venue_require_resolution_rules=True)
    d = RiskEngine(lim).evaluate(_ok_proposal(), _venue_ctx(resolution_rules_present=False))
    assert not d.approved and d.code == RiskCode.RESOLUTION_RULES_MISSING


# 22
def test_kalshi_high_ambiguity_blocks_risk():
    d = RiskEngine(RiskLimits()).evaluate(_ok_proposal(), _venue_ctx(ambiguity_score=0.9))
    assert not d.approved and d.code == RiskCode.SETTLEMENT_AMBIGUITY_HIGH


def _fill_side_assert(side, outcome, expected_near, opposite):
    nb = _snap_book().normalized(outcome)
    view = KalshiBookView(nb)
    res = PaperBroker().execute(_order(side, 0.99 if side == OrderSide.BUY else 0.01, outcome),
                                book=view, venue_kind="kalshi")
    assert res.fills, f"expected fills, got {res.status}/{res.reject_reason}"
    p = res.fills[0].price
    assert abs(p - expected_near) < abs(p - opposite)


# 23
def test_paper_broker_buy_yes_consumes_derived_yes_asks():
    _fill_side_assert(OrderSide.BUY, "YES", D("0.63"), D("0.42"))


# 24
def test_paper_broker_sell_yes_consumes_yes_bids():
    _fill_side_assert(OrderSide.SELL, "YES", D("0.42"), D("0.63"))


# 25
def test_paper_broker_buy_no_consumes_derived_no_asks():
    _fill_side_assert(OrderSide.BUY, "NO", D("0.58"), D("0.37"))


# 26
def test_paper_broker_sell_no_consumes_no_bids():
    _fill_side_assert(OrderSide.SELL, "NO", D("0.37"), D("0.58"))


# 27
def test_kalshi_reference_price_fallback_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PAPER_ALLOW_KALSHI_REFERENCE_PRICE_FILLS", raising=False)
    res = PaperBroker().execute(_order(OrderSide.BUY, 0.99), book=None,
                                reference_price=D("0.5"), venue_kind="kalshi")
    assert res.status == OrderStatus.REJECTED and not res.fills


# 28
def test_venue_registry_routes_polymarket_and_kalshi():
    reg = build_default_registry()
    assert set(reg.venues()) >= {"polymarket", "kalshi"}
    assert reg.get("polymarket") is not None and reg.get("kalshi") is not None


# 29
def test_venue_metadata_storage_migration_idempotent(tmp_path):
    p = tmp_path / "v.db"
    Store(p)
    s2 = Store(p)
    for t in ("venue_markets", "venue_series", "resolution_rules", "kalshi_orderbook_deltas"):
        row = s2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        assert row is not None


def _load_fixture_events() -> list[dict]:
    return [json.loads(line) for line in _FIXTURE.read_text().splitlines() if line.strip()]


# 30
def test_replay_kalshi_fixture_reconstructs_book():
    books = reconstruct(_load_fixture_events())
    b = books["FED-23DEC-T3.00"]
    nb = b.normalized("YES")
    assert nb.best_bid == D("0.44")  # final snapshot top YES bid 44c
    assert not b.needs_snapshot  # final snapshot cleared the gap


# 31
def test_replay_kalshi_sequence_gap_blocks_until_snapshot():
    events = _load_fixture_events()
    # up to (and including) the gap delta (seq 4 after seq 2) — before the resync snapshot
    pre = events[:6]
    book = reconstruct(pre)["FED-23DEC-T3.00"]
    assert book.needs_snapshot
    nb = book.normalized("YES")
    vs = VenueSnapshot(required=True, venue="kalshi", seq_gap=nb.gap_detected,
                       needs_snapshot=nb.needs_snapshot)
    d = RiskEngine(RiskLimits()).evaluate(_ok_proposal(), _ctx_with(vs))
    assert not d.approved and d.code == RiskCode.SEQUENCE_GAP_REQUIRES_SNAPSHOT
    # full replay (with resync snapshot) clears it
    assert not reconstruct(events)["FED-23DEC-T3.00"].needs_snapshot


def _ctx_with(vs: VenueSnapshot) -> RiskContext:
    ctx = RiskContext(equity=1000.0)
    ctx.venue = vs
    return ctx


# 32
def test_replay_metrics_by_venue():
    breakdown = venue_breakdown(_load_fixture_events())
    assert breakdown.get("kalshi") == 8


# 33
def test_research_cache_key_includes_venue(tmp_path):
    from engine.research import ReplayResearchCache
    store = Store(tmp_path / "v.db")
    for venue, p in (("kalshi", "0.6"), ("polymarket", "0.3")):
        store.add_probability_estimate({
            "estimate_id": f"e-{venue}", "venue": venue, "market_id": "SAME", "asset_id": None,
            "outcome": "YES", "ts_ms": 100, "p_ensemble": p, "stale_after_ts_ms": 10_000_000})
    cache = ReplayResearchCache(store)
    k = cache.latest_estimate(venue="kalshi", market_id="SAME", asset_id=None, at_ts_ms=200)
    pm = cache.latest_estimate(venue="polymarket", market_id="SAME", asset_id=None, at_ts_ms=200)
    assert k["p_ensemble"] == "0.6" and pm["p_ensemble"] == "0.3"


# 34
def test_no_network_in_replay_with_kalshi_fixture(monkeypatch):
    import httpx
    def _boom(*a, **k):
        raise AssertionError("network call during replay!")
    monkeypatch.setattr(httpx, "get", _boom)
    from engine.replay import ReplayEventLoader
    events = ReplayEventLoader().from_jsonl(str(_FIXTURE))
    books = reconstruct(events)
    assert "FED-23DEC-T3.00" in books


# 35
def test_kalshi_readonly_smoke_help():
    spec = importlib.util.spec_from_file_location("kalshi_smoke_cli",
                                                  _SCRIPTS / "kalshi_readonly_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as e:
        mod.main(["--help"])
    assert e.value.code == 0


# 36
def test_sync_prediction_markets_does_not_call_order_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_ENABLED", "0")  # no creds -> graceful degrade
    spec = importlib.util.spec_from_file_location("sync_pm_cli",
                                                  _SCRIPTS / "sync_prediction_markets.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    rc = mod.main(["--venue", "kalshi", "--db", str(tmp_path / "v.db")])
    assert rc == 0
    assert not hasattr(KalshiRestClient, "place_order")


# 37
def test_no_secrets_in_kalshi_logs(monkeypatch):
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "ak-TOPSECRET-XYZ")
    pem = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
    out = kauth.redact(f"connecting key=ak-TOPSECRET-XYZ pem={pem}")
    assert "ak-TOPSECRET-XYZ" not in out
    assert "ABC" not in out and "[REDACTED" in out


# 38
def test_api_venues_status_no_secrets(monkeypatch):
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "ak-MUSTNOTLEAK")
    monkeypatch.setenv("KALSHI_ENABLED", "1")
    reg = build_default_registry()
    blob = json.dumps([s.model_dump() for s in reg.statuses()], default=str)
    assert "ak-MUSTNOTLEAK" not in blob


# 39
def test_existing_polymarket_market_data_still_works():
    from engine.market_data.orderbook import OrderbookState  # Phase 2 module intact
    ob = OrderbookState(asset_id="tok-1")
    assert ob is not None
    reg = build_default_registry()
    assert reg.get("polymarket").get_status().enabled is True


# 40
def test_compile_and_import_kalshi_modules():
    import importlib
    for name in ("metadata", "base", "identifiers", "resolution", "registry"):
        importlib.import_module(f"engine.venues.{name}")
    for name in ("auth", "rest", "ws", "orderbook", "normalizer", "lifecycle", "smoke", "replay"):
        importlib.import_module(f"engine.venues.kalshi.{name}")
    importlib.import_module("engine.venues.polymarket")
