"""Bregman/ABCAS near-miss + smarter-grouping diagnostics (read-only, NON-EXECUTING).

Proves: similar titles normalize into the same candidate family; unrelated markets
do NOT group; incomplete groups stay rejected (completeness never fabricated); valid
complete groups pass into certification; duplicate outcomes + invalid simplex are
rejected WITH a diagnostic reason; thin depth / stale books produce per-leg
near-miss detail without weakening any threshold; top near-misses are persisted +
ranked. No trade is ever executed by these diagnostics.
"""

from engine.training.bregman_text import (normalize_text, event_family_key,
                                          classify_market_kind)
from engine.training.bregman_grouping import (group_markets, validate_simplex,
                                              SimplexGroup, SimplexLeg)
from engine.training.bregman_near_miss import (analyze_rejection, summarize,
                                              rank_near_misses, simplex_diagnostic,
                                              depth_quality, completeness_diagnostic)


def _mk(mid, q, ask, depth, *, group_key=None, age=5.0, bid=None):
    return {"market_id": mid, "group_key": group_key or f"market:{mid}",
            "question": q, "top_depth_usd": depth, "book_age_s": age,
            "clob_token_ids": [mid + "y", mid + "n"],
            "raw": {"bestAsk": ask, "bestBid": ask - 0.02 if bid is None else bid,
                    "question": q}}


# --- normalization + grouping ---------------------------------------------- #
def test_similar_titles_normalize_to_same_family():
    assert normalize_text("Will the Fed cut rates in Sept.?") == \
        normalize_text("Fed to cut rates in September")


def test_similar_markets_group_into_one_candidate_family():
    recs = [_mk("a", "Who wins the 2028 election? Trump", 0.5, 200),
            _mk("b", "Who wins the 2028 election? Newsom", 0.3, 200),
            _mk("c", "Who wins the 2028 election? Other", 0.25, 200)]
    groups = group_markets(recs)
    assert len(groups) == 1
    assert len(groups[0].legs) == 3
    assert groups[0].mutually_exclusive is True
    # completeness is NOT fabricated from a normalized family
    assert groups[0].exhaustive is False


def test_unrelated_markets_do_not_group_together():
    recs = [_mk("a", "Will it rain in London tomorrow?", 0.5, 200),
            _mk("b", "Who wins the Super Bowl? Chiefs", 0.3, 200)]
    groups = group_markets(recs)
    # two distinct families -> not merged into one multi-leg group
    assert all(len(g.legs) <= 2 for g in groups)
    fams = {event_family_key(r) for r in recs}
    assert len(fams) == 2


def test_winner_take_all_classification():
    assert classify_market_kind("Who wins the 2028 election?", n_legs=5) == "winner_take_all"
    assert classify_market_kind("BTC above 100000 on Friday?", n_legs=2) == "range"
    assert classify_market_kind("Will the Fed cut?", n_legs=2) == "binary"


# --- completeness (never fabricated) --------------------------------------- #
def test_incomplete_group_stays_rejected():
    g = SimplexGroup("event:fam", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "ta", ask=0.5, bid=0.49, depth_usd=200),
                      SimplexLeg("b", "YES", "tb", ask=0.4, bid=0.39, depth_usd=200)],
                     exhaustive=False)
    ok, why = validate_simplex(g)
    assert ok is False and why == "not_exhaustive"


def test_valid_complete_group_passes_grouping_into_certification():
    g = SimplexGroup("event:fam", "exhaustive_event",
                     [SimplexLeg("a", "YES", "ta", ask=0.5, bid=0.49, depth_usd=200),
                      SimplexLeg("b", "YES", "tb", ask=0.45, bid=0.44, depth_usd=200)],
                     exhaustive=True, mutually_exclusive=True)
    ok, why = validate_simplex(g)
    assert ok is True and why == "ok"


def test_not_exhaustive_diagnostic_lists_observed_and_kind():
    g = SimplexGroup("event:who-wins", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "ta", ask=0.4, bid=0.39, depth_usd=200),
                      SimplexLeg("b", "YES", "tb", ask=0.3, bid=0.29, depth_usd=200),
                      SimplexLeg("c", "YES", "tc", ask=0.2, bid=0.19, depth_usd=200)],
                     exhaustive=False, meta={"question": "Who wins the election?"})
    d = completeness_diagnostic(g)
    assert d["completeness_proven"] is False
    assert d["observed_count"] == 3
    assert d["reason_incomplete"]
    assert d["market_kind"] in ("winner_take_all", "multi_way")


# --- simplex diagnostics --------------------------------------------------- #
def test_duplicate_outcomes_rejected_with_reason():
    g = SimplexGroup("d", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "tok1", ask=0.5, bid=0.49),
                      SimplexLeg("a", "YES", "tok1", ask=0.5, bid=0.49)],
                     exhaustive=False)
    ok, why = validate_simplex(g)
    assert ok is False and why == "duplicate_legs"
    assert simplex_diagnostic(g)["duplicate_outcomes"] is True


def test_event_group_all_yes_is_not_duplicate():
    # every leg of a multi-market event group is legitimately outcome "YES"
    g = SimplexGroup("e", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "tA", ask=0.5),
                      SimplexLeg("b", "YES", "tB", ask=0.4)],
                     exhaustive=False)
    assert simplex_diagnostic(g)["duplicate_outcomes"] is False


def test_invalid_simplex_diagnostic_breakdown():
    g = SimplexGroup("x", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "tA", ask=2.0),
                      SimplexLeg("b", "YES", "tB", ask=2.0)],
                     exhaustive=True)
    d = simplex_diagnostic(g)
    assert d["sum_of_probabilities"] == 4.0
    assert d["invalid_normalization"] is True
    assert d["true_invalid_economics"] is True


def test_simplex_parsing_issue_distinguished_from_invalid_economics():
    g = SimplexGroup("p", "mutually_exclusive",
                     [SimplexLeg("a", "YES", "tA", ask=0.5),
                      SimplexLeg("b", "YES", "tB", ask=0.0)],   # leg failed to price
                     exhaustive=True)
    d = simplex_diagnostic(g)
    assert d["suspected_parsing_issue"] is True
    assert d["true_invalid_economics"] is False


# --- depth + near-miss ----------------------------------------------------- #
def test_thin_depth_per_leg_detail_without_weakening_threshold():
    g = SimplexGroup("g", "exhaustive_event",
                     [SimplexLeg("a", "YES", "tA", ask=0.4, bid=0.39, depth_usd=10),
                      SimplexLeg("b", "YES", "tB", ask=0.5, bid=0.49, depth_usd=200)],
                     exhaustive=True)
    dq = depth_quality(g, min_depth_usd=50.0)
    assert dq["thin_legs"] == 1
    assert dq["thin_cause"] == "one_leg"
    assert dq["worst_leg_market_id"] == "a"
    assert dq["required_depth_usd"] == 50.0       # threshold reported, NOT lowered
    nm = analyze_rejection(g, "depth_too_thin", min_depth_usd=50.0,
                           max_spread=0.08, max_age_s=20.0)
    assert nm["fix_category"] == "depth"
    assert nm["one_fix_away"] is True
    assert nm["remaining_blockers"] == ["depth"]
    assert nm["executed"] is False and nm["trade_gate_bypassed"] is False


def test_stale_book_records_refresh_attempt_before_rejection():
    g = SimplexGroup("s", "exhaustive_event",
                     [SimplexLeg("a", "YES", "tA", ask=0.4, bid=0.39, depth_usd=200,
                                 stale=True, fresh_book=False, book_age_s=45.0),
                      SimplexLeg("b", "YES", "tB", ask=0.5, bid=0.49, depth_usd=200)],
                     exhaustive=True)
    nm = analyze_rejection(g, "stale_book", min_depth_usd=50.0, max_spread=0.08,
                           max_age_s=20.0, refresh_attempted=True, refresh_ok=False,
                           refresh_reason="refresh_failed")
    fq = nm["freshness"]
    assert fq["stale_legs"] == 1
    assert fq["refresh_attempted"] is True and fq["refresh_ok"] is False
    assert fq["freshness_threshold_s"] == 20.0    # threshold reported, NOT loosened
    assert fq["worst_leg_age_s"] == 45.0


def test_top_near_misses_persisted_and_ranked():
    g_close = SimplexGroup("close", "exhaustive_event",
                           [SimplexLeg("a", "YES", "tA", ask=0.4, bid=0.39, depth_usd=10),
                            SimplexLeg("b", "YES", "tB", ask=0.5, bid=0.49, depth_usd=200)],
                           exhaustive=True)
    g_far = SimplexGroup("far", "mutually_exclusive",
                         [SimplexLeg("c", "YES", "tC", ask=0.0, depth_usd=0)],
                         exhaustive=False)
    nm1 = analyze_rejection(g_close, "depth_too_thin", min_depth_usd=50.0,
                            max_spread=0.08, max_age_s=20.0)
    nm2 = analyze_rejection(g_far, "not_exhaustive", min_depth_usd=50.0,
                            max_spread=0.08, max_age_s=20.0)
    ranked = rank_near_misses([nm2, nm1], top_n=2)
    assert ranked[0]["group_key"] == "close"      # closer one ranks first
    summ = summarize([nm1, nm2], top_n=5)
    assert summ["bregman_near_misses_total"] == 2
    assert summ["near_miss_depth_only_count"] == 1
    assert summ["near_miss_not_exhaustive_count"] == 1
    assert "depth_too_thin" in summ["near_miss_by_rejection_reason"]


def test_near_miss_buckets_and_negative_lower_bound_honesty():
    # both near-misses have implied sum > payout -> negative after-cost lower bound
    g1 = SimplexGroup("g1", "exhaustive_event",
                      [SimplexLeg("a", "YES", "tA", ask=0.6, bid=0.59, depth_usd=5),
                       SimplexLeg("b", "YES", "tB", ask=0.6, bid=0.59, depth_usd=5)],
                      exhaustive=True)
    nm = analyze_rejection(g1, "depth_too_thin", min_depth_usd=25.0,
                           max_spread=0.08, max_age_s=20.0)
    summ = summarize([nm], top_n=5)
    assert "near_miss_buckets" in summ
    assert set(summ["near_miss_buckets"]).issuperset({
        "top_by_depth_quality", "top_by_completeness_confidence",
        "top_by_after_cost_lower_bound", "top_by_one_fix_away",
        "top_by_grok_news_relevance"})
    assert summ["near_miss_all_negative_after_cost_lower_bound"] is True
    assert summ["near_miss_tradeable_count"] == 0      # diagnostics NEVER tradeable


# --- trainer depth-sufficient + stale-refresh census ----------------------- #
def _trainer(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)


def test_trainer_depth_sufficient_metrics_no_threshold_change(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    required = float(t.bregman.min_depth_usd)
    def rec(mid, ask, depth, gk):
        return MarketRecord.from_raw({"id": mid, "question": f"Who wins? {mid}",
            "bestAsk": ask, "bestBid": ask - 0.02, "clobTokenIds": [mid + "y", mid + "n"],
            "active": True, "groupItemTitle": gk, "liquidityNum": depth, "volumeNum": depth})
    # one thin leg ($5) + ample legs -> group is depth-insufficient (threshold unchanged)
    recs = [rec("a", 0.5, 5, "ev"), rec("b", 0.3, 5000, "ev"), rec("c", 0.2, 5000, "ev")]
    t.closed_loop.begin_tick()
    t.scan_bregman(recs, now=1000.0)
    bx = t.bregman_exec_metrics
    assert bx["bregman_required_depth_usd"] == required   # NOT lowered
    assert bx["bregman_depth_insufficient_groups"] >= 1
    assert "bregman_all_groups_thin" in bx
    assert "bregman_depth_sufficient_groups" in bx


def test_binary_near_miss_one_market_two_token_ids_yesno_labels(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    r = MarketRecord.from_raw({"id": "573655", "question": "Will X happen?",
        "bestAsk": "0.5", "bestBid": "0.48", "clobTokenIds": ["tokYES", "tokNO"],
        "active": True, "liquidityNum": 5, "volumeNum": 5})
    t.closed_loop.begin_tick()
    t.scan_bregman([r], now=1000.0)
    top = t.bregman_exec_metrics.get("bregman_top_near_misses", [])
    assert top, "expected a near-miss for the thin binary market"
    nm = top[0]
    assert nm["market_ids"] == ["573655"]              # ONE market, not duplicated
    assert len(set(nm["token_ids"])) == 2              # two DISTINCT token ids
    assert nm["outcome_labels"] == ["YES", "NO"]       # not 'unknown'
    assert nm["completeness"]["observed_outcomes"] == ["YES", "NO"]
    assert nm["near_miss_tradeable"] is False
    assert nm["token_ids_unavailable"] is False
    assert nm["single_market_binary"] is True


def test_candidate_generation_blocker_explicit_when_zero(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    r = MarketRecord.from_raw({"id": "m1", "question": "Will X?", "bestAsk": "0.5",
        "bestBid": "0.48", "clobTokenIds": ["a", "b"], "active": True,
        "liquidityNum": 5, "volumeNum": 5})       # ~$ thin depth
    t.closed_loop.begin_tick()
    t.scan_bregman([r], now=1000.0)
    bx = t.bregman_exec_metrics
    assert bx["certified_opportunities"] == 0
    assert bx["bregman_groups_entered_certifier"] >= 1
    assert bx["bregman_candidate_generation_blocker"] is not None
    assert bx["bregman_candidate_generation_blocker_counts"]
    assert bx["bregman_candidate_generation_blocker_samples"]
    # sample carries the clean canonical identity (no duplicated market id)
    s = bx["bregman_candidate_generation_blocker_samples"][0]
    assert s["market_ids"] == ["m1"]
    assert len(set(s["token_ids"])) == 2


def test_price_parse_census_populated(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    r = MarketRecord.from_raw({"id": "m1", "question": "Q", "bestAsk": "0.5",
        "bestBid": "0.48", "clobTokenIds": ["a", "b"], "active": True,
        "liquidityNum": 100, "volumeNum": 100})
    t.closed_loop.begin_tick()
    t.scan_bregman([r], now=1000.0)
    bx = t.bregman_exec_metrics
    assert bx["bregman_price_parse_attempts"] >= 2
    assert bx["bregman_price_parse_success_rate"] == 1.0
    assert bx["bregman_non_numeric_price_count"] == 0


def test_depth_quality_extras_reported(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    recs = [MarketRecord.from_raw({"id": m, "question": "Q", "bestAsk": "0.5",
        "bestBid": "0.48", "clobTokenIds": [m + "a", m + "b"], "active": True,
        "liquidityNum": 5, "volumeNum": 5}) for m in ("m1", "m2")]
    t.closed_loop.begin_tick()
    t.scan_bregman(recs, now=1000.0)
    bx = t.bregman_exec_metrics
    for k in ("bregman_worst_leg_depth_usd", "bregman_best_depth_quality_score",
              "bregman_all_groups_depth_insufficient", "bregman_required_depth_usd"):
        assert k in bx
    assert bx["bregman_all_groups_depth_insufficient"] is True


def test_trainer_stale_refresh_metrics_reported(tmp_path, monkeypatch):
    from engine.markets.universe_manager import MarketRecord
    t = _trainer(tmp_path, monkeypatch)
    def rec(mid, ask, gk):
        return MarketRecord.from_raw({"id": mid, "question": f"Q {mid}",
            "bestAsk": ask, "bestBid": ask - 0.02, "clobTokenIds": [mid + "y", mid + "n"],
            "active": True, "groupItemTitle": gk, "liquidityNum": 5000, "volumeNum": 5000})
    recs = [rec("a", 0.5, "ev"), rec("b", 0.45, "ev")]
    t.closed_loop.begin_tick()
    t.scan_bregman(recs, now=1000.0)
    bx = t.bregman_exec_metrics
    for k in ("bregman_promising_groups_refreshed", "bregman_refresh_success",
              "bregman_refresh_failed", "bregman_stale_after_refresh"):
        assert k in bx
