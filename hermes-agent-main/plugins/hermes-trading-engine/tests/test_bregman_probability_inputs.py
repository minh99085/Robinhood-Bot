"""Bregman arbitrage fair-probability preparation (TDD, deterministic, offline).

Quant scope exercised here:
* Bregman arbitrage fair-probability preparation — group related markets by their
  linked Chainlink feed and prepare a consistent fair-probability set (the
  Bregman/squared-loss centroid + per-market deviations) for a downstream
  arbitrage projection. This is INPUT PREPARATION ONLY: it never executes,
  sizes, approves, or overrides certified arbitrage math.
* Risk Management — markets flagged no-trade (e.g. stale oracle) are excluded
  from the prepared fair-probability set.

No randomness, no network, no Grok call.
"""

from __future__ import annotations

from engine.feeds.chainlink import ChainlinkReading, StaticChainlinkSource
from engine.feeds.chainlink_registry import load_registry
from engine.chainlink_scanner import ChainlinkScanner
from engine.markets import universe_manager as um
from engine.research.ensemble import prepare_bregman_fair_probabilities
from engine.training.config import TrainingConfig
from engine.training.probability_stack import ProbabilityStack

from tests._pmtrain_helpers import FakeResearch, market

_NOW = 1_000_000.0
_ETH = "ETH/USD"


def _reading(value: float, updated_at: float) -> ChainlinkReading:
    return ChainlinkReading(_ETH, int(value * 1e8), 8, updated_at, 1, updated_at)


def _series(base: float, n: int, t0: float) -> list:
    return [_reading(base + i, t0 + i * 60.0) for i in range(n)]


def _scanner(readings: list) -> ChainlinkScanner:
    return ChainlinkScanner(StaticChainlinkSource({_ETH: readings}),
                            registry={_ETH: load_registry()[_ETH]})


def _eth_rec(i: int, threshold: int, *, bid=0.28, ask=0.30) -> um.MarketRecord:
    raw = market(i, bid=bid, ask=ask, category="crypto", now=_NOW)
    raw["question"] = f"Will ETH be above ${threshold} on date?"
    raw["slug"] = f"eth-above-{threshold}"
    return um.MarketRecord.from_raw(raw, now=_NOW)


# --------------------------------------------------------------------------- #
def test_linked_markets_share_bregman_group_id():
    scanner = _scanner(_series(3000.0, 8, _NOW - 480))
    stack = ProbabilityStack(TrainingConfig(), chainlink=scanner)
    e1 = stack.estimate(_eth_rec(0, 2500), FakeResearch(fair=0.80), now=_NOW)
    e2 = stack.estimate(_eth_rec(1, 2800), FakeResearch(fair=0.70), now=_NOW)
    assert e1.bregman_group_id == e2.bregman_group_id == _ETH


def test_prepare_groups_and_centroid():
    estimates = [
        {"market_id": "a", "bregman_group_id": _ETH, "calibrated_probability": 0.80,
         "no_trade_probability_reason": ""},
        {"market_id": "b", "bregman_group_id": _ETH, "calibrated_probability": 0.60,
         "no_trade_probability_reason": ""},
    ]
    prep = prepare_bregman_fair_probabilities(estimates)
    assert _ETH in prep
    grp = prep[_ETH]
    assert grp["n"] == 2
    assert abs(grp["consensus"] - 0.70) < 1e-9          # squared-loss centroid = mean
    devs = {m["market_id"]: m["deviation"] for m in grp["members"]}
    assert abs(devs["a"] - 0.10) < 1e-9
    assert abs(devs["b"] + 0.10) < 1e-9


def test_no_trade_markets_excluded_from_preparation():
    estimates = [
        {"market_id": "a", "bregman_group_id": _ETH, "calibrated_probability": 0.80,
         "no_trade_probability_reason": ""},
        {"market_id": "b", "bregman_group_id": _ETH, "calibrated_probability": 0.60,
         "no_trade_probability_reason": "chainlink_stale_or_irrelevant"},
    ]
    prep = prepare_bregman_fair_probabilities(estimates)
    members = prep[_ETH]["members"]
    assert [m["market_id"] for m in members] == ["a"]
    assert prep[_ETH]["n"] == 1


def test_unlinked_estimates_have_no_group():
    estimates = [
        {"market_id": "x", "bregman_group_id": "", "calibrated_probability": 0.5,
         "no_trade_probability_reason": ""},
    ]
    assert prepare_bregman_fair_probabilities(estimates) == {}


def test_preparation_is_deterministic():
    estimates = [
        {"market_id": "a", "bregman_group_id": _ETH, "calibrated_probability": 0.81},
        {"market_id": "b", "bregman_group_id": _ETH, "calibrated_probability": 0.59},
        {"market_id": "c", "bregman_group_id": _ETH, "calibrated_probability": 0.70},
    ]
    assert (prepare_bregman_fair_probabilities(estimates)
            == prepare_bregman_fair_probabilities(list(estimates)))


def test_preparation_does_not_execute_or_size():
    """Output is pure fair-probability preparation — no order/size/approval keys."""
    estimates = [{"market_id": "a", "bregman_group_id": _ETH,
                  "calibrated_probability": 0.8}]
    prep = prepare_bregman_fair_probabilities(estimates)
    grp = prep[_ETH]
    forbidden = ("order", "size", "place", "submit", "arm", "approve")
    for key in grp:
        assert not any(f in key.lower() for f in forbidden)
