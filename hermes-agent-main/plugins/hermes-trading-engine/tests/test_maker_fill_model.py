"""Priority-C: maker/passive-fill cost model.

A patient passive entry recovers a CONSERVATIVE fraction of the spread (never the whole
spread, floored at the real bid), gated to fresh/deep/tight books. This lowers the
after-cost entry cost so more directional candidates clear the credible-edge gate — without
any fabricated/reference fills. Disabled (fraction 0) reproduces the taker-only model.
"""

from __future__ import annotations

import pytest

from engine.training.config import TrainingConfig
from engine.training.paper_execution import (PaperExecutionPolicy, PaperExecutionContext,
                                             SRC_LIVE_CLOB)


def _ctx(**over):
    base = dict(fill_source=SRC_LIVE_CLOB, ask=0.31, bid=0.29, spread=0.02, depth_usd=200.0,
                tick_size=0.01, notional_usd=5.0, gross_edge=0.05, fresh_book=True,
                accepting_orders=True)
    base.update(over)
    return PaperExecutionContext(**base)


def _pol(fraction):
    cfg = TrainingConfig.aggressive_paper()
    cfg.maker_capture_fraction = fraction
    return PaperExecutionPolicy(cfg)


def test_maker_credit_improves_after_cost_edge_on_good_book():
    pol = _pol(0.3)
    ac = pol._after_cost(_ctx())
    assert ac["maker_capture_fraction"] == 0.3
    assert ac["maker_spread_savings"] > 0.0
    assert ac["after_cost_edge"] > ac["taker_after_cost_edge"]


def test_maker_savings_capped_at_fraction_of_spread():
    pol = _pol(0.3)
    ac = pol._after_cost(_ctx(spread=0.02))
    # never credit more than fraction * spread (0.3 * 0.02 = 0.006)
    assert ac["maker_spread_savings"] <= 0.006 + 1e-9


def test_effective_price_floored_at_bid():
    # an enormous capture fraction must never push the entry below the real bid
    pol = _pol(1.0)
    ctx = _ctx(ask=0.60, bid=0.20, spread=0.40)
    ac = pol._after_cost(ctx)
    assert ac["exec_price"] >= 0.20


def test_gated_off_on_thin_or_wide_or_stale_book():
    pol = _pol(0.3)
    assert pol._after_cost(_ctx(depth_usd=1.0))["maker_capture_fraction"] == 0.0   # thin
    assert pol._after_cost(_ctx(spread=0.30))["maker_capture_fraction"] == 0.0      # wide
    assert pol._after_cost(_ctx(fresh_book=False))["maker_capture_fraction"] == 0.0  # stale


def test_disabled_reproduces_taker_only():
    pol = _pol(0.0)
    ac = pol._after_cost(_ctx())
    assert ac["maker_capture_fraction"] == 0.0
    assert ac["maker_spread_savings"] == 0.0
    assert ac["after_cost_edge"] == ac["taker_after_cost_edge"]


def test_no_bid_means_no_maker_credit():
    pol = _pol(0.3)
    ac = pol._after_cost(_ctx(bid=None, spread=None))
    assert ac["maker_capture_fraction"] == 0.0


def test_base_profile_has_maker_off():
    pol = PaperExecutionPolicy(TrainingConfig())
    assert pol.maker_capture_fraction == 0.0
    assert pol.maker_model_enabled is False
