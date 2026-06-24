"""Within-window risk-free arbitrage (dutch book) for BTC up/down 5-min (PAPER ONLY).

Proves: arb detected when up_vwap+down_vwap < 1-fees-eps; none when sum >= 1; depth cap respected;
partial-fill rejected; sell-both detected/log-only; ArbLedger books + settles deterministically and
keeps P&L SEGREGATED from the directional ledger.
"""

from __future__ import annotations

from engine.pulse.markets import OrderBook
from engine.pulse.arbitrage import detect_arbitrage, ArbLedger, ArbOpportunity


def _book(best_bid, best_ask, asks, bids=None, ts=0.0):
    bids = bids or [(best_bid, 1000.0)]
    return OrderBook(best_bid=best_bid, best_ask=best_ask,
                     ask_depth_usd=round(sum(p * s for p, s in asks), 2),
                     bid_depth_usd=round(sum(p * s for p, s in bids), 2),
                     asks=asks, bids=bids, ts=ts)


def test_arb_detected_when_sum_below_one():
    # up asks 0.45, down asks 0.45 -> vwap sum 0.90 < 1 - 0 - 0.05 -> actionable dutch book
    up = _book(0.44, 0.45, asks=[(0.45, 100000.0)])
    dn = _book(0.44, 0.45, asks=[(0.45, 100000.0)])
    opp = detect_arbitrage(up, dn, size_usd=5.0, fees=0.0, epsilon=0.05, max_depth_consume_frac=0.9)
    assert opp is not None and opp.kind == "buy_both" and opp.actionable is True
    assert abs(opp.ask_sum - 0.90) < 1e-9 and opp.guaranteed_profit_usd > 0
    assert opp.vwap_residual < 0 and opp.shares > 0


def test_no_arb_when_sum_at_or_above_one():
    # typical tight market: 0.52 + 0.49 = 1.01 -> NOT actionable
    up = _book(0.51, 0.52, asks=[(0.52, 100000.0)])
    dn = _book(0.48, 0.49, asks=[(0.49, 100000.0)])
    opp = detect_arbitrage(up, dn, size_usd=5.0, epsilon=0.05, max_depth_consume_frac=0.9)
    assert opp is None or opp.actionable is False
    # and just-below-1 but within epsilon (0.97) is also not actionable
    up2 = _book(0.47, 0.48, asks=[(0.48, 100000.0)])
    dn2 = _book(0.48, 0.49, asks=[(0.49, 100000.0)])
    o2 = detect_arbitrage(up2, dn2, size_usd=5.0, epsilon=0.05, max_depth_consume_frac=0.9)
    assert o2 is not None and o2.actionable is False and o2.reason == "below_epsilon"


def test_depth_cap_keeps_full_fill():
    # the 0.5 depth cap SHRINKS the size so it always fully fills (no partial) -> still actionable
    up = _book(0.44, 0.45, asks=[(0.45, 4.44)])      # ~ $2 notional
    dn = _book(0.44, 0.45, asks=[(0.45, 4.44)])
    opp = detect_arbitrage(up, dn, size_usd=5.0, epsilon=0.05, max_depth_consume_frac=0.5)
    assert opp is not None and opp.depth_capped is True and opp.actionable is True
    # per-leg notional capped to <= 50% of the ~$2 depth -> total (both legs) <= ~$2, fully filled
    assert opp.shares > 0 and opp.guaranteed_profit_usd > 0 and opp.cost_usd <= 2.0 + 1e-6


def test_partial_fill_rejected_without_cap():
    # disable the cap (frac huge); $5 target on a ~$2 ladder -> partial fill -> rejected
    up = _book(0.44, 0.45, asks=[(0.45, 4.44)])
    dn = _book(0.44, 0.45, asks=[(0.45, 4.44)])
    opp = detect_arbitrage(up, dn, size_usd=5.0, epsilon=0.05, max_depth_consume_frac=100.0)
    assert opp is not None and opp.actionable is False and opp.reason == "partial_fill"


def test_sell_both_detected_logonly():
    # bids sum > 1 + eps -> sell-both detected but NOT actionable (no paper short)
    up = _book(0.60, 0.62, asks=[(0.62, 100.0)])
    dn = _book(0.50, 0.52, asks=[(0.52, 100.0)])     # ask sum 1.14 (no buy arb); bids 1.10 > 1.05
    opp = detect_arbitrage(up, dn, size_usd=5.0, epsilon=0.05)
    assert opp is not None and opp.kind == "sell_both" and opp.actionable is False


def test_stale_book_rejected():
    up = _book(0.44, 0.45, asks=[(0.45, 100000.0)], ts=1000.0)
    dn = _book(0.44, 0.45, asks=[(0.45, 100000.0)], ts=1000.0)
    opp = detect_arbitrage(up, dn, size_usd=5.0, epsilon=0.05, max_depth_consume_frac=0.9,
                           now=1100.0, max_book_age_s=30.0)
    assert opp is not None and opp.actionable is False and opp.reason == "stale_book"


def test_arb_ledger_books_settles_segregated():
    led = ArbLedger()
    opp = ArbOpportunity(kind="buy_both", up_vwap=0.45, down_vwap=0.45, shares=11.0, cost_usd=9.9,
                         guaranteed_profit_usd=1.1, ask_sum=0.90, tob_residual=-0.1,
                         vwap_residual=-0.1, actionable=True, reason="ok")
    assert led.book("w1", opp, close_ts=2000.0, now=1000.0) is True
    assert led.book("w1", opp, close_ts=2000.0, now=1000.0) is False     # no double-book
    assert led.executed == 1 and round(led.guaranteed_booked_usd, 4) == 1.1
    assert led.settle_due(now=1500.0) == 0                               # before close
    assert led.settle_due(now=2000.0) == 1                               # at close -> settled
    rep = led.report()
    assert rep["risk_free"] is True and rep["segregated_from_directional"] is True
    assert rep["settled"] == 1 and round(rep["realized_profit_usd"], 4) == 1.1
    # deterministic: profit booked regardless of which side won (no outcome dependence)
    st = led.to_state(); led2 = ArbLedger(); led2.load_state(st)
    assert led2.executed == 1 and round(led2.realized_profit_usd, 4) == 1.1


# ============================ engine end-to-end =========================================== #
from engine.pulse.markets import PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


class _ArbMkt:
    """Crossed books: up_ask + down_ask = 0.90 < 1 -> a risk-free dutch book every window."""
    def __init__(self, w, *, arb):
        self._w, self._arb = w, arb

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        a_up = 0.45 if self._arb else 0.55
        a_dn = 0.45 if self._arb else 0.55       # 0.90 -> arb; 1.10 -> none
        w.up_book = OrderBook(best_bid=a_up - 0.01, best_ask=a_up, ask_depth_usd=50000,
                              bid_depth_usd=50000, asks=[(a_up, 100000.0)], bids=[(a_up - 0.01, 100000.0)])
        w.down_book = OrderBook(best_bid=a_dn - 0.01, best_ask=a_dn, ask_depth_usd=50000,
                                bid_depth_usd=50000, asks=[(a_dn, 100000.0)], bids=[(a_dn - 0.01, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _arb_engine(tmp_path, *, arb=True, **over):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      selectivity_exploration_rate=0.0, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_ArbMkt(win, arb=arb), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_engine_takes_arb_first_segregated_and_reconciled(tmp_path):
    # crossed books -> the engine books the risk-free arb (separate ledger), takes NO directional
    # trade on that window, settles deterministically, and global reconciliation still holds.
    eng, t0 = _arb_engine(tmp_path, arb=True)
    _drive(eng, t0)
    arb = eng.status()["arbitrage"]
    assert arb["executed"] >= 1 and arb["settled"] >= 1 and arb["realized_profit_usd"] > 0
    assert eng.ledger.trades == 0                          # directional skipped on the arb window
    assert arb["segregated_from_directional"] is True and arb["risk_free"] is True
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["live_trading_enabled"] is False and eng.status()["paper_only"] is True


def test_engine_no_arb_when_books_not_crossed(tmp_path):
    eng, t0 = _arb_engine(tmp_path, arb=False)             # ask sum 1.10 -> no dutch book
    _drive(eng, t0)
    assert eng.status()["arbitrage"]["executed"] == 0
    assert eng.light_report()["global_reconciled"] is True


def test_directional_allowlist_blocks_unproven_then_allows_winning(tmp_path):
    # allowlist ON + no proven-winning bucket -> directional candidate hard-blocked pre-execution.
    eng, t0 = _arb_engine(tmp_path, arb=False, directional_require_winning_bucket=True,
                          selectivity_min_samples=30)
    _drive(eng, t0)
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("directional_allowlist", 0) >= 1
    assert eng.ledger.trades == 0
    assert eng.light_report()["global_reconciled"] is True
    # with the allowlist OFF the same candidate is free to trade (no winning-bucket requirement)
    eng2, t02 = _arb_engine(tmp_path, arb=False, directional_require_winning_bucket=False)
    _drive(eng2, t02)
    assert eng2.ledger.trades >= 1


def test_grok_decider_observe_only_by_default(tmp_path):
    # default mode is observe-only (shadow): Grok may grade but must NOT affect trading.
    eng, _ = _arb_engine(tmp_path, arb=False)
    assert eng.cfg.grok_decider_mode == "shadow"
    if eng.grok_decider is not None:
        assert eng.grok_decider.affects_trading() is False
