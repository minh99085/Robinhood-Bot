"""Fast read-only BTC spot feed (PAPER ONLY, key-less)."""

from __future__ import annotations

from engine.feeds.btc_fast_price import BtcFastPriceFeed, disagreement_bps


def test_fresh_fetch_is_valid():
    f = BtcFastPriceFeed(fetch=lambda: 65000.0, clock=lambda: 1000.0, max_age_seconds=10)
    st = f.read(now=1000.0)
    assert st.valid is True
    assert st.stale is False
    assert st.price == 65000.0
    assert st.age_seconds == 0.0


def test_missing_price_is_invalid():
    f = BtcFastPriceFeed(fetch=lambda: None, max_age_seconds=10)
    st = f.read(now=1000.0)
    assert st.valid is False
    assert st.error == "missing_price"
    assert st.consecutive_failures == 1


def test_stale_after_failure_gap():
    prices = iter([65000.0, None])
    f = BtcFastPriceFeed(fetch=lambda: next(prices), max_age_seconds=10)
    f.read(now=1000.0)              # success
    st = f.read(now=1020.0)        # fail, 20s since last success > 10s
    assert st.valid is False
    assert st.stale is True
    assert st.age_seconds == 20.0


def test_short_horizon_returns():
    seq = iter([100.0, 101.0, 102.0])
    f = BtcFastPriceFeed(fetch=lambda: next(seq))
    f.read(now=0.0)
    f.read(now=40.0)
    f.read(now=80.0)
    r30 = f.return_over(30, now=80.0)   # price at ts<=50 is 101.0 (ts=40)
    assert r30 is not None
    assert abs(r30 - (102.0 / 101.0 - 1.0)) < 1e-9


def test_disagreement_bps_helper():
    assert disagreement_bps(65000.0, 65000.0) == 0.0
    d = disagreement_bps(65000.0, 65325.0)
    assert 49.0 < d < 51.0
    assert disagreement_bps(0.0, 100.0) is None


def test_disagreement_recorded_on_read():
    f = BtcFastPriceFeed(fetch=lambda: 65000.0, max_age_seconds=10)
    st = f.read(now=1000.0, anchor_price=65325.0)
    assert st.disagreement_vs_chainlink_bps is not None
    assert 49.0 < st.disagreement_vs_chainlink_bps < 51.0


def test_disabled_feed():
    f = BtcFastPriceFeed(enabled=False, fetch=lambda: 65000.0)
    st = f.read(now=1.0)
    assert st.enabled is False
    assert st.valid is False


def test_status_has_required_fields():
    f = BtcFastPriceFeed(fetch=lambda: 65000.0)
    d = f.read(now=1.0).to_dict()
    for k in ("enabled", "provider", "symbol", "price", "observed_at", "age_seconds",
              "stale", "valid", "error", "consecutive_failures",
              "disagreement_vs_chainlink_bps"):
        assert k in d, k
