"""Market selection by after-cost edge (not trade count).

Quant scope — *Signal Generation & Strategy Development* + *Data Preprocessing &
Feature Engineering*: proves the after-cost profitability score ranks markets by
net edge and that ``annotate_profitability`` can re-rank a shortlist so a modest
edge on a cheap-to-trade book beats a fat gross edge on an expensive one. PAPER
ONLY.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.training.candidate_ranker import annotate_profitability
from engine.training.profitability_governor import after_cost_profitability_score


def _costs(**kw):
    base = dict(fee=0.001, spread=0.004, slippage=0.0025, ambiguity=0.0,
                stale=0.0, evidence=0.0, calibration=0.0, liquidity=0.0)
    base.update(kw)
    return base


def test_after_cost_score_ranks_by_net_not_gross():
    # market A: big gross but expensive; market B: modest gross but cheap
    a = after_cost_profitability_score(gross_edge=0.05,
                                       cost_components=_costs(spread=0.03, slippage=0.02))
    b = after_cost_profitability_score(gross_edge=0.02, cost_components=_costs())
    assert b > a  # cheaper net edge wins despite smaller gross


def _rec(mid, *, spread, liq):
    return SimpleNamespace(market_id=mid, spread=spread, liquidity_usd=liq,
                           top_depth_usd=liq, category="crypto",
                           book_age_s=0.0, has_resolution_text=True, end_ts=None,
                           yes_price=0.5, raw={})


def test_annotate_profitability_reranks_by_after_cost():
    cfg = SimpleNamespace(max_allowed_spread=0.08, max_spread=0.08,
                          taker_fee_bps=0.0, slippage_bps=25.0)
    # both have similar quality base score, but one has a much wider spread
    scored = [
        {"record": _rec("wide", spread=0.07, liq=50000.0), "score": 80.0, "components": {}},
        {"record": _rec("tight", spread=0.005, liq=50000.0), "score": 78.0, "components": {}},
    ]
    out = annotate_profitability(scored, cfg, profitability_first=True)
    assert all("after_cost_score" in d and "timing" in d for d in out)
    # the tight-spread market now ranks first on after-cost profitability
    assert out[0]["record"].market_id == "tight"


def test_annotate_profitability_additive_when_not_profitability_first():
    cfg = SimpleNamespace(max_allowed_spread=0.08, max_spread=0.08,
                          taker_fee_bps=0.0, slippage_bps=25.0)
    scored = [
        {"record": _rec("a", spread=0.01, liq=50000.0), "score": 90.0, "components": {}},
        {"record": _rec("b", spread=0.02, liq=50000.0), "score": 70.0, "components": {}},
    ]
    out = annotate_profitability(scored, cfg, profitability_first=False)
    # ordering unchanged (still by quality score), annotations present
    assert [d["record"].market_id for d in out] == ["a", "b"]
    assert all("after_cost_score" in d for d in out)
