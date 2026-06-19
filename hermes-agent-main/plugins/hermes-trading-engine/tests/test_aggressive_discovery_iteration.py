"""Aggressive profit-discovery iteration (#1-#4) — PAPER ONLY, hard gates intact.

Covers:
  #1 not_exhaustive decomposition + per-candidate conversion attribution (pure).
  #2 targeted family completion priority (high-lower-bound incomplete families fetched first).
  #3 tightened standard-exploration execution-quality floor (config).
  #4 segregated relaxed-discovery exploration lane: loosen ONLY the SOFT spread/ambiguity
     tolerances for the exploration lane (already excluded from readiness); stale-book,
     depth, missing-ask and every fake-fill ban stay STRICT; readiness gates untouched.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.not_exhaustive_analysis import (
    analyze_not_exhaustive, SUB_MISSING_SIBLING, SUB_NO_DECLARED, SUB_TRULY_INCOMPLETE)
from engine.training.family_completion import expand_event_families
from engine.markets import universe_manager as um
from tests._pmtrain_helpers import clean_live_env

_NOW = 1_790_000_000.0


# --------------------------------------------------------------------------- #
# #1 not_exhaustive decomposition
# --------------------------------------------------------------------------- #
def _nm(group_key, *, declared, observed, alb, one_fix=True, blockers=("exhaustive",)):
    return {"reject_reason": "not_exhaustive", "one_fix_away": one_fix,
            "remaining_blockers": list(blockers), "after_cost_lower_bound": alb,
            "group_key": group_key, "market_ids": ["a", "b"], "near_miss_score": 0.7,
            "completeness": {"observed_count": observed, "declared_expected_count": declared,
                             "completeness_proven": False}}


def test_not_exhaustive_subtypes_and_fixable_ranking():
    near = [
        _nm("event:1", declared=3, observed=2, alb=0.04),         # fixable (best)
        _nm("event:2", declared=4, observed=2, alb=0.02),         # fixable
        _nm("event:3", declared=None, observed=2, alb=0.03),      # no declared count
        _nm("event:4", declared=3, observed=3, alb=0.01),         # truly incomplete
        _nm("event:5", declared=3, observed=2, alb=-0.05),        # negative lb -> not fixable
        _nm("event:6", declared=3, observed=2, alb=0.06, one_fix=False,
            blockers=("exhaustive", "depth")),                    # 2 blockers -> not fixable
    ]
    out = analyze_not_exhaustive(near)
    assert out["not_exhaustive_total"] == 6
    assert out["subtype_counts"][SUB_MISSING_SIBLING] == 4
    assert out["subtype_counts"][SUB_NO_DECLARED] == 1
    assert out["subtype_counts"][SUB_TRULY_INCOMPLETE] == 1
    assert out["fixable_positive_lb_count"] == 2
    keys = [c["group_key"] for c in out["top_fixable_candidates"]]
    assert keys == ["event:1", "event:2"]                          # ranked by lower bound desc
    assert out["best_fixable_lower_bound"] == 0.04
    assert out["top_fixable_candidates"][0]["missing_outcome_count"] == 1


def test_not_exhaustive_ignores_certified_records():
    near = [{"reject_reason": "thin_depth", "completeness": {"completeness_proven": True}}]
    assert analyze_not_exhaustive(near)["not_exhaustive_total"] == 0


# --------------------------------------------------------------------------- #
# #2 targeted family completion priority
# --------------------------------------------------------------------------- #
def _no_embed(mid, *, event_id):
    raw = {"id": mid, "clobTokenIds": [f"{mid}A", f"{mid}B"], "question": mid,
           "groupItemTitle": mid, "outcomePrices": ["0.30", "0.70"],
           "events": [{"id": event_id, "slug": event_id}],
           "bestAsk": 0.30, "bestBid": 0.28, "liquidityNum": 500.0}
    return um.MarketRecord.from_raw(raw, now=_NOW)


def test_priority_family_fetched_first_under_cap():
    a = _no_embed("ma", event_id="EA")
    b = _no_embed("mb", event_id="EB")
    seen = []

    def fetch(eid):
        seen.append(eid)
        return {"id": eid, "markets": [{"id": f"{eid}-{i}", "clobTokenIds": [f"t{i}a", f"t{i}b"],
                                        "question": str(i), "groupItemTitle": str(i),
                                        "outcomePrices": ["0.3", "0.7"]} for i in range(3)]}

    # cap = 1 fetch/tick; without priority, ordering is liquidity-based (tie) -> nondeterministic.
    # priority_keys pins EB's family first, so EB MUST be the one fetched.
    out, tel = expand_event_families(
        [a, b], now=_NOW, event_fetcher=fetch, max_events_fetched=1,
        priority_keys={b.group_key})
    assert seen == ["EB"]
    assert tel["family_completion_targeted_prioritized"] == 1
    assert tel["family_completion_events_fetched"] == 1


# --------------------------------------------------------------------------- #
# #3 tightened standard-exploration execution-quality floor
# --------------------------------------------------------------------------- #
def test_aggressive_profile_tightens_exec_quality_floor():
    cfg = TrainingConfig.aggressive_paper()
    assert cfg.exploration_min_execution_quality >= 0.25


# --------------------------------------------------------------------------- #
# #4 segregated relaxed-discovery exploration lane
# --------------------------------------------------------------------------- #
def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    base = dict(mode="paper_train", max_open_trades=8,
                exploration_max_spread=0.08, exploration_max_ambiguity_score=0.45,
                exploration_max_book_age_sec=20.0, exploration_max_expected_loss_usd=5.0)
    base.update(cfg)
    return PolymarketPaperTrainer(TrainingConfig(**base), data_dir=tmp_path)


def _candidate(*, spread, amb=0.05, depth=8.0, age=2.0, fresh=True, ask=0.50):
    rec = SimpleNamespace(top_depth_usd=depth, book_age_s=age, market_id="mx",
                          group_key="event:x")
    est = SimpleNamespace(spread=spread, ambiguity_score=amb, fresh_book=fresh)
    edge = SimpleNamespace(executable_price=ask, net_edge=0.0)
    return rec, est, edge


def test_relaxed_lane_admits_wide_spread_strict_rejects(tmp_path, monkeypatch):
    # spread 0.095: above strict 0.08, below relaxed 0.08*1.3=0.104
    rec, est, edge = _candidate(spread=0.095)

    strict = _trainer(tmp_path, monkeypatch, relaxed_discovery_enabled=False)
    ok_s, nm_s = strict._exploration_eligibility(rec, est, edge)
    assert ok_s is False and nm_s["failed_gate"] == "wide_spread"

    relaxed = _trainer(tmp_path, monkeypatch, relaxed_discovery_enabled=True,
                       relaxed_discovery_loosen_pct=0.30)
    ok_r, info_r = relaxed._exploration_eligibility(rec, est, edge)
    assert ok_r is True
    assert info_r["relaxed_discovery_admitted"] is True
    assert relaxed._relaxed_discovery_admitted == 1


def test_relaxed_lane_keeps_stale_book_strict(tmp_path, monkeypatch):
    # even with relaxed enabled, a stale book is REJECTED (no fake fills, ever).
    rec, est, edge = _candidate(spread=0.02, age=999.0)
    relaxed = _trainer(tmp_path, monkeypatch, relaxed_discovery_enabled=True)
    ok, nm = relaxed._exploration_eligibility(rec, est, edge)
    assert ok is False and nm["failed_gate"] == "stale_book"


def test_relaxed_lane_keeps_missing_ask_strict(tmp_path, monkeypatch):
    rec, est, edge = _candidate(spread=0.02, fresh=False)
    relaxed = _trainer(tmp_path, monkeypatch, relaxed_discovery_enabled=True)
    ok, nm = relaxed._exploration_eligibility(rec, est, edge)
    assert ok is False and nm["failed_gate"] == "missing_ask_or_stale_book"


def test_relaxed_lane_not_flagged_for_within_strict_candidate(tmp_path, monkeypatch):
    # a candidate already inside the STRICT tolerances is not labelled relaxed-admitted.
    rec, est, edge = _candidate(spread=0.02)
    relaxed = _trainer(tmp_path, monkeypatch, relaxed_discovery_enabled=True)
    ok, info = relaxed._exploration_eligibility(rec, est, edge)
    assert ok is True and info.get("relaxed_discovery_admitted") is False
