"""Grok-driven Bregman candidate generation (#3) — research-only.

Grok PROPOSES mispriced/incoherent groups; the deterministic certifier still PROVES
them. Candidates are never tradeable; they only rank what to scan/research next.
"""

from __future__ import annotations

from engine.research.bregman_candidate_finder import (
    group_incoherence, grok_disagreement, score_group, rank_candidates, summarize)
from engine.research.advisory_targets import select_advisory_target


def test_incoherent_mece_set_flagged():
    # 3 outcomes whose asks sum to 0.90 < $1 -> buy-all candidate (incoherence 0.10)
    legs = [{"ask": 0.30}, {"ask": 0.30}, {"ask": 0.30}]
    assert abs(group_incoherence(legs) - 0.10) < 1e-9
    # coherent set (sum == 1) -> no structural candidate
    assert group_incoherence([{"ask": 0.5}, {"ask": 0.5}]) == 0.0


def test_grok_disagreement_measures_mispricing():
    legs = [{"ask": 0.40, "grok_prob": 0.70}, {"ask": 0.60, "grok_prob": 0.30}]
    # |0.70-0.40|=0.30 ; |0.30-0.60|=0.30 -> mean 0.30
    assert abs(grok_disagreement(legs) - 0.30) < 1e-9
    assert grok_disagreement([{"ask": 0.4}, {"ask": 0.6}]) == 0.0   # no grok probs


def test_score_group_is_never_tradeable():
    g = {"group_id": "g1", "complete": True,
         "legs": [{"ask": 0.30, "ask_depth": 500}, {"ask": 0.30, "ask_depth": 500},
                  {"ask": 0.30, "ask_depth": 500}]}
    s = score_group(g)
    assert s["tradeable"] is False and s["advisory_only"] is True
    assert s["incoherence"] > 0 and s["candidate_score"] > 0


def test_rank_drops_zero_signal_and_orders_desc():
    groups = [
        {"group_id": "flat", "legs": [{"ask": 0.5}, {"ask": 0.5}]},          # no signal
        {"group_id": "arb", "complete": True,                                # incoherence 0.4
         "legs": [{"ask": 0.2, "ask_depth": 500}, {"ask": 0.2, "ask_depth": 500},
                  {"ask": 0.2, "ask_depth": 500}]},
        {"group_id": "disagree", "complete": True,
         "legs": [{"ask": 0.5, "grok_prob": 0.8, "ask_depth": 500},
                  {"ask": 0.5, "grok_prob": 0.2, "ask_depth": 500}]},
    ]
    ranked = rank_candidates(groups, top_n=10)
    ids = [r["group_id"] for r in ranked]
    assert "flat" not in ids                       # zero-signal dropped
    assert ids and ids[0] == "arb"                 # strongest (incoherence) first
    scores = [r["candidate_score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_summary_cross_references_certified():
    groups = [{"group_id": "g1", "complete": True,
               "legs": [{"ask": 0.3, "ask_depth": 500}, {"ask": 0.3, "ask_depth": 500},
                        {"ask": 0.3, "ask_depth": 500}]}]
    summ = summarize(groups, certified_ids={"g1"}, top_n=10)
    assert summ["grok_bregman_candidates_proposed"] == 1
    assert summ["grok_bregman_candidates_certified"] == 1
    assert summ["certification_unchanged"] is True


def test_advisory_target_prefers_strong_grok_candidate():
    near = [{"group_key": "nm1", "near_miss_score": 0.9, "market_ids": ["m1"],
             "completeness": {"completeness_proven": True}}]
    cands = [{"group_id": "g_arb", "candidate_score": 0.2, "incoherence": 0.2,
              "grok_disagreement": 0.0, "complete": True, "market_ids": ["g_arb"]}]
    sel = select_advisory_target(near_misses=near, grok_candidates=cands)
    assert sel["target_kind"] == "grok_bregman_candidate"   # beats the near-miss
    assert sel["reason"] == "grok_flagged_bregman_candidate"


def test_advisory_target_ignores_weak_grok_candidate():
    near = [{"group_key": "nm1", "near_miss_score": 0.9, "market_ids": ["m1"],
             "completeness": {"completeness_proven": True}}]
    weak = [{"group_id": "g", "candidate_score": 0.001, "complete": True}]
    sel = select_advisory_target(near_misses=near, grok_candidates=weak,
                                 min_candidate_score=0.02)
    assert sel["target_kind"] == "bregman_near_miss"        # weak candidate ignored
