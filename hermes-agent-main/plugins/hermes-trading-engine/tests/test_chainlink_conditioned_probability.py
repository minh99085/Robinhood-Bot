"""Chainlink-conditioned probability tests (TDD, deterministic, offline).

Quant scope exercised here:
* Data Acquisition & Ingestion — deterministic, timestamp-safe Chainlink
  readings (no RPC, no network).
* Data Preprocessing & Feature Engineering — oracle features surfaced on the
  probability estimate.
* Statistical & Probabilistic Modeling — a fresh, relevant oracle conditions the
  fair probability; a stale/inconsistent one does not.
* Risk Management — stale / missing / inconsistent oracle data can NEVER make the
  probability more aggressive than fresh data; it sets a no-trade reason instead.
* Bregman arbitrage preparation — a linked oracle assigns a `bregman_group_id`.

No randomness, no network, no Grok call.
"""

from __future__ import annotations

from engine.feeds.chainlink import ChainlinkReading, StaticChainlinkSource
from engine.feeds.chainlink_registry import load_registry
from engine.chainlink_scanner import ChainlinkScanner
from engine.markets import universe_manager as um
from engine.training.config import TrainingConfig
from engine.training.probability_stack import ProbabilityStack, market_mid

from tests._pmtrain_helpers import FakeResearch, market

_NOW = 1_000_000.0
_ETH = "ETH/USD"


def _reading(value: float, updated_at: float, decimals: int = 8) -> ChainlinkReading:
    return ChainlinkReading(_ETH, int(value * 10 ** decimals), decimals,
                            updated_at, 1, updated_at)


def _series(base: float, n: int, t0: float, step: float = 60.0) -> list:
    return [_reading(base + i, t0 + i * step) for i in range(n)]


def _scanner(readings: list) -> ChainlinkScanner:
    src = StaticChainlinkSource({_ETH: readings})
    return ChainlinkScanner(src, registry={_ETH: load_registry()[_ETH]},
                            history_limit=30)


def _eth_market(i: int, threshold: int) -> um.MarketRecord:
    raw = market(i, bid=0.28, ask=0.30, liq=20_000, depth=1000, category="crypto",
                 now=_NOW)
    raw["question"] = f"Will ETH be above ${threshold} on date?"
    raw["slug"] = f"eth-above-{threshold}"
    return um.MarketRecord.from_raw(raw, now=_NOW)


def _stack(scanner: ChainlinkScanner) -> ProbabilityStack:
    return ProbabilityStack(TrainingConfig(), chainlink=scanner)


# --------------------------------------------------------------------------- #
# fresh oracle conditions the probability + surfaces features
# --------------------------------------------------------------------------- #
def test_fresh_oracle_populates_features_and_bregman_group():
    scanner = _scanner(_series(3000.0, 8, _NOW - 480))
    est = _stack(scanner).estimate(_eth_market(0, 2500), FakeResearch(fair=0.80),
                                   now=_NOW)
    assert est.chainlink_feed == _ETH
    assert est.chainlink_features and "freshness" in est.chainlink_features
    assert est.bregman_group_id == _ETH
    assert est.chainlink_no_trade is False
    assert not est.no_trade_probability_reason


# --------------------------------------------------------------------------- #
# stale oracle: no-trade reason + never more aggressive than fresh
# --------------------------------------------------------------------------- #
def test_stale_oracle_sets_no_trade_reason():
    stale = _scanner([_reading(3000.0, _NOW - 4 * 3600)])    # 4h old -> stale
    est = _stack(stale).estimate(_eth_market(0, 2500), FakeResearch(fair=0.80),
                                 now=_NOW)
    assert est.chainlink_no_trade is True
    assert est.no_trade_probability_reason == "chainlink_stale_or_irrelevant"


def test_stale_oracle_never_more_aggressive_than_fresh():
    mkt = _eth_market(0, 2500)
    mid = market_mid(mkt)
    fresh = _stack(_scanner(_series(3000.0, 8, _NOW - 480))).estimate(
        mkt, FakeResearch(fair=0.80), now=_NOW)
    stale = _stack(_scanner([_reading(3000.0, _NOW - 4 * 3600)])).estimate(
        mkt, FakeResearch(fair=0.80), now=_NOW)
    # stale oracle must not move the fair value further from the market than fresh
    assert abs(stale.p_final - mid) <= abs(fresh.p_final - mid) + 1e-9


def test_inconsistent_oracle_blocks():
    # a single bad print (>50% jump) flags the oracle inconsistent
    readings = _series(3000.0, 6, _NOW - 360)
    readings.append(_reading(9000.0, _NOW - 30))     # outlier jump
    est = _stack(_scanner(readings)).estimate(_eth_market(0, 2500),
                                              FakeResearch(fair=0.80), now=_NOW)
    assert est.chainlink_no_trade is True
    assert est.no_trade_probability_reason == "chainlink_stale_or_irrelevant"


# --------------------------------------------------------------------------- #
# unlinked market: chainlink abstains (does not block, does not move)
# --------------------------------------------------------------------------- #
def test_unlinked_market_abstains():
    scanner = _scanner(_series(3000.0, 8, _NOW - 480))
    raw = market(0, bid=0.40, ask=0.42, category="politics", now=_NOW)
    raw["question"] = "Will candidate X win the election?"
    raw["slug"] = "election-x"
    rec = um.MarketRecord.from_raw(raw, now=_NOW)
    est = _stack(scanner).estimate(rec, FakeResearch(fair=0.55), now=_NOW)
    assert est.chainlink_no_trade is False
    assert est.chainlink_feed == ""
    assert est.bregman_group_id == ""


def test_chainlink_uncertainty_rises_when_stale():
    fresh = _stack(_scanner(_series(3000.0, 8, _NOW - 480))).estimate(
        _eth_market(0, 2500), FakeResearch(fair=0.80), now=_NOW)
    stale = _stack(_scanner([_reading(3000.0, _NOW - 4 * 3600)])).estimate(
        _eth_market(0, 2500), FakeResearch(fair=0.80), now=_NOW)
    assert stale.uncertainty_components["chainlink"] >= fresh.uncertainty_components["chainlink"]
