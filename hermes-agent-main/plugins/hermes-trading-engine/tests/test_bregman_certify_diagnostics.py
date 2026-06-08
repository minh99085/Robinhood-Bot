"""Trainer Bregman certifier diagnostics — certification is never silent.

Every group that reaches BregmanArbitrageEngine.certify logs (even on reject):
exhaustive, settlement_consistent, divergence_gap, projected_profit_lower_bound, and
the precise pipeline STAGE (validate_simplex / settlement_consistent / realism / edge
/ certified). Gates stay strict (depth/spread/freshness/ambiguity unchanged). Improved
exhaustive detection never fabricates completeness.
"""

from engine.training.bregman_execution import BregmanArbitrageEngine, rejection_stage
from engine.training.bregman_grouping import (SimplexGroup, SimplexLeg, group_markets,
                                              _group_is_exhaustive)


def _leg(mid, outcome, tok, ask, depth, *, fresh=True, stale=False, amb=0.0):
    return SimplexLeg(market_id=mid, outcome=outcome, token_id=tok, ask=ask,
                      bid=round(ask - 0.01, 4), depth_usd=depth, fresh_book=fresh,
                      stale=stale, ambiguity_score=amb)


def _eng():
    return BregmanArbitrageEngine()


def test_rejection_stage_taxonomy_is_exhaustive():
    assert rejection_stage("not_exhaustive") == "validate_simplex"
    assert rejection_stage("invalid_simplex") == "validate_simplex"
    assert rejection_stage("settlement_ambiguity") == "settlement_consistent"
    assert rejection_stage("depth_too_thin") == "realism"
    assert rejection_stage("stale_book") == "realism"
    assert rejection_stage("no_positive_edge") == "edge"


def test_not_exhaustive_reject_logs_full_diagnostics():
    eng = _eng()
    req = float(eng.min_depth_usd)
    g = SimplexGroup("ne", "mutually_exclusive",
                     [_leg("a", "YES", "aY", 0.3, req * 4),
                      _leg("b", "YES", "bY", 0.3, req * 4)], exhaustive=False)
    c = eng.certify(g)
    assert c.certified is False
    assert c.rejection_stage == "validate_simplex"
    d = c.certify_diagnostics
    assert d["exhaustive"] is False
    assert d["settlement_consistent"] is False
    assert d["divergence_gap"] is not None
    # projected lower bound is logged EVEN ON REJECT (never silent)
    assert d["projected_profit_lower_bound"] is not None
    assert d["failure_mode"] == "not_exhaustive"


def test_depth_reject_is_realism_stage_with_projected_lb():
    eng = _eng()
    g = SimplexGroup("th", "binary_yes_no",
                     [_leg("a", "YES", "aY", 0.4, 5.0), _leg("a", "NO", "aN", 0.4, 5.0)],
                     exhaustive=True)
    c = eng.certify(g)
    assert c.rejection_stage == "realism"
    assert c.no_trade_reason == "depth_too_thin"
    assert c.certify_diagnostics["projected_profit_lower_bound"] is not None


def test_ambiguity_reject_is_settlement_stage():
    eng = _eng()
    req = float(eng.min_depth_usd)
    high_amb = float(eng.max_ambiguity) + 0.5
    g = SimplexGroup("amb", "binary_yes_no",
                     [_leg("a", "YES", "aY", 0.4, req * 4, amb=high_amb),
                      _leg("a", "NO", "aN", 0.4, req * 4, amb=high_amb)], exhaustive=True)
    c = eng.certify(g)
    assert c.no_trade_reason == "settlement_ambiguity"
    assert c.rejection_stage == "settlement_consistent"
    assert c.certify_diagnostics["max_ambiguity_score"] == high_amb


def test_certified_group_logs_certified_stage_and_positive_lb():
    eng = _eng()
    req = float(eng.min_depth_usd)
    g = SimplexGroup("ok", "mutually_exclusive",
                     [_leg(m, "YES", m + "Y", 0.3, req * 8) for m in ("a", "b", "c")],
                     exhaustive=True)
    c = eng.certify(g)
    assert c.certified is True and c.is_opportunity is True
    assert c.rejection_stage == "certified"
    assert c.certify_diagnostics["settlement_consistent"] is True
    assert c.certify_diagnostics["projected_profit_lower_bound"] > 0


def test_diagnostics_serialize_in_to_dict():
    eng = _eng()
    g = SimplexGroup("ne", "mutually_exclusive",
                     [_leg("a", "YES", "aY", 0.3, 5.0), _leg("b", "YES", "bY", 0.3, 5.0)],
                     exhaustive=False)
    d = eng.certify(g).to_dict()
    assert "rejection_stage" in d and "certify_diagnostics" in d
    assert d["certify_diagnostics"]["divergence_gap"] is not None


# --- improved exhaustive detection (never fabricated) ----------------------- #
def test_exhaustive_detected_from_declared_market_count():
    recs = [{"market_id": "a", "group_key": "evt:e1",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 3},
             "top_depth_usd": 100, "clob_token_ids": ["aY", "aN"], "question": "Who?"},
            {"market_id": "b", "group_key": "evt:e1",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 3},
             "top_depth_usd": 100, "clob_token_ids": ["bY", "bN"], "question": "Who?"},
            {"market_id": "c", "group_key": "evt:e1",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 3},
             "top_depth_usd": 100, "clob_token_ids": ["cY", "cN"], "question": "Who?"}]
    assert _group_is_exhaustive(recs) is True       # declared count == 3 legs


def test_exhaustive_detected_from_events_markets_length():
    recs = [{"market_id": "a", "group_key": "evt:e2",
             "raw": {"bestAsk": "0.5", "bestBid": "0.49",
                     "events": [{"id": "e2", "markets": [1, 2]}]},
             "top_depth_usd": 100, "clob_token_ids": ["aY", "aN"]},
            {"market_id": "b", "group_key": "evt:e2",
             "raw": {"bestAsk": "0.5", "bestBid": "0.49",
                     "events": [{"id": "e2", "markets": [1, 2]}]},
             "top_depth_usd": 100, "clob_token_ids": ["bY", "bN"]}]
    assert _group_is_exhaustive(recs) is True


def test_incomplete_group_not_fabricated_as_exhaustive():
    # 2 grouped legs but declared marketCount=5 -> NOT complete (no fabrication)
    recs = [{"market_id": "a", "group_key": "evt:e3",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 5},
             "top_depth_usd": 100, "clob_token_ids": ["aY", "aN"]},
            {"market_id": "b", "group_key": "evt:e3",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 5},
             "top_depth_usd": 100, "clob_token_ids": ["bY", "bN"]}]
    assert _group_is_exhaustive(recs) is False
