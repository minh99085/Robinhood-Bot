"""Profit-discovery upgrades: multi-window arb, UP block, bankroll cap, dependency LCMM."""

from __future__ import annotations

from engine.pulse.markets import OrderBook, PulseWindow, SERIES_SLUG_5M, SERIES_SLUG_15M
from engine.pulse.engine import PulseEngine, PulseConfig
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.dependency_arb import scan_windows, group_nested_windows


def _book(ask=0.45, bid=0.40):
    return OrderBook(best_bid=bid, best_ask=ask, ask_depth_usd=50000, bid_depth_usd=50000,
                     asks=[(ask, 100000.0)], bids=[(bid, 100000.0)])


class _MultiArbMkt:
    def __init__(self, windows):
        self._windows = windows

    def active_windows(self, now=None, **kw):
        return list(self._windows)

    def hydrate_books(self, w):
        w.up_book = _book(0.45)
        w.down_book = _book(0.45)
        return w

    def fetch_resolution(self, market_id):
        return True

    def report(self):
        return {"multi_series": True}


def _engine(tmp_path, market, **cfg_over):
    t0 = 10_000_000.0
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 1.0
        return price["p"]

    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(
        tick_seconds=1.0, size_usd=5.0, min_edge=0.02, basis_buffer=0.0,
        min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
        settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
        selectivity_exploration_rate=0.0, data_dir=str(tmp_path),
        directional_block_up_until_promoted=True,
        directional_max_bankroll_frac=0.10,
        starting_capital_usd=500.0,
        arb_epsilon=0.05,
        **cfg_over,
    )
    return PulseEngine(cfg, market_feed=market, price_feed=feed), t0


def test_multi_window_arb_scans_both_series(tmp_path):
    t0 = 10_000_000.0
    w5 = PulseWindow(event_id="e5", market_id="m5", slug="s5", title="5m",
                     open_ts=t0, close_ts=t0 + 300, up_token_id="U5", down_token_id="D5",
                     series_slug=SERIES_SLUG_5M, window_seconds=300, series_label="5m")
    w15 = PulseWindow(event_id="e15", market_id="m15", slug="s15", title="15m",
                      open_ts=t0, close_ts=t0 + 900, up_token_id="U15", down_token_id="D15",
                      series_slug=SERIES_SLUG_15M, window_seconds=900, series_label="15m")
    eng, t0 = _engine(
        tmp_path, _MultiArbMkt([w5, w15]),
        directional_enabled=False,
        arb_max_usd=40.0,
        arb_global_max_open_usd=500.0,
        arb_nonatomic_enabled=False,
    )
    for i in range(8):
        eng.tick(now=t0 + i)
    arb = eng.status()["arbitrage"]
    assert arb["executed"] >= 2
    assert arb.get("windows_by_series", {}).get("5m", 0) >= 1
    assert arb.get("windows_by_series", {}).get("15m", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True


def test_dependency_nested_implication_detected():
    t0 = 10_000_000.0
    p = PulseWindow(event_id="p", market_id="mp", slug="sp", title="15m",
                    open_ts=t0, close_ts=t0 + 900, up_token_id="UP", down_token_id="DP",
                    window_seconds=900, series_label="15m")
    c = PulseWindow(event_id="c", market_id="mc", slug="sc", title="5m",
                    open_ts=t0 + 60, close_ts=t0 + 360, up_token_id="UC", down_token_id="DC",
                    window_seconds=300, series_label="5m")
    p.up_book = OrderBook(best_bid=0.40, best_ask=0.42, asks=[(0.42, 1000.0)],
                          bids=[(0.40, 1000.0)])
    p.down_book = _book()
    c.up_book = OrderBook(best_bid=0.55, best_ask=0.57, asks=[(0.57, 1000.0)],
                          bids=[(0.55, 1000.0)])
    c.down_book = _book()
    groups = group_nested_windows([p, c])
    assert len(groups) == 1
    vios = scan_windows([p, c], epsilon=0.02)
    assert len(vios) >= 1
    assert vios[0].constraint_type == "nested_implication"


def test_up_block_gate_configured_until_promoted(tmp_path):
    t0 = 10_000_000.0
    w = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC",
                    open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    eng, _ = _engine(tmp_path, _MultiArbMkt([w]), arbitrage_enabled=False)
    assert eng.cfg.directional_block_up_until_promoted is True
    assert eng._up_direction_promoted() is False
    risk = eng.light_report()["directional_risk"]
    assert risk["block_up_until_promoted"] is True
    assert risk["up_promoted"] is False


def test_arb_global_open_cap_limits_second_window(tmp_path):
    t0 = 10_000_000.0
    w5 = PulseWindow(event_id="e5", market_id="m5", slug="s5", title="5m",
                     open_ts=t0, close_ts=t0 + 300, up_token_id="U5", down_token_id="D5",
                     series_slug=SERIES_SLUG_5M, window_seconds=300, series_label="5m")
    w15 = PulseWindow(event_id="e15", market_id="m15", slug="s15", title="15m",
                      open_ts=t0, close_ts=t0 + 900, up_token_id="U15", down_token_id="D15",
                      series_slug=SERIES_SLUG_15M, window_seconds=900, series_label="15m")
    eng, t0 = _engine(
        tmp_path, _MultiArbMkt([w5, w15]),
        directional_enabled=False,
        arb_global_max_open_usd=45.0,
        arb_max_usd=25.0,
        arb_nonatomic_enabled=False,
    )
    for i in range(6):
        eng.tick(now=t0 + i)
    arb = eng.status()["arbitrage"]
    assert arb["executed"] >= 1
    open_exp = sum(
        float(p.get("cost_usd") or 0.0)
        for p in eng.arb_ledger.positions.values()
        if p.get("status") == "open"
    )
    assert open_exp <= 45.0 + 1e-6
    assert arb["executed"] == 1


def test_directional_bankroll_cap_in_report(tmp_path):
    t0 = 10_000_000.0
    w = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC",
                    open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    eng, t0 = _engine(tmp_path, _MultiArbMkt([w]))
    eng.tick(now=t0 + 5)
    cap = eng.light_report()["directional_risk"]
    assert cap["bankroll_cap_usd"] == 50.0
    assert cap["block_up_until_promoted"] is True