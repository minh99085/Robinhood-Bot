"""6B: Bregman neg-risk family completeness enrichment.

Polymarket neg-risk siblings (sharing a negRiskMarketID) form ONE mutually-exclusive,
collectively-exhaustive event. Grouping by that family id assembles a complete set that
the per-market event key would fragment — but exhaustiveness is STILL proven only by the
declared outcome count matching the scanned legs (completeness is never fabricated).
"""

from __future__ import annotations

import time

import pytest

from engine.markets import universe_manager as um
from engine.training.bregman_grouping import (group_markets, family_completeness_report,
                                              _negrisk_family_id, _is_neg_risk)
from tests._pmtrain_helpers import clean_live_env, market


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    clean_live_env(monkeypatch, tmp_path)


def _mk(i, *, negrisk_id=None, outcome_count=None, ask=0.30, **over):
    now = time.time()
    raw = market(i, bid=round(ask - 0.02, 4), ask=ask, now=now)
    raw["clobTokenIds"] = [f"tok{i}a", f"tok{i}b"]
    if negrisk_id is not None:
        raw["negRiskMarketID"] = negrisk_id
        raw["negRisk"] = True
    if outcome_count is not None:
        raw["outcomeCount"] = outcome_count
    raw.update(over)
    return um.MarketRecord.from_raw(raw, now=now)


def test_negrisk_siblings_group_into_one_family():
    # three siblings of the SAME neg-risk family, NO shared event group_key
    recs = [_mk(0, negrisk_id="EVT1"), _mk(1, negrisk_id="EVT1"), _mk(2, negrisk_id="EVT1")]
    groups = group_markets(recs, include_binary=False)
    # all three assembled into ONE group (not three fragmented binaries)
    fam = [g for g in groups if len(g.legs) == 3]
    assert len(fam) == 1
    assert fam[0].mutually_exclusive is True


def test_negrisk_family_exhaustive_when_count_matches_legs():
    # 3 siblings + declared outcomeCount 3 -> complete/exhaustive (certifiable)
    recs = [_mk(i, negrisk_id="EVT2", outcome_count=3) for i in range(3)]
    groups = group_markets(recs, include_binary=False)
    g = max(groups, key=lambda x: len(x.legs))
    assert len(g.legs) == 3 and g.exhaustive is True


def test_negrisk_family_incomplete_when_count_exceeds_legs():
    # declared 4 outcomes but only 2 scanned -> NOT exhaustive (never fabricated)
    recs = [_mk(i, negrisk_id="EVT3", outcome_count=4) for i in range(2)]
    groups = group_markets(recs, include_binary=False)
    g = max(groups, key=lambda x: len(x.legs))
    assert len(g.legs) == 2 and g.exhaustive is False


def test_family_report_surfaces_negrisk_fields():
    recs = [_mk(0, negrisk_id="EVT4"), _mk(1, negrisk_id="EVT4")]
    rep = family_completeness_report(recs)
    assert rep["is_neg_risk_family"] is True
    assert rep["negrisk_family_ids"] == ["negrisk:EVT4"]


def test_helpers_detect_negrisk():
    assert _negrisk_family_id(_mk(0, negrisk_id="X")) == "negrisk:X"
    assert _is_neg_risk(_mk(0, negrisk_id="X")) is True
    assert _negrisk_family_id(_mk(0)) is None        # non-neg-risk market
