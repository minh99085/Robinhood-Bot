"""WS5: non-atomic sequential leg-fill stress test for dutch-book arb."""

from __future__ import annotations

from engine.pulse.markets import OrderBook
from engine.pulse.arbitrage import detect_arbitrage
from engine.pulse.arb_nonatomic import simulate_buy_both_nonatomic


def _book(best_bid, best_ask, asks, bids=None, ts=0.0):
    bids = bids or [(best_bid, 1000.0)]
    return OrderBook(
        best_bid=best_bid, best_ask=best_ask,
        ask_depth_usd=round(sum(p * s for p, s in asks), 2),
        bid_depth_usd=round(sum(p * s for p, s in bids), 2),
        asks=asks, bids=bids, ts=ts,
    )


def test_nonatomic_bible_failure_case_rejected():
    """Leg-1 cheap UP; leg-2 slippage kills guaranteed profit (Bible 0.30 + 0.78 case)."""
    up = _book(0.28, 0.30, asks=[(0.30, 10_000.0)])
    dn = _book(0.43, 0.45, asks=[(0.45, 10_000.0)])
    sim = simulate_buy_both_nonatomic(
        up, dn, target_usd=50.0, fees=0.0, epsilon=0.05, leg2_slippage_bps=8000.0)
    assert sim["survives"] is False
    assert sim["reason"] in ("nonatomic_profit_gone", "below_epsilon_after_nonatomic",
                             "leg2_partial_after_impact")


def test_nonatomic_passes_when_edge_survives_slippage():
    up = _book(0.44, 0.45, asks=[(0.45, 100_000.0)])
    dn = _book(0.44, 0.45, asks=[(0.45, 100_000.0)])
    sim = simulate_buy_both_nonatomic(
        up, dn, target_usd=50.0, fees=0.0, epsilon=0.05, leg2_slippage_bps=50.0)
    assert sim["survives"] is True
    assert sim["reason"] == "ok"


def test_detect_arbitrage_rejects_when_nonatomic_fails():
    up = _book(0.28, 0.30, asks=[(0.30, 10_000.0)])
    dn = _book(0.43, 0.45, asks=[(0.45, 10_000.0)])
    raw = detect_arbitrage(
        up, dn, size_usd=50.0, epsilon=0.05, max_depth_consume_frac=0.9,
        nonatomic_check=False)
    assert raw is not None and raw.actionable is True
    stressed = detect_arbitrage(
        up, dn, size_usd=50.0, epsilon=0.05, max_depth_consume_frac=0.9,
        nonatomic_check=True, nonatomic_slippage_bps=8000.0)
    assert stressed is not None
    assert stressed.actionable is False
    assert stressed.reason.startswith("nonatomic_")


def test_nonatomic_disabled_allows_actionable():
    up = _book(0.28, 0.30, asks=[(0.30, 10_000.0)])
    dn = _book(0.43, 0.45, asks=[(0.45, 10_000.0)])
    opp = detect_arbitrage(
        up, dn, size_usd=50.0, epsilon=0.05, max_depth_consume_frac=0.9,
        nonatomic_check=False)
    assert opp is not None and opp.actionable is True