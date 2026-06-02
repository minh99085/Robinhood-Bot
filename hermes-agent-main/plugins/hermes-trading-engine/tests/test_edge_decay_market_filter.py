"""Edge-decay model + timing filter.

Quant scope — *Statistical & Probabilistic Modeling* + *Strategy Optimization &
Robustness Testing* + *CLOB v2 Execution*: proves edge decays with execution
delay, wide spread, thin liquidity, short time-to-resolution, and by strategy /
market type, that the edge half-life is well-defined, and that a fully-decayed
edge yields a wait/skip timing decision. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.profitability_governor import (
    EdgeDecayModel, timing_decision, STATE_CLEAN, STATE_GRAYLIST, STATE_BLACKLIST)


def _kw(**kw):
    base = dict(strategy="directional", market_type="binary", liquidity_usd=50000.0,
                spread=0.01, time_to_resolution_s=7 * 86400.0, execution_delay_s=1.0)
    base.update(kw)
    return base


def test_decay_factor_in_unit_interval_and_reduces_edge():
    m = EdgeDecayModel()
    f = m.decay_factor(**_kw())
    assert 0.0 < f <= 1.0
    assert m.decayed_edge(0.05, **_kw()) < 0.05


def test_decay_worse_with_delay_spread_thin_liquidity_short_ttr():
    m = EdgeDecayModel()
    base = m.decay_factor(**_kw())
    assert m.decay_factor(**_kw(execution_delay_s=600.0)) < base
    assert m.decay_factor(**_kw(spread=0.08)) < base
    assert m.decay_factor(**_kw(liquidity_usd=200.0)) < base
    assert m.decay_factor(**_kw(time_to_resolution_s=120.0)) < base


def test_bregman_decays_slower_than_directional():
    m = EdgeDecayModel()
    breg = m.decay_factor(**_kw(strategy="bregman"))
    direc = m.decay_factor(**_kw(strategy="directional"))
    assert breg >= direc


def test_edge_half_life_shorter_in_fragile_regime():
    m = EdgeDecayModel()
    stable = m.edge_half_life(**_kw())
    fragile = m.edge_half_life(**_kw(spread=0.08, liquidity_usd=200.0,
                                     time_to_resolution_s=120.0))
    assert fragile < stable
    assert stable > 0.0


def test_fully_decayed_edge_does_not_trade_now():
    m = EdgeDecayModel()
    # huge execution delay collapses the decay factor toward 0
    f = m.decay_factor(**_kw(execution_delay_s=100000.0, spread=0.08, liquidity_usd=100.0))
    d = timing_decision(net_edge=0.05, decay_factor=f, graylist_state=STATE_CLEAN,
                        aggressive=False)
    assert d in ("wait", "skip")


def test_timing_decision_paths():
    # strong edge + fresh -> trade now
    assert timing_decision(net_edge=0.05, decay_factor=0.95, graylist_state=STATE_CLEAN,
                           aggressive=False) == "trade_now"
    # positive edge but decaying -> wait
    assert timing_decision(net_edge=0.05, decay_factor=0.2, graylist_state=STATE_CLEAN,
                           aggressive=False) == "wait"
    # negative edge -> skip
    assert timing_decision(net_edge=-0.01, decay_factor=0.95, graylist_state=STATE_CLEAN,
                           aggressive=False) == "skip"
    # graylisted + aggressive -> tiny exploration
    assert timing_decision(net_edge=0.05, decay_factor=0.95, graylist_state=STATE_GRAYLIST,
                           aggressive=True) == "tiny_exploration"
    # graylisted + conservative -> skip
    assert timing_decision(net_edge=0.05, decay_factor=0.95, graylist_state=STATE_GRAYLIST,
                           aggressive=False) == "skip"
    # blacklisted -> always skip
    assert timing_decision(net_edge=0.05, decay_factor=0.95, graylist_state=STATE_BLACKLIST,
                           aggressive=True) == "skip"
