"""6A: credible (CI-lower-bound) after-cost edge gate.

A readiness/exploit trade only opens when the LOWER confidence bound of its after-cost
edge clears the floor — "credible positive expectancy". Stricter than the point-estimate
gate; never loosens a hard gate; exploration probes are exempt.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.edge_engine import EdgeEngine
from engine.training.probability_stack import ProbabilityStack

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch


def _est_with_ci(p_final, ci_low, ci_high, *, ask=0.40):
    return SimpleNamespace(
        market_id="m0", p_final=p_final, best_ask=ask, p_market_mid=0.40,
        confidence_interval_low=ci_low, confidence_interval_high=ci_high,
        spread=0.02, ambiguity_score=0.05, stale_score=0.0, evidence_score=0.8,
        calibration_error=0.0, liquidity_usd=20000.0, fresh_book=True,
        research_usable=True, research_source="grok_cache", confidence=0.8,
        model_has_edge=True, chainlink_no_trade=False, chainlink_confidence=0.0,
        chainlink_feed="", research_age_s=None)


def _rec():
    raw = market(0, bid=0.39, ask=0.41, depth=20000, category="crypto", now=1_000_000.0)
    rec = um.MarketRecord.from_raw(raw, now=1_000_000.0)
    rec.market_id = "m0"
    return rec


def test_edge_result_carries_credible_lower_bound():
    cfg = TrainingConfig(mode="paper_train", min_credible_after_cost_edge=0.0)
    # strong YES edge (p 0.70 vs ask 0.40) with a TIGHT CI -> credible lower bound > 0
    est = _est_with_ci(0.70, 0.68, 0.72, ask=0.40)
    r = EdgeEngine(cfg).evaluate(est, _rec(), outcome="YES")
    assert r.net_edge > 0
    assert r.after_cost_edge_lower_bound <= r.net_edge       # lower bound <= point edge
    assert r.credible_positive_expectancy is True


def test_wide_ci_makes_edge_not_credible():
    cfg = TrainingConfig(mode="paper_train", min_credible_after_cost_edge=0.0)
    # same point edge but a WIDE CI -> the unfavorable bound wipes the edge -> not credible
    est = _est_with_ci(0.70, 0.30, 0.95, ask=0.40)
    r = EdgeEngine(cfg).evaluate(est, _rec(), outcome="YES")
    assert r.credible_margin > 0
    assert r.after_cost_edge_lower_bound < r.net_edge
    assert r.credible_positive_expectancy is False


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path)


def test_consider_blocks_non_credible_readiness_trade(tmp_path, monkeypatch):
    clean_live_env(monkeypatch, tmp_path)
    # grok_cache research so the candidate clears hard gates and reaches the 6A gate
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", require_credible_after_cost_edge=True,
                       probability_ensemble_enabled=False),
        data_dir=tmp_path, signal_model=FakeResearch(fair=0.85, source="grok_cache"))
    real_eval = t.prob.estimate

    def wide_ci(rec, sm, now=None):
        est = real_eval(rec, sm, now=now)
        # strong point edge but a WIDE ensemble CI -> not credible
        object.__setattr__(est, "p_final", 0.85)
        object.__setattr__(est, "best_ask", 0.41)
        object.__setattr__(est, "confidence_interval_low", 0.0)
        object.__setattr__(est, "confidence_interval_high", 1.0)
        return est
    monkeypatch.setattr(t.prob, "estimate", wide_ci)
    raw = market(0, bid=0.39, ask=0.41, depth=20000, category="crypto", now=1_000_000.0)
    rec = um.MarketRecord.from_raw(raw, now=1_000_000.0)
    t._consider(rec, now=1_000_000.0)
    # the strong-but-uncertain candidate is NOT opened as a readiness trade
    assert t._credible_gate_metrics.get("readiness_blocked_not_credible", 0) >= 1
    assert not [p for p in t.positions if not getattr(p, "exploration", False)]


def test_profitability_report_surfaces_credible_metrics(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, require_credible_after_cost_edge=True)
    pr = t.profitability_ranking_report()
    for k in ("credible_after_cost_edge_required", "min_credible_after_cost_edge",
              "readiness_credible_trades", "readiness_blocked_not_credible"):
        assert k in pr
    assert pr["credible_after_cost_edge_required"] is True


def test_aggressive_profile_enables_credible_gate():
    from engine.training.config import AggressivePaperTrainingConfig
    assert AggressivePaperTrainingConfig().require_credible_after_cost_edge is True
