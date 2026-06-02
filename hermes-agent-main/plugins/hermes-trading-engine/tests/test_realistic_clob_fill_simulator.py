"""Realistic CLOB v2 fill simulation (PAPER ONLY).

A probabilistic, depth-aware fill model so aggressive paper trades get REALISTIC
feedback (not guaranteed fills): fill probability from spread / depth / size /
book age / volatility / queue proxy / price aggressiveness, partial fills, and an
adverse-selection markout. Deterministic given a seed. Offline.
"""

from __future__ import annotations

from decimal import Decimal

from engine.execution.paper_broker import PaperBroker, RealisticFillModel
from engine.execution.slippage import SlippageModel, markout_bps
from engine.execution.types import (D, OrderRequest, OrderSide, OrderStatus,
                                     OrderType, TimeInForce)


class FakeBook:
    """Minimal CLOB book the PaperBroker can fill against."""

    def __init__(self, *, best_bid, best_ask, depth=1000.0, stale=False, resolved=False):
        self.best_bid = D(best_bid)
        self.best_ask = D(best_ask)
        self.spread = self.best_ask - self.best_bid
        self.asks = {self.best_ask: D(depth)}
        self.bids = {self.best_bid: D(depth)}
        self.resolved = resolved
        self._stale = stale

    def is_stale(self, _ms):
        return self._stale


# --- fill probability model -------------------------------------------------
def test_fill_probability_bounded_and_monotone():
    m = RealisticFillModel()
    base = dict(spread=0.02, depth_usd=1000.0, order_usd=50.0, book_age_ms=200,
                volatility=0.01, queue_proxy=0.2, aggressiveness=1.0)
    p = m.fill_probability(**base)
    assert 0.0 <= p <= 1.0
    # tighter spread -> higher; wider -> lower
    assert m.fill_probability(**{**base, "spread": 0.005}) >= p
    assert m.fill_probability(**{**base, "spread": 0.07}) <= p
    # more depth -> higher; bigger order -> lower
    assert m.fill_probability(**{**base, "depth_usd": 5000.0}) >= p
    assert m.fill_probability(**{**base, "order_usd": 800.0}) <= p
    # staler book -> lower; higher vol -> lower; back of queue -> lower
    assert m.fill_probability(**{**base, "book_age_ms": 5000}) <= p
    assert m.fill_probability(**{**base, "volatility": 0.2}) <= p
    assert m.fill_probability(**{**base, "queue_proxy": 0.95}) <= p
    # more aggressive (deeper marketable cross) -> higher
    assert m.fill_probability(**{**base, "aggressiveness": 2.0}) >= p


def test_fill_probability_zero_on_stale_or_no_depth():
    m = RealisticFillModel()
    assert m.fill_probability(spread=0.01, depth_usd=1000, order_usd=10, stale=True) == 0.0
    assert m.fill_probability(spread=0.01, depth_usd=0.0, order_usd=10) == 0.0


def test_fill_fraction_partial_when_order_exceeds_depth_slice():
    m = RealisticFillModel(max_depth_fraction=Decimal("0.35"))
    # order far exceeds the executable depth slice -> fraction < 1
    frac = m.fill_fraction(order_usd=1000.0, depth_usd=1000.0)
    assert 0.0 < frac < 1.0
    # tiny order vs deep book -> full
    assert m.fill_fraction(order_usd=10.0, depth_usd=5000.0) == 1.0


# --- broker realistic mode --------------------------------------------------
def _order(coid, qty="100", price="0.55", tif=TimeInForce.IOC):
    return OrderRequest(client_order_id=coid, venue="polymarket", market_id="m1",
                        asset_id="a1", side=OrderSide.BUY, order_type=OrderType.MARKETABLE_LIMIT,
                        limit_price=D(price), quantity=D(qty), time_in_force=tif, venue_kind="pm")


def test_realistic_mode_not_guaranteed_fill_rate():
    # Marginal book (wide-ish spread, thin depth, small but non-trivial order):
    # realistic fills are probabilistic — NOT every order fills.
    broker = PaperBroker(realistic=True, reject_on_stale=False)
    book = FakeBook(best_bid="0.50", best_ask="0.56", depth=120.0)
    filled = 0
    for i in range(200):
        res = broker.execute(_order(f"co-{i}", qty="150", price="0.56"), book=book)
        if res.fills:
            filled += 1
    rate = filled / 200
    assert 0.0 < rate < 1.0          # neither always nor never fills
    # the model exposes the probability it used
    res = broker.execute(_order("co-diag", qty="150", price="0.56"), book=book)
    assert res.realistic is True
    assert res.fill_probability is not None and 0.0 <= res.fill_probability <= 1.0


def test_realistic_partial_fill_sets_remaining():
    broker = PaperBroker(realistic=True, reject_on_stale=False)
    # huge order vs thin depth -> partial fill (some remaining)
    book = FakeBook(best_bid="0.54", best_ask="0.55", depth=100.0)
    saw_partial = False
    for i in range(50):
        res = broker.execute(_order(f"p-{i}", qty="500", price="0.56"), book=book)
        if res.status == OrderStatus.PARTIALLY_FILLED:
            assert res.remaining > 0 and res.filled_quantity > 0
            assert res.partial_fill is True
            saw_partial = True
    assert saw_partial


def test_default_mode_unchanged_full_fill():
    # Default (non-realistic) broker keeps deterministic depth-limited fills.
    broker = PaperBroker(reject_on_stale=False)
    book = FakeBook(best_bid="0.54", best_ask="0.55", depth=100000.0)
    res = broker.execute(_order("d1", qty="10", price="0.56"), book=book)
    assert res.status == OrderStatus.FILLED
    assert res.realistic is False


def test_high_quality_book_fills_reliably():
    broker = PaperBroker(realistic=True, reject_on_stale=False)
    book = FakeBook(best_bid="0.549", best_ask="0.551", depth=50000.0)  # tight + deep
    filled = sum(1 for i in range(100)
                 if broker.execute(_order(f"hq-{i}", qty="20", price="0.56"), book=book).fills)
    assert filled / 100 > 0.9        # high-quality book fills almost always


# --- slippage impact + markout ---------------------------------------------
def test_impact_adjust_worse_with_size_and_volatility():
    s = SlippageModel(slippage_bps=Decimal("25"))
    base = s.impact_adjust(D("0.50"), OrderSide.BUY, spread=D("0.01"),
                           order_usd=50.0, depth_usd=1000.0, volatility=0.0)
    bigger = s.impact_adjust(D("0.50"), OrderSide.BUY, spread=D("0.01"),
                             order_usd=800.0, depth_usd=1000.0, volatility=0.0)
    volwise = s.impact_adjust(D("0.50"), OrderSide.BUY, spread=D("0.01"),
                              order_usd=50.0, depth_usd=1000.0, volatility=0.2)
    assert base >= D("0.50")                 # BUY only ever pays up
    assert bigger > base                     # bigger order -> worse
    assert volwise > base                    # more volatile -> worse


def test_markout_bps_adverse_for_buyer_when_mid_falls():
    # BUY filled at 0.55, mid later 0.53 -> adverse (negative markout)
    mk = markout_bps(D("0.55"), D("0.53"), "BUY")
    assert mk is not None and mk < 0
    # favourable when mid rises
    assert markout_bps(D("0.55"), D("0.57"), "BUY") > 0
