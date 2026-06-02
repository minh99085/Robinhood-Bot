"""OrderbookState (Decimal) event-application tests."""

from __future__ import annotations

from decimal import Decimal

from engine.market_data.orderbook import OrderbookState


def _seed_book() -> OrderbookState:
    ob = OrderbookState("asset-1", "market-1")
    ob.apply_book_event(
        bids=[{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "50"}],
        asks=[{"price": "0.42", "size": "80"}, {"price": "0.45", "size": "30"}],
        tick_size="0.01")
    return ob


def test_orderbook_book_event_replaces_snapshot():
    ob = _seed_book()
    assert ob.best_bid == Decimal("0.40")
    assert ob.best_ask == Decimal("0.42")
    assert ob.spread == Decimal("0.02")
    assert ob.midpoint == Decimal("0.41")
    assert ob.has_book is True
    # a second book event fully replaces the prior levels
    ob.apply_book_event(bids=[{"price": "0.30", "size": "10"}],
                        asks=[{"price": "0.35", "size": "10"}])
    assert ob.best_bid == Decimal("0.30")
    assert ob.best_ask == Decimal("0.35")
    assert Decimal("0.40") not in ob.bids


def test_orderbook_price_change_updates_bid():
    ob = _seed_book()
    ob.apply_price_change([{"side": "BUY", "price": "0.41", "size": "60"}])
    assert ob.bids[Decimal("0.41")] == Decimal("60")
    assert ob.best_bid == Decimal("0.41")


def test_orderbook_price_change_size_zero_removes_level():
    ob = _seed_book()
    assert Decimal("0.40") in ob.bids
    deltas = ob.apply_price_change([{"side": "BUY", "price": "0.40", "size": "0"}])
    assert Decimal("0.40") not in ob.bids
    assert deltas[0]["action"] == "remove"
    assert ob.best_bid == Decimal("0.39")  # next level down


def test_orderbook_price_change_updates_ask():
    ob = _seed_book()
    ob.apply_price_change([{"side": "SELL", "price": "0.43", "size": "20"}])
    assert ob.asks[Decimal("0.43")] == Decimal("20")
    assert ob.best_ask == Decimal("0.42")  # 0.42 still best; 0.43 is worse


def test_tick_size_change_marks_state_dirty():
    ob = _seed_book()
    assert ob.tick_size_dirty is False
    ob.apply_tick_size_change("0.001")
    assert ob.tick_size == Decimal("0.001")
    assert ob.tick_size_dirty is True
    # a fresh book snapshot acknowledges the change and clears the flag
    ob.apply_book_event(bids=[{"price": "0.40", "size": "1"}],
                        asks=[{"price": "0.41", "size": "1"}])
    assert ob.tick_size_dirty is False


def test_best_bid_ask_updates_bbo():
    ob = OrderbookState("asset-2", "market-2")
    ob.apply_best_bid_ask(best_bid="0.50", best_ask="0.55")
    assert ob.best_bid == Decimal("0.50")
    assert ob.best_ask == Decimal("0.55")
    bbo = ob.bbo()
    assert bbo is not None
    assert bbo.bid == 0.50
    assert bbo.ask == 0.55


def test_price_change_before_book_is_unreliable():
    ob = OrderbookState("asset-3", "market-3")
    ob.apply_price_change([{"side": "BUY", "price": "0.40", "size": "10"}])
    assert ob.unreliable is True  # deltas with no base snapshot are untrustworthy


def test_is_stale_uses_last_update():
    ob = _seed_book()
    assert ob.is_stale(max_age_ms=10_000) is False
    ob.last_update_ms = 1  # ancient
    assert ob.is_stale(max_age_ms=10_000) is True


def test_imbalance_sign_tracks_resting_size():
    ob = _seed_book()  # bid size 150, ask size 110 -> bid-heavy (positive)
    imb = ob.imbalance()
    assert imb is not None and imb > 0
    # flip the book to be ask-heavy
    ob.apply_book_event(bids=[{"price": "0.40", "size": "10"}],
                        asks=[{"price": "0.42", "size": "100"}])
    assert ob.imbalance() < 0


def test_imbalance_none_without_book():
    ob = OrderbookState("asset-x", "market-x")
    assert ob.imbalance() is None
    assert ob.microprice() is None


def test_microprice_between_bbo_and_weighted_by_size():
    ob = _seed_book()  # bid 0.40 (size 150 total), ask 0.42 (size 110 total)
    mp = ob.microprice()
    assert mp is not None
    assert Decimal("0.40") <= mp <= Decimal("0.42")
    # depth-weighted toward the heavier (bid) side -> above the plain midpoint
    assert mp > ob.midpoint
