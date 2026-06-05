"""Tests for engine.features.oracle_features (pure feature transforms).

Tests-first: cover anchor staleness penalty, short-horizon returns, realized
volatility, microtrend, trend persistence, feed disagreement, market-close
proximity, the Bregman risk-filter contract (never arbitrage proof), and the
read-only feed adapters. No network, deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.features import oracle_features as of


# --------------------------------------------------------------------------- #
# disagreement
# --------------------------------------------------------------------------- #
def test_disagreement_bps_basic():
    assert of.disagreement_bps(101.0, 100.0) == 100.0
    assert of.disagreement_bps(100.0, 100.0) == 0.0
    assert of.disagreement_bps(None, 100.0) is None
    assert of.disagreement_bps(100.0, 0.0) is None
    assert of.disagreement_bps(-1.0, 100.0) is None


# --------------------------------------------------------------------------- #
# stale anchor penalty
# --------------------------------------------------------------------------- #
def test_stale_anchor_penalty_fresh_decay_stale():
    # Within heartbeat -> full confidence.
    assert of.stale_anchor_penalty(0, 7200, 3600) == 1.0
    assert of.stale_anchor_penalty(3600, 7200, 3600) == 1.0
    # At/after max age -> zero confidence.
    assert of.stale_anchor_penalty(7200, 7200, 3600) == 0.0
    assert of.stale_anchor_penalty(9000, 7200, 3600) == 0.0
    # Midway between heartbeat and max age -> ~0.5.
    assert abs(of.stale_anchor_penalty(5400, 7200, 3600) - 0.5) < 1e-6
    # Unknown age -> fully penalized.
    assert of.stale_anchor_penalty(None, 7200, 3600) == 0.0


# --------------------------------------------------------------------------- #
# realized volatility / microtrend / trend persistence
# --------------------------------------------------------------------------- #
def test_realized_volatility():
    import statistics
    rets = [0.01, -0.01, 0.02, -0.02]
    assert of.realized_volatility(rets) == round(statistics.stdev(rets), 10)
    assert of.realized_volatility([0.01]) is None
    assert of.realized_volatility([]) is None


def test_microtrend_direction():
    assert of.microtrend([0.01, 0.02, 0.03]) == 1.0
    assert of.microtrend([-0.01, -0.02]) == -1.0
    assert of.microtrend([0.01, -0.01]) == 0.0
    assert of.microtrend([]) == 0.0


def test_trend_persistence():
    assert of.trend_persistence([0.01, 0.02, 0.03]) == 1.0       # all same sign
    assert of.trend_persistence([0.01, -0.01, 0.01, -0.01]) == 0.0  # alternating
    assert of.trend_persistence([0.0, 0.0]) is None              # no non-zero


# --------------------------------------------------------------------------- #
# returns from history
# --------------------------------------------------------------------------- #
def test_returns_from_history():
    hist = [(0, 100.0), (240, 100.0), (270, 105.0), (300, 110.0)]
    rets = of.returns_from_history(hist, now=300, horizons=(30, 60, 300))
    assert abs(rets[60] - 0.10) < 1e-9          # vs price at ts<=240 (100)
    assert abs(rets[300] - 0.10) < 1e-9         # vs earliest (100)
    assert rets[30] is not None and rets[30] > 0  # vs 105 -> +4.76%


def test_returns_from_history_empty():
    assert of.returns_from_history([], now=0) == {30: None, 60: None, 300: None}


# --------------------------------------------------------------------------- #
# market close proximity
# --------------------------------------------------------------------------- #
def test_market_close_features():
    near = of.build_market_close_features(now=100, close_ts=130, window_seconds=300)
    assert near.seconds_to_close == 30
    assert abs(near.close_proximity - 0.9) < 1e-6
    assert near.within_close_window is True

    past = of.build_market_close_features(now=200, close_ts=100, window_seconds=300)
    assert past.close_proximity == 1.0 and past.within_close_window is True

    far = of.build_market_close_features(now=0, close_ts=1000, window_seconds=300)
    assert far.close_proximity == 0.0 and far.within_close_window is False

    unknown = of.build_market_close_features(now=None, close_ts=None)
    assert unknown.seconds_to_close is None and unknown.close_proximity == 0.0


# --------------------------------------------------------------------------- #
# builders + adapters
# --------------------------------------------------------------------------- #
def _anchor_status(age=1800, price=62000.0):
    return {"price": price, "age_seconds": age, "heartbeat_seconds": 3600,
            "max_age_seconds": 7200, "stale": age >= 7200, "valid": age < 7200}


def test_build_anchor_features_from_dict_and_object():
    a = of.build_anchor_features(_anchor_status(age=1800))
    assert a.present and a.confidence_multiplier == 1.0 and a.stale_penalty == 0.0
    a2 = of.build_anchor_features(_anchor_status(age=5400))
    assert abs(a2.confidence_multiplier - 0.5) < 1e-6
    # object (duck-typed) works too
    obj = SimpleNamespace(price=62000.0, age_seconds=1800, heartbeat_seconds=3600,
                          max_age_seconds=7200, stale=False, valid=True)
    assert of.build_anchor_features(obj).present is True
    # missing anchor
    none = of.build_anchor_features(None)
    assert none.present is False and none.confidence_multiplier == 0.0


def test_build_fast_features_from_returns_map():
    fast = {"price": 62100.0, "age_seconds": 2.0, "valid": True}
    f = of.build_fast_features(fast, returns={30: 0.001, 60: 0.002, 300: 0.005})
    assert f.present and f.samples == 3
    assert f.returns[60] == 0.002
    assert f.realized_vol is not None
    assert f.microtrend == 1.0


def test_build_fast_features_from_history():
    hist = [(0, 100.0), (240, 100.0), (270, 105.0), (300, 110.0)]
    f = of.build_fast_features({"price": 110.0}, history=hist, now=300)
    assert f.returns[60] is not None


def test_build_cross_features():
    c = of.build_cross_features(62000.0, 62100.0, max_disagreement_bps=150.0)
    assert c.disagreement_bps is not None and c.agree is True
    c2 = of.build_cross_features(62000.0, 70000.0, max_disagreement_bps=150.0)
    assert c2.agree is False


# --------------------------------------------------------------------------- #
# Bregman risk-filter contract
# --------------------------------------------------------------------------- #
def test_risk_filter_allows_fresh_agreeing_feeds():
    fs = of.build_oracle_features(
        anchor=_anchor_status(age=1800), fast={"price": 62100.0, "valid": True},
        fast_returns={30: 0.001, 60: 0.001, 300: 0.002}, now=1000, market_close_ts=999999)
    rf = fs.risk_filter
    assert rf["allow"] is True
    assert rf["is_arbitrage_proof"] is False
    assert 0.0 < rf["size_multiplier"] <= 1.0


def test_risk_filter_vetoes_stale_anchor():
    fs = of.build_oracle_features(
        anchor=_anchor_status(age=7200), fast={"price": 62100.0}, now=1000)
    assert fs.risk_filter["allow"] is False
    assert "anchor_stale" in fs.risk_filter["reasons"]
    assert fs.risk_filter["is_arbitrage_proof"] is False


def test_risk_filter_vetoes_feed_disagreement():
    fs = of.build_oracle_features(
        anchor=_anchor_status(age=1800, price=62000.0),
        fast={"price": 70000.0}, fast_returns={30: 0.0}, now=1000,
        max_disagreement_bps=150.0)
    assert fs.risk_filter["allow"] is False
    assert "feed_disagreement" in fs.risk_filter["reasons"]
    assert fs.risk_filter["is_arbitrage_proof"] is False


def test_risk_filter_missing_anchor_blocks():
    fs = of.build_oracle_features(anchor=None, fast={"price": 62100.0})
    assert fs.risk_filter["allow"] is False
    assert "anchor_missing" in fs.risk_filter["reasons"]


def test_risk_filter_never_proves_arbitrage_in_any_case():
    # Contract: regardless of inputs, the filter is never arbitrage proof.
    for kwargs in (
        {},
        {"anchor": _anchor_status(age=1800), "fast": {"price": 62000.0}},
        {"anchor": _anchor_status(age=7200)},
    ):
        fs = of.build_oracle_features(**kwargs)
        assert fs.risk_filter["is_arbitrage_proof"] is False


def test_risk_filter_near_close_shrinks_size():
    far = of.build_oracle_features(
        anchor=_anchor_status(age=0), fast={"price": 62000.0},
        fast_returns={30: 0.0}, now=0, market_close_ts=10_000, close_window_seconds=300)
    near = of.build_oracle_features(
        anchor=_anchor_status(age=0), fast={"price": 62000.0},
        fast_returns={30: 0.0}, now=0, market_close_ts=30, close_window_seconds=300)
    assert near.risk_filter["size_multiplier"] < far.risk_filter["size_multiplier"]
    assert "near_close" in near.risk_filter["reasons"]


# --------------------------------------------------------------------------- #
# feed adapter + serialization + safety
# --------------------------------------------------------------------------- #
def test_returns_from_feed_uses_return_over():
    calls = {}

    class _Feed:
        def return_over(self, seconds, now=None):
            calls[seconds] = now
            return {30.0: 0.001, 60.0: 0.002, 300.0: 0.003}.get(seconds)

    out = of.returns_from_feed(_Feed(), now=123, horizons=(30, 60, 300))
    assert out[30] == 0.001 and out[300] == 0.003
    assert calls[30.0] == 123


def test_returns_from_feed_falls_back_to_hist():
    feed = SimpleNamespace(_hist=[(0, 100.0), (300, 110.0)])
    out = of.returns_from_feed(feed, now=300, horizons=(300,))
    assert out[300] is not None


def test_build_oracle_features_all_none_is_safe():
    fs = of.build_oracle_features()
    assert fs.anchor.present is False
    assert fs.fast.present is False
    assert fs.risk_filter["allow"] is False
    d = fs.to_dict()
    assert set(d) == {"anchor", "fast", "cross", "market_close", "risk_filter"}


def test_quant_responsibilities_documented():
    for domain in ("acquisition_ingestion", "preprocessing_features",
                   "probabilistic_modeling", "bregman_signal_development",
                   "risk_portfolio", "backtesting", "optimization_robustness",
                   "clobv2_execution", "monitoring", "compliance_security_ops"):
        assert domain in of.QUANT_RESPONSIBILITIES
        assert of.QUANT_RESPONSIBILITIES[domain]
