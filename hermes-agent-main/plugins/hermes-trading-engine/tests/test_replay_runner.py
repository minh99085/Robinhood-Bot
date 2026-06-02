"""ReplayRunner: determinism, isolation, broker depth, mark-to-market, artifacts."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from engine.execution.types import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from engine.market_data.orderbook import OrderbookState
from engine.replay import ReplayConfig, ReplayEventLoader, ReplayRunner, write_report
from engine.replay.episode import ReplayEvent
from engine.schemas import RiskDecision
from engine.storage import Store

FIXTURE = Path(__file__).parent / "fixtures" / "sample_polymarket_replay.jsonl"


def _ev(ts, et, **payload):
    payload = dict(payload)
    payload["timestamp"] = ts
    payload["event_type"] = et
    payload.setdefault("asset_id", "tokA")
    payload.setdefault("market", "mkt1")
    return ReplayEvent(ts_ms=ts, event_type=et, venue="polymarket", market_id="mkt1",
                       asset_id="tokA", payload=payload, sequence=ts)


def _cfg(policy="noop", params=None, **kw):
    return ReplayConfig(policy_name=policy, policy_params=params or {},
                        strategy_tick_ms=kw.pop("tick", 1000),
                        equity_snapshot_ms=kw.pop("eq", 1000),
                        initial_cash=kw.pop("cash", 10000.0), seed=kw.pop("seed", 42), **kw)


def _fixture_events():
    return ReplayEventLoader().from_jsonl(str(FIXTURE))


def test_replay_noop_policy_places_no_orders():
    r = ReplayRunner(_cfg("noop"), Store(":memory:"), _fixture_events())
    report = r.run()
    assert report["status"] == "finished"
    assert r.orders == [] and r.fills == []
    assert r.metrics["ending_equity"] == 10000.0  # cash unchanged


def test_replay_applies_book_then_price_change():
    r = ReplayRunner(_cfg("noop"), Store(":memory:"), _fixture_events())
    r.run()
    book = r.md.get_orderbook("tokA")
    assert book is not None
    assert book.best_bid == Decimal("0.43")  # from the post-tick-size refresh book
    assert book.best_ask == Decimal("0.44")
    assert book.resolved is True  # market_resolved applied


def test_replay_policy_proposal_routes_through_risk_and_oms():
    events = [_ev(1000, "book", bids=[{"price": "0.49", "size": "500"}],
                  asks=[{"price": "0.50", "size": "500"}])]
    r = ReplayRunner(_cfg("simple_edge", {"fair_probability": 0.9, "min_edge": 0.01, "quantity": 10}),
                     Store(":memory:"), events)
    calls = {"risk": 0, "oms": 0}
    real_r = r.risk.evaluate
    r.risk.evaluate = lambda p, c: (calls.__setitem__("risk", calls["risk"] + 1) or real_r(p, c))
    real_o = r.oms.submit

    def osub(*a, **k):
        calls["oms"] += 1
        return real_o(*a, **k)

    r.oms.submit = osub
    r.run()
    assert calls["risk"] >= 1 and calls["oms"] >= 1
    assert len(r.fills) >= 1


def test_replay_order_fill_uses_paper_broker_depth():
    # tight spread (0.499/0.50) so the RiskEngine spread gate doesn't reject
    events = [_ev(1000, "book", bids=[{"price": "0.499", "size": "500"}],
                  asks=[{"price": "0.50", "size": "100"}])]
    r = ReplayRunner(_cfg("simple_edge", {"fair_probability": 0.9, "min_edge": 0.01, "quantity": 1000}),
                     Store(":memory:"), events)
    r.run()
    assert len(r.fills) == 1
    assert float(r.fills[0]["quantity"]) == 35.0  # 100 * 0.35 depth haircut
    assert r.orders[0]["status"] == OrderStatus.PARTIALLY_FILLED


def test_replay_tick_size_change_blocks_until_refresh():
    events = _fixture_events()
    r = ReplayRunner(_cfg("noop"), Store(":memory:"), events)
    # replay up to and including the tick_size_change (ts 1700000005000)
    for ev in events:
        if ev.ts_ms > 1700000005000:
            break
        r.clock.advance_to(ev.ts_ms)
        r.md._dispatch_event(ev.payload)
    from engine.schemas import TradeProposal
    book = r.md.get_orderbook("tokA")
    proposal = TradeProposal(strategy="t", market="polymarket", symbol="mkt1", side="YES",
                             notional=4.4, price=float(book.best_ask), edge_after_costs=0.4,
                             meta={"asset_id": "tokA", "quantity": 10})
    d1 = r.risk.evaluate(proposal, r._risk_context(proposal, "tokA"))
    assert d1.approved is False
    assert d1.code == "tick_size_changed_requires_refresh"
    # apply the post-refresh book (ts 1700000006000) -> dirty cleared
    refresh = next(e for e in events if e.ts_ms == 1700000006000 and e.event_type == "book")
    r.clock.advance_to(refresh.ts_ms)
    r.md._dispatch_event(refresh.payload)
    book2 = r.md.get_orderbook("tokA")
    proposal2 = TradeProposal(strategy="t", market="polymarket", symbol="mkt1", side="YES",
                              notional=4.4, price=float(book2.best_ask), edge_after_costs=0.4,
                              meta={"asset_id": "tokA", "quantity": 10})
    d2 = r.risk.evaluate(proposal2, r._risk_context(proposal2, "tokA"))
    assert d2.approved is True  # block clears after refresh


def test_replay_is_deterministic_same_seed():
    events = _fixture_events()
    params = {"fair_probability": 0.9, "min_edge": 0.01, "quantity": 10}
    r1 = ReplayRunner(_cfg("simple_edge", params), Store(":memory:"), _fixture_events())
    r2 = ReplayRunner(_cfg("simple_edge", params), Store(":memory:"), _fixture_events())
    r1.run()
    r2.run()
    assert r1.metrics == r2.metrics
    assert [o["client_order_id"] for o in r1.orders] == [o["client_order_id"] for o in r2.orders]
    assert [e["equity"] for e in r1.equity_rows] == [e["equity"] for e in r2.equity_rows]


def test_replay_different_seed_only_affects_seeded_policy():
    r1 = ReplayRunner(_cfg("noop", seed=1), Store(":memory:"), _fixture_events())
    r2 = ReplayRunner(_cfg("noop", seed=2), Store(":memory:"), _fixture_events())
    r1.run()
    r2.run()
    assert r1.metrics == r2.metrics  # NoOp is unaffected by seed


def test_replay_writes_isolated_tables(tmp_path):
    out = Store(tmp_path / "op.sqlite3")
    events = [_ev(1000, "book", bids=[{"price": "0.499", "size": "500"}],
                  asks=[{"price": "0.50", "size": "500"}])]
    r = ReplayRunner(_cfg("simple_edge", {"fair_probability": 0.9, "min_edge": 0.01, "quantity": 10}),
                     out, events)
    r.run()
    # operational OMS tables are untouched
    assert out.get_orders() == []
    assert out.get_fills() == []
    # replay tables populated for this run
    assert len(out.get_replay_orders(r.run_id)) >= 1
    assert len(out.get_replay_fills(r.run_id)) >= 1


def test_replay_report_artifacts_created(tmp_path):
    r = ReplayRunner(_cfg("noop"), Store(":memory:"), _fixture_events())
    r.run()
    out_dir = write_report(r, tmp_path)
    for name in ("summary.json", "metrics.json", "equity_curve.csv", "replay_report.md"):
        assert (out_dir / name).exists()


def test_replay_end_open_order_policy_cancel():
    r = ReplayRunner(_cfg("noop", end_open_order_policy="cancel"), Store(":memory:"),
                     [_ev(1000, "book", bids=[{"price": "0.40", "size": "100"}],
                          asks=[{"price": "0.42", "size": "100"}])])
    book = OrderbookState("tokA", "mkt1")
    book.apply_book_event(bids=[{"price": "0.40", "size": "100"}],
                          asks=[{"price": "0.42", "size": "100"}])
    order = OrderRequest(client_order_id="co-rest-1", venue="polymarket", market_id="mkt1",
                         asset_id="tokA", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                         limit_price=Decimal("0.41"), quantity=Decimal("10"),
                         time_in_force=TimeInForce.GTC, venue_kind="pm")
    res = r.oms.submit(order, RiskDecision(approved=True, code="OK"), book=book)
    assert res.status == OrderStatus.OPEN
    r._finalize()  # end-of-run: cancel policy
    assert r.oms.get_order("co-rest-1")["status"] == OrderStatus.CANCELLED


def test_replay_mark_to_market_final_equity():
    events = [
        _ev(1000, "book", bids=[{"price": "0.49", "size": "500"}], asks=[{"price": "0.50", "size": "500"}]),
        _ev(2000, "book", bids=[{"price": "0.60", "size": "500"}], asks=[{"price": "0.62", "size": "500"}]),
    ]
    r = ReplayRunner(_cfg("simple_edge", {"fair_probability": 0.9, "min_edge": 0.01, "quantity": 10}),
                     Store(":memory:"), events)
    r.run()
    # position marked at final midpoint (~0.61) > entry (~0.506) -> net gain
    assert r.metrics["ending_equity"] > 10000.0
