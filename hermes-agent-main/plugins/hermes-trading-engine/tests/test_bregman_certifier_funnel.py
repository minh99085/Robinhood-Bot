"""End-to-end ABCAS funnel: discovery -> projection -> certification diagnostics.

Proves the combinatorial funnel reaches the certifier and produces usable output /
clear diagnostics (the bot_inspection_20260608_130909 failure):
* an INCOHERENT multi-outcome group (sum of asks < $1, ample depth) is CERTIFIED with
  positive projected profit;
* a COHERENT group (sum == $1) is rejected with the exact reason + a logged
  projected profit + Bregman distance D(mu*||theta) (never silently dropped);
* per-stage rejection taxonomy + near-miss samples are emitted;
* the binary single-book "arb" correctly nets ~spread (no free lunch) — gates intact.
"""

import json

from engine.strategies.bregman_scanner import BregmanPaperScanner


def _explicit(mid, asks, relation, depth=500.0):
    return {"id": mid, "active": True, "enableOrderBook": True, "relation": relation,
            "outcomes": [{"id": f"{mid}:{i}", "label": f"o{i}", "price": a, "ask": a,
                          "bid": a - 0.01, "ask_depth": depth}
                         for i, a in enumerate(asks)]}


def test_incoherent_multiway_group_is_certified_through_full_funnel():
    # sum of asks 0.30*3 = 0.90 < $1 payout, ample depth -> a real certified arb
    tel = BregmanPaperScanner(min_depth_usd=1.0).scan(
        [_explicit("arb", [0.30, 0.30, 0.30], "mece")])
    assert tel["constraint_groups_scanned"] == 1
    assert tel["candidate_arbitrages"] >= 1
    assert tel["certified_arbitrages"] >= 1                  # acceptance #1
    assert tel["best_projected_profit_per_set"] > 0
    assert tel["expected_min_profit"] > 0


def test_coherent_group_rejected_with_logged_profit_and_distance():
    # sum of asks 0.34+0.33+0.33 = 1.0 -> no arb; must log WHY (not silent)
    tel = BregmanPaperScanner(min_depth_usd=1.0).scan(
        [_explicit("fair", [0.34, 0.33, 0.33], "mece")])
    assert tel["certified_arbitrages"] == 0
    # projected profit + Bregman distance ARE logged even when not certified
    assert "best_projected_profit_per_set" in tel
    assert "max_bregman_distance" in tel
    assert tel["near_miss_count"] >= 1
    nm = tel["near_miss_certified_samples"][0]
    assert nm["reject_reason"] == "no_positive_worst_case_profit"
    assert "projected_after_fee_profit_per_set" in nm
    assert "bregman_distance" in nm
    assert nm["tradeable"] is False


def test_stage_rejection_taxonomy_present():
    tel = BregmanPaperScanner(min_depth_usd=1.0).scan(
        [_explicit("fair", [0.34, 0.33, 0.33], "mece"),
         {"id": "bad", "active": True, "enableOrderBook": True,
          "outcomes": json.dumps(["Yes", "No"]),
          "outcomePrices": json.dumps(["N/A", "null"]),
          "clobTokenIds": json.dumps(["a", "b"])}])
    stages = tel["stage_rejections"]
    assert "adapter_failed" in stages              # discovery skip
    assert "certifier_no_positive_profit" in stages
    assert "realism_fees_spread_depth" in stages
    assert stages["adapter_failed"] >= 1           # the malformed price market


def test_near_miss_samples_ranked_closest_to_positive():
    tel = BregmanPaperScanner(min_depth_usd=1.0).scan([
        _explicit("close", [0.33, 0.33, 0.335], "mece"),   # ~1.0 (closest)
        _explicit("far", [0.45, 0.45, 0.45], "mece")])      # 1.35 (far)
    samples = tel["near_miss_certified_samples"]
    assert len(samples) >= 2
    # ranked by projected profit descending (closest-to-positive first)
    profits = [s["projected_after_fee_profit_per_set"] for s in samples]
    assert profits == sorted(profits, reverse=True)


def test_model_probability_not_defaulting_to_002_floor():
    from engine.training.closed_loop import _resolve_model_probability
    from types import SimpleNamespace as NS
    # the degenerate 0.02 ensemble floor (no model signal) -> use market-implied mid
    assert _resolve_model_probability(
        NS(p_final=0.02), NS(p_market_mid=0.41, calibrated_probability=None)) == 0.41
    # a genuine model signal is preserved
    assert _resolve_model_probability(
        NS(p_final=0.6), NS(p_market_mid=0.41, calibrated_probability=None)) == 0.6
    # a calibrated probability wins over both
    assert _resolve_model_probability(
        NS(p_final=0.02), NS(p_market_mid=0.41, calibrated_probability=0.55)) == 0.55
    # nothing available -> None (never fabricate)
    assert _resolve_model_probability(
        NS(p_final=0.0), NS(p_market_mid=0.0, calibrated_probability=None)) is None


def test_binary_single_book_complement_nets_spread_no_free_lunch():
    # one binary market: buy YES + buy NO(=1-YESbid) costs >= $1 + spread -> never arb
    def gamma(mid, py):
        return {"id": mid, "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps([str(py), str(round(1 - py, 2))]),
                "clobTokenIds": json.dumps([mid + "_Y", mid + "_N"]),
                "bestBid": str(py - 0.01), "bestAsk": str(py), "topDepthUsd": "500",
                "active": True, "enableOrderBook": True}
    tel = BregmanPaperScanner(min_depth_usd=1.0).scan([gamma("b", 0.40)])
    assert tel["certified_arbitrages"] == 0       # gates intact; spread is real
    assert tel["best_projected_profit_per_set"] <= 0
