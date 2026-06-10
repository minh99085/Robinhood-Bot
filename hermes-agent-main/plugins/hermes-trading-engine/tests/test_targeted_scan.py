"""Targeted market-scan PRIORITIZATION + market-quality scorer (PAPER ONLY).

Proves quality scoring prioritizes effort WITHOUT loosening any gate or enabling
trades: tiered (not hard pass/fail) scoring, hard structural checks, side-specific
(not summed) depth, category prioritization with metadata-proven completeness, scan
waste cooldown, and report telemetry. Targeted scan never executes/sizes/gates and
never disables broad scan.
"""

import json
import time

from engine.training.market_quality import (score_market, structural_checks,
                                            side_specific_depth, parse_book_timestamp,
                                            QualityThresholds, TIER_REJECT)
from engine.training.targeted_scan import TargetedMarketScanner, classify_categories


_NOW = 1_700_000_000.0


def _mkt(mid="m", q="Will the team win?", ask=0.51, depth=400, liq=40000, vol=20000,
         age=3, end="2099-06-12", labels=("Yes", "No"), tokens=None, negrisk=False,
         bid=None):
    tokens = tokens if tokens is not None else [mid + "Y", mid + "N"]
    raw = {"id": mid, "question": q, "outcomes": json.dumps(list(labels)),
           "outcomePrices": json.dumps([str(round(ask - 0.01, 2)), str(round(1 - ask, 2))]),
           "clobTokenIds": json.dumps(tokens),
           "bestBid": str(bid if bid is not None else round(ask - 0.01, 2)),
           "bestAsk": str(ask), "topDepthUsd": str(depth), "liquidityNum": str(liq),
           "volume24hr": str(vol), "bookUpdatedTs": str(_NOW - age), "endDate": end}
    if negrisk:
        raw["negRiskComplete"] = True
    return raw


# --- scorer: structural + tiered ------------------------------------------- #
def test_scorer_valid_binary_payload():
    r = score_market(_mkt(), now=_NOW)
    assert r["structural_ok"] is True
    assert r["market_quality_tier"] in ("gold", "silver", "bronze")
    assert r["trade_eligible"] is False        # quality NEVER implies executability


def test_scorer_missing_token_ids():
    sc = structural_checks(_mkt(tokens=[]))
    assert "token_ids_unavailable" in sc["failures"]


def test_scorer_missing_labels_flagged():
    sc = structural_checks(_mkt(labels=("", "")))
    assert any("label" in f for f in sc["failures"]) or sc["failures"]


def test_scorer_invalid_bid_ask_is_critical_reject():
    sc = structural_checks(_mkt(ask=0.5, bid=0.6))   # bid > ask
    assert sc["critical"] is True
    r = score_market(_mkt(ask=0.5, bid=0.6), now=_NOW)
    assert r["market_quality_tier"] == TIER_REJECT


def test_scorer_timestamp_ms_and_seconds():
    sec = _mkt()
    ms = _mkt(); ms["bookUpdatedTs"] = str(int(_NOW * 1000))
    assert abs(parse_book_timestamp(sec) - (_NOW - 3)) < 1.0
    assert abs(parse_book_timestamp(ms) - _NOW) < 1.0


def test_scorer_timestamp_iso_and_missing():
    iso = _mkt(); del iso["bookUpdatedTs"]; iso["updatedAt"] = "2026-06-10T04:00:00+00:00"
    assert parse_book_timestamp(iso) is not None        # ISO parses
    missing = _mkt(); del missing["bookUpdatedTs"]
    assert parse_book_timestamp(missing) is None        # missing -> None (unknown)
    sc = structural_checks(missing)
    assert sc["book_timestamp_status"] == "missing"
    assert "book_timestamp_unparseable" not in sc["failures"]  # missing is NOT a failure
    assert sc["critical"] is False


def test_missing_timestamp_is_not_counted_stale():
    # the production catalog often lacks book timestamps -> must NOT all be "stale"
    s = TargetedMarketScanner(enabled=True)
    recs = [_mkt(mid=f"m{i}") for i in range(5)]
    for r in recs:
        del r["bookUpdatedTs"]                           # no timestamp at all
    tel = s.scan(recs, now=_NOW)
    assert tel["stale_book_scan_waste_count"] == 0       # was the 873/873 bug
    assert tel["targeted_scan_missing_data_counts"]["missing_book_timestamp"] == 5


def test_missing_depth_is_not_counted_thin():
    # liquidity present but NO top-of-book depth field -> depth UNKNOWN, not thin
    s = TargetedMarketScanner(enabled=True)
    recs = []
    for i in range(5):
        r = _mkt(mid=f"m{i}"); del r["topDepthUsd"]; r["liquidityNum"] = "8000"
        recs.append(r)
    tel = s.scan(recs, now=_NOW)
    assert tel["thin_depth_scan_waste_count"] == 0       # was the 869/873 bug
    assert tel["targeted_scan_missing_data_counts"]["missing_depth"] == 5
    # and categories still populate (not disqualified by missing data)
    assert tel["complete_yes_no_tight_spread_markets_scanned"] >= 1


def test_known_thin_and_known_stale_still_counted():
    # KNOWN data that is genuinely thin/stale MUST still be flagged (gates unchanged)
    s = TargetedMarketScanner(enabled=True)
    thin = _mkt(mid="thin", depth=2)                     # real topDepthUsd=2 -> known thin
    stale = _mkt(mid="stale", age=99999); stale["topDepthUsd"] = "400"  # known old book
    tel = s.scan([thin, stale], now=_NOW)
    assert tel["thin_depth_scan_waste_count"] >= 1
    assert tel["stale_book_scan_waste_count"] >= 1


def test_scoring_is_tiered_not_hard_all_pass():
    # thin + zero volume must NOT be reject solely; it is down-prioritized (watch/bronze)
    r = score_market(_mkt(depth=5, liq=100, vol=0), now=_NOW)
    assert r["market_quality_tier"] != TIER_REJECT
    assert r["market_quality_score"] > 0


def test_side_specific_depth_not_summed():
    legs = [{"visible_ask_depth_usd": 10, "visible_bid_depth_usd": 999},
            {"visible_ask_depth_usd": 200, "visible_bid_depth_usd": 999}]
    d = side_specific_depth(legs, side="buy")
    assert d["worst_leg_depth_usd"] == 10        # ask-side worst leg, NOT bid+ask
    assert d["min_leg_depth_usd"] == 10
    # bid-side ignored for buy (no 999+999 summation)
    assert d["executable_notional_usd"] == 210


# --- categories ------------------------------------------------------------ #
def test_high_liquidity_binary_prioritized():
    r = score_market(_mkt(ask=0.51, depth=400), now=_NOW)
    cats = classify_categories(_mkt(ask=0.51, depth=400), r)
    assert "high_liquidity_binary" in cats


def test_negative_risk_requires_metadata_proof():
    # title-similar multi-candidate WITHOUT metadata -> NOT negative_risk_complete
    plain = _mkt(mid="x", q="Who wins the election? A")
    rp = score_market(plain, now=_NOW)
    assert "negative_risk_complete" not in classify_categories(plain, rp)
    # WITH negRiskComplete metadata -> qualifies
    nr = _mkt(mid="y", negrisk=True)
    rn = score_market(nr, now=_NOW)
    assert "negative_risk_complete" in classify_categories(nr, rn)


def test_short_resolution_scored_higher_for_learning():
    soon = score_market(_mkt(mid="s", end="2023-11-16"), now=_NOW)  # ~1 day out
    far = score_market(_mkt(mid="f", end="2099-01-01"), now=_NOW)
    assert soon["resolution_horizon_score"] >= far["resolution_horizon_score"]


def test_btc_macro_reference_needs_external_context():
    btc = score_market(_mkt(mid="b", q="Will BTC top $100k?"), now=_NOW)
    plain = score_market(_mkt(mid="p", q="Will the team win the cup?"), now=_NOW)
    assert btc["external_reference_score"] > 0
    assert plain["external_reference_score"] == 0
    assert "btc_eth_chainlink" in classify_categories(_mkt(mid="b", q="Will BTC top $100k?"), btc)


# --- scanner: prioritization, no execution, cooldown ----------------------- #
def test_targeted_scan_does_not_disable_broad_and_cannot_execute():
    s = TargetedMarketScanner(enabled=True)
    tel = s.scan([_mkt(mid="a"), _mkt(mid="b", depth=5, liq=100, vol=0)], now=_NOW)
    assert tel["targeted_market_scan_enabled"] is True
    assert tel["targeted_markets_scanned_total"] == 2
    # broad exploration budget is always reserved (targeting never disables broad scan)
    assert tel["targeted_scan_budget_by_category"].get("broad_exploration", 0) > 0
    assert tel["targeted_scan_can_execute"] is False
    assert tel["targeted_scan_can_size"] is False
    assert tel["market_quality_tier_counts"]


def test_scan_waste_cooldown_after_repeated_thin_stale():
    s = TargetedMarketScanner(enabled=True, cooldown_ticks=5)
    thin = _mkt(mid="bad", depth=2, liq=50, vol=0, age=300)   # thin + stale
    dep_ever = 0
    for _ in range(4):
        tel = s.scan([thin], now=_NOW)
        dep_ever = max(dep_ever, tel["scan_deprioritized_groups"])
    # repeated waste -> deprioritized (on the trigger tick) with an active cooldown
    assert dep_ever >= 1
    assert tel["scan_cooldown_active_groups"] >= 1
    assert tel["thin_depth_scan_waste_count"] >= 1
    assert tel["stale_book_scan_waste_count"] >= 1


def test_disabled_scanner_is_noop():
    s = TargetedMarketScanner(enabled=False)
    tel = s.scan([_mkt()], now=_NOW)
    assert tel["targeted_market_scan_enabled"] is False


def test_empty_categories_have_sampled_noop_reasons():
    # plain binary markets -> negrisk/short-res/news categories are empty WITH reasons
    s = TargetedMarketScanner(enabled=True)
    recs = [_mkt(mid=f"m{i}", q="Will the team win?") for i in range(3)]
    for r in recs:
        del r["endDate"]                                  # no resolution date
    tel = s.scan(recs, now=_NOW)
    noop = tel["targeted_scan_noop_reasons"]
    assert "negative_risk_complete" in noop
    assert "0/" in noop["negative_risk_complete"]         # sampled count in the reason
    assert "short_resolution" in noop
    assert "metadata" in noop["negative_risk_complete"]    # explains the WHY


# --- Bregman contract: binary groups counted, not category hits ------------ #
def _binary_near_miss(mid="573655", age=7, depth=5):
    from engine.markets.universe_manager import MarketRecord
    from engine.training.bregman_grouping import group_markets
    from engine.training.bregman_near_miss import analyze_rejection
    rec = MarketRecord.from_raw(
        {"id": mid, "question": "Will X happen?", "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": ["tokY", "tokN"],
         "bestBid": "0.5", "bestAsk": "0.51", "topDepthUsd": str(depth),
         "bookUpdatedTs": str(_NOW - age), "liquidityNum": "8000"}, now=_NOW)
    g = group_markets([rec])[0]
    nm = analyze_rejection(g, "depth_too_thin", min_depth_usd=25.0,
                           max_spread=0.08, max_age_s=20.0)
    return rec, g, nm


def test_bregman_binary_near_miss_makes_binary_groups_seen_positive():
    rec, g, nm = _binary_near_miss()
    assert nm["group_type"] == "binary_yes_no"
    assert nm["single_market_binary"] is True
    assert nm["outcome_labels"] == ["YES", "NO"]
    tel = TargetedMarketScanner(enabled=True).scan(
        [rec], near_miss_by_market={rec.market_id: nm}, bregman_groups=[nm], now=_NOW)
    assert tel["targeted_scan_bregman_groups_seen"] == 1
    assert tel["targeted_scan_binary_groups_seen"] > 0      # was the 'binaries seen=0' bug
    assert tel["targeted_scan_yes_no_pairs_seen"] > 0
    assert tel["targeted_scan_field_source"].startswith("bregman_normalized_groups")


def test_cannot_report_zero_binaries_when_bregman_binary_groups_exist():
    _, _, nm = _binary_near_miss(mid="a")
    _, _, nm2 = _binary_near_miss(mid="b")
    tel = TargetedMarketScanner(enabled=True).scan([], bregman_groups=[nm, nm2], now=_NOW)
    assert tel["targeted_scan_binary_groups_seen"] == 2     # counted from groups, not records
    # the no-op reason can NEVER say binaries seen=0 here
    noop = tel["targeted_scan_noop_reasons"].get("high_liquidity_binary", "")
    assert "binaries seen=0" not in noop


def test_classify_bregman_group_binary_categories():
    from engine.training.targeted_scan import classify_bregman_group
    thin = {"group_type": "binary_yes_no", "single_market_binary": True,
            "outcome_labels": ["YES", "NO"], "token_ids": ["y", "n"],
            "depth_quality": {"thin_legs": 2}, "freshness": {"stale_legs": 0},
            "spread_quality": {"wide_legs": 0}, "completeness": {"completeness_proven": True}}
    deep = dict(thin, depth_quality={"thin_legs": 0})
    assert "complete_yes_no_tight_spread" in classify_bregman_group(thin)
    assert "high_liquidity_binary" not in classify_bregman_group(thin)   # thin != high-liq
    assert "high_liquidity_binary" in classify_bregman_group(deep)


def test_bregman_binary_populates_categories_even_when_raw_missing_fields():
    # raw records carry NO depth/timestamp; Bregman normalized binary groups must
    # still populate the binary categories (raw missing must not hide them).
    from engine.markets.universe_manager import MarketRecord
    from engine.training.bregman_grouping import group_markets
    from engine.training.bregman_near_miss import analyze_rejection
    raw_recs = []
    nms = []
    for i in range(3):
        full = MarketRecord.from_raw(
            {"id": f"m{i}", "question": "Will X?", "outcomes": json.dumps(["Yes", "No"]),
             "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": [f"m{i}Y", f"m{i}N"],
             "bestBid": "0.5", "bestAsk": "0.51", "topDepthUsd": "400",
             "bookUpdatedTs": str(_NOW - 5), "liquidityNum": "20000"}, now=_NOW)
        nms.append(analyze_rejection(group_markets([full])[0], "no_positive_edge",
                                     min_depth_usd=25.0, max_spread=0.08, max_age_s=20.0))
        # the RAW record handed to targeted scan is field-poor (no depth/ts)
        raw_recs.append(MarketRecord.from_raw(
            {"id": f"m{i}", "question": "Will X?", "outcomes": json.dumps(["Yes", "No"]),
             "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": [f"m{i}Y", f"m{i}N"],
             "bestBid": "0.5", "bestAsk": "0.51"}, now=_NOW))
    tel = TargetedMarketScanner(enabled=True).scan(raw_recs, bregman_groups=nms, now=_NOW)
    assert tel["targeted_scan_binary_groups_seen"] == 3
    # headline binary category is populated FROM Bregman normalized groups
    assert tel["high_liquidity_binary_markets_scanned"] >= 3
    assert tel["targeted_scan_bregman_categories"].get("high_liquidity_binary", 0) >= 3


def test_category_populated_or_exact_normalized_reject_reason():
    from engine.markets.universe_manager import MarketRecord
    from engine.training.bregman_grouping import group_markets
    from engine.training.bregman_near_miss import analyze_rejection
    rec = MarketRecord.from_raw(
        {"id": "thin", "question": "Will X?", "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": ["tY", "tN"],
         "bestBid": "0.5", "bestAsk": "0.51", "topDepthUsd": "3",
         "bookUpdatedTs": str(_NOW - 5)}, now=_NOW)
    nm = analyze_rejection(group_markets([rec])[0], "depth_too_thin",
                           min_depth_usd=25.0, max_spread=0.08, max_age_s=20.0)
    tel = TargetedMarketScanner(enabled=True).scan([rec], bregman_groups=[nm], now=_NOW)
    # high_liquidity_binary is 0 (thin) BUT the normalized reject reason proves WHY
    assert tel["targeted_scan_normalized_reject_reasons"].get("depth_too_thin", 0) >= 1
    assert tel["targeted_scan_binary_groups_seen"] >= 1   # still a real binary group


def test_bregman_leg_freshness_reconciles_with_market_record():
    rec, g, _ = _binary_near_miss(age=7)
    assert rec.book_age_s is not None
    for leg in g.legs:
        assert leg.book_age_s == rec.book_age_s            # single parser, leg populated


def test_shared_timestamp_parser_sec_ms_iso_missing():
    from engine.arbitrage.price_parsing import parse_epoch_seconds
    assert abs(parse_epoch_seconds(_NOW) - _NOW) < 1
    assert abs(parse_epoch_seconds(int(_NOW * 1000)) - _NOW) < 1
    assert parse_epoch_seconds("2026-06-10T04:00:00+00:00") is not None
    assert parse_epoch_seconds(None) is None
    assert parse_epoch_seconds("") is None
    assert parse_epoch_seconds("null") is None


def test_missing_timestamp_record_not_stale_via_shared_parser():
    from engine.markets.universe_manager import MarketRecord
    rec = MarketRecord.from_raw(
        {"id": "m", "question": "Q", "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": ["mY", "mN"],
         "bestBid": "0.5", "bestAsk": "0.51", "liquidityNum": "8000"}, now=_NOW)  # no ts
    assert rec.book_age_s is None                          # missing -> unknown, not 0/old
    tel = TargetedMarketScanner(enabled=True).scan([rec], now=_NOW)
    assert tel["stale_book_scan_waste_count"] == 0


def test_cooldown_metrics_reconcile():
    s = TargetedMarketScanner(enabled=True, cooldown_ticks=5)
    bad = _mkt(mid="bad", depth=1)                        # known-thin (real topDepthUsd=1)
    seen_reason = {}
    for _ in range(4):
        tel = s.scan([bad], now=_NOW)
        seen_reason.update(tel.get("scan_cooldown_reason_counts", {}))
    assert tel["scan_cooldown_active_groups"] >= 1
    assert seen_reason.get("thin_depth", 0) >= 1          # cooldown reason reconciles


# --- trainer integration: telemetry + no gate change ----------------------- #
def test_live_trainer_path_populates_binary_metrics_from_bregman(tmp_path, monkeypatch):
    """End-to-end LIVE path: scan_bregman() -> _run_targeted_scan(bregman_groups=...) ->
    bregman_exec_metrics. Proves binary metrics come from Bregman-normalized groups
    (the user's exact bug: 'binaries seen=0' while Bregman has binary_yes_no groups)."""
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from engine.markets.universe_manager import MarketRecord
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    now = time.time()
    recs = [MarketRecord.from_raw(
        {"id": f"m{i}", "question": f"Will event {i} happen?",
         "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.49"]), "clobTokenIds": [f"m{i}Y", f"m{i}N"],
         "bestBid": "0.5", "bestAsk": "0.51", "topDepthUsd": "5",
         "bookUpdatedTs": str(now - 5), "liquidityNum": "8000"}, now=now) for i in range(6)]
    t.closed_loop.begin_tick()
    t.scan_bregman(recs, now=now)                  # the LIVE path
    bx = t.bregman_exec_metrics
    assert bx["bregman_near_misses_total"] >= 6
    # binary metrics MUST be populated from Bregman-normalized groups (never 0)
    assert bx["targeted_scan_bregman_groups_seen"] >= 6
    assert bx["targeted_scan_binary_groups_seen"] >= 6     # was 'binaries seen=0'
    assert bx["targeted_scan_yes_no_pairs_seen"] >= 6
    assert bx["complete_yes_no_tight_spread_markets_scanned"] >= 6
    # thin markets -> high_liquidity_binary may be 0, but the EXACT blocker is proven
    assert bx["targeted_scan_normalized_reject_reasons"].get("depth_too_thin", 0) >= 1
    assert bx["targeted_scan_field_source"].startswith("bregman_normalized_groups")


def test_trainer_targeted_scan_metrics_and_no_trade(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from engine.markets.universe_manager import MarketRecord
    from tests._pmtrain_helpers import clean_live_env
    import engine.training.polymarket_trainer as P
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    recs = [MarketRecord.from_raw(_mkt(mid="hi", q="Will BTC top $100k?"), now=time.time()),
            MarketRecord.from_raw(_mkt(mid="thin", depth=5, liq=100, vol=0), now=time.time())]
    # no certifiable groups -> certified stays 0 (gates intact), but telemetry populated
    from engine.training.bregman_grouping import group_markets as _gm
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "hi"}], now=time.time())  # records arg drives grouping
    tel = t._run_targeted_scan(recs, [], time.time())
    assert tel["targeted_market_scan_enabled"] is True
    assert tel["targeted_markets_scanned_total"] == 2
    assert tel["market_quality_tier_counts"]
    assert tel["targeted_scan_can_execute"] is False
