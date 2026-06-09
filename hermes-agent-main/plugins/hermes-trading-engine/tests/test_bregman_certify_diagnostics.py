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


def test_zero_certified_explanation_positive_projected_but_rejected(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    import engine.training.polymarket_trainer as P
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    req = float(t.bregman.min_depth_usd)
    # incomplete subsets with POSITIVE raw projected profit (sum < $1) -> rejected
    g1 = SimplexGroup("ne1", "mutually_exclusive",
                      [_leg("a", "YES", "aY", 0.3, req * 4), _leg("b", "YES", "bY", 0.3, req * 4)],
                      exhaustive=False)
    g2 = SimplexGroup("ne2", "mutually_exclusive",
                      [_leg("c", "YES", "cY", 0.2, req * 4), _leg("d", "YES", "dY", 0.25, req * 4)],
                      exhaustive=False)
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [g1, g2])
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "a"}, {"market_id": "c"}], now=1000.0)
    bx = t.bregman_exec_metrics
    assert bx["certified_opportunities"] == 0
    # non-zero projected profit IS logged even though 0 certified
    assert bx["bregman_best_projected_lower_bound"] is not None
    assert bx["bregman_best_projected_lower_bound"] > 0
    assert bx["bregman_positive_projected_but_rejected_count"] == 2
    assert bx["bregman_positive_projected_rejected_by_stage"]["validate_simplex"] == 2
    assert bx["bregman_rejection_stage_counts"]["validate_simplex"] == 2
    expl = bx["bregman_zero_certified_explanation"]
    assert "validate_simplex" in expl and "POSITIVE raw projected profit" in expl
    assert "exhaustive" in expl.lower()       # explains the gate (not loosened)


def test_profit_lower_bound_is_always_float_on_reject():
    eng = _eng()
    req = float(eng.min_depth_usd)
    # not_exhaustive (positive lb), coherent (zero lb), overpriced (negative lb)
    for legs, exh, expect_sign in [
        ([_leg("a", "YES", "aY", 0.3, req * 4), _leg("b", "YES", "bY", 0.3, req * 4)], False, "pos"),
        ([_leg("c", "YES", "cY", 0.5, req * 4), _leg("c", "NO", "cN", 0.5, req * 4)], True, "zero"),
        ([_leg("d", "YES", "dY", 0.6, req * 4), _leg("d", "NO", "dN", 0.6, req * 4)], True, "neg")]:
        g = SimplexGroup("g", "binary_yes_no" if exh else "mutually_exclusive",
                         legs, mutually_exclusive=True, exhaustive=exh)
        lb = eng.certify(g).certify_diagnostics["profit_lower_bound"]
        assert isinstance(lb, float)          # ALWAYS a float, never None
        if expect_sign == "pos":
            assert lb > 0
        elif expect_sign == "neg":
            assert lb < 0
        else:
            assert abs(lb) < 1e-9


def test_per_group_profit_lower_bound_census_in_metrics(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    import engine.training.polymarket_trainer as P
    clean_live_env(monkeypatch, tmp_path)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    req = float(t.bregman.min_depth_usd)
    g1 = SimplexGroup("ne", "mutually_exclusive",
                      [_leg("a", "YES", "aY", 0.3, req * 4), _leg("b", "YES", "bY", 0.3, req * 4)],
                      exhaustive=False)                                    # +0.4
    g2 = SimplexGroup("over", "binary_yes_no",
                      [_leg("d", "YES", "dY", 0.6, req * 4), _leg("d", "NO", "dN", 0.6, req * 4)],
                      exhaustive=True)                                     # -0.2
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [g1, g2])
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "a"}], now=1000.0)
    bx = t.bregman_exec_metrics
    # every group is in the sample with an exhaustive flag + float lower bound
    sample = bx["bregman_certify_diagnostics_sample"]
    assert len(sample) == 2
    for s in sample:
        assert "exhaustive" in s and "settlement_consistent" in s
        assert isinstance(s["profit_lower_bound"], float)
        assert s["rejection_reason"]
    assert bx["bregman_groups_positive_lower_bound"] == 1
    assert bx["bregman_groups_negative_lower_bound"] == 1
    assert bx["bregman_profit_lower_bound_min"] is not None
    assert bx["bregman_profit_lower_bound_max"] is not None


def test_zero_certified_explanation_when_certified():
    from engine.training import PolymarketPaperTrainer
    expl = PolymarketPaperTrainer._bregman_zero_certified_explanation(
        2, {"certified": 2}, 0, {}, 0.1)
    assert "certified" in expl


def test_incomplete_group_not_fabricated_as_exhaustive():
    # 2 grouped legs but declared marketCount=5 -> NOT complete (no fabrication)
    recs = [{"market_id": "a", "group_key": "evt:e3",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 5},
             "top_depth_usd": 100, "clob_token_ids": ["aY", "aN"]},
            {"market_id": "b", "group_key": "evt:e3",
             "raw": {"bestAsk": "0.3", "bestBid": "0.29", "marketCount": 5},
             "top_depth_usd": 100, "clob_token_ids": ["bY", "bN"]}]
    assert _group_is_exhaustive(recs) is False
