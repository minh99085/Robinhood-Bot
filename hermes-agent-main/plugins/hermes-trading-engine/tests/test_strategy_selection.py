"""Tests for the tiered strategy router + PnL attribution. Tests-first.

Covers: Tier-1 Bregman outranks all; Tier-2 dislocation gating; Tier-3 model
edge; Tier-4 exploration last resort; aggressive bad-fill rejection; dynamic EV
cutoffs via threshold learning; exploration vs validation PnL separation.
"""

from __future__ import annotations

from engine.strategies.router import (
    RouterConfig,
    StrategyRouter,
    StrategySignal,
    ThresholdLearner,
    Tier,
)
from engine.strategies.strategy_attribution import (
    PnLAttribution,
    split_exploration_validation,
)


class _Cert:
    def __init__(self, certified=True, fill=True, size=10.0):
        self.certified = certified
        self.fill_feasible = fill
        self.size = size


class _Opp:
    def __init__(self, edge=0.05, certified=True, fill=True, size=10.0, ids=("a", "b")):
        self.edge = edge
        self.outcome_ids = list(ids)
        self.certificate = _Cert(certified, fill, size)


def _router():
    return StrategyRouter(RouterConfig())


def test_tier1_bregman_outranks_everything():
    r = _router()
    disloc = r.dislocation_signal(edge=0.5, size=5, fill_ok=True, regime="trending_up")
    model = r.model_edge_signal(edge=0.5, size=5, fill_ok=True)
    expl = r.exploration_signal(size=1)
    d = r.route(bregman=[_Opp(edge=0.03)], dislocation=disloc,
                model_edge=model, exploration=expl)
    assert d.tier == int(Tier.BREGMAN)
    assert d.selected.source == "bregman"


def test_uncertified_bregman_rejected_then_lower_tier_used():
    r = _router()
    disloc = r.dislocation_signal(edge=0.5, size=5, fill_ok=True, regime="trending_up")
    d = r.route(bregman=[_Opp(edge=0.5, certified=False)], dislocation=disloc)
    assert d.tier == int(Tier.DISLOCATION)
    assert any(s.source == "bregman" for s in d.rejected)


def test_tier2_dislocation_when_no_bregman():
    r = _router()
    disloc = r.dislocation_signal(edge=0.5, size=5, fill_ok=True, regime="trending_up")
    model = r.model_edge_signal(edge=0.5, size=5, fill_ok=True)
    d = r.route(dislocation=disloc, model_edge=model)
    assert d.tier == int(Tier.DISLOCATION)


def test_tier2_blocked_falls_through_to_model_edge():
    r = _router()
    disloc = r.dislocation_signal(edge=0.5, size=5, fill_ok=True,
                                  regime="chop", block_reason="regime_chop")
    model = r.model_edge_signal(edge=0.5, size=5, fill_ok=True)
    d = r.route(dislocation=disloc, model_edge=model)
    assert d.tier == int(Tier.MODEL_EDGE)
    assert any("tier2_blocked" in x for x in d.reasons)


def test_tier4_exploration_is_last_resort():
    r = _router()
    expl = r.exploration_signal(size=1)
    d = r.route(exploration=expl)
    assert d.tier == int(Tier.EXPLORATION)
    assert d.selected.is_exploration is True


def test_bad_fill_rejected_aggressively():
    r = _router()
    disloc = r.dislocation_signal(edge=0.5, size=5, fill_ok=False, regime="trending_up")
    model = r.model_edge_signal(edge=0.5, size=5, fill_ok=False)
    d = r.route(dislocation=disloc, model_edge=model)
    assert d.selected is None
    assert len(d.rejected) == 2
    assert any("bad_fill" in x for x in d.reasons)


def test_below_cutoff_rejected():
    r = StrategyRouter(RouterConfig(tier3_min_edge=0.10))
    model = r.model_edge_signal(edge=0.05, size=5, fill_ok=True)
    d = r.route(model_edge=model)
    assert d.selected is None
    assert any("tier3_below_cutoff" in x for x in d.reasons)


def test_threshold_learner_tightens_after_losses():
    cfg = RouterConfig(tier3_min_edge=0.02, cutoff_loss_step=0.01, cutoff_max_extra=0.05)
    learner = ThresholdLearner(cfg)
    base = cfg.tier3_min_edge
    assert learner.cutoff(base) == base
    for _ in range(3):
        learner.update(-1.0)
    assert learner.cutoff(base) > base
    # bounded
    for _ in range(50):
        learner.update(-1.0)
    assert learner.cutoff(base) <= base + cfg.cutoff_max_extra + 1e-9


def test_threshold_learner_relaxes_after_wins():
    cfg = RouterConfig(cutoff_loss_step=0.02, cutoff_win_relax=0.01)
    learner = ThresholdLearner(cfg)
    learner.update(-1.0)
    high = learner.extra
    learner.update(+1.0)
    assert learner.extra < high


def test_dynamic_cutoff_changes_routing():
    cfg = RouterConfig(tier3_min_edge=0.02, cutoff_loss_step=0.05, cutoff_max_extra=0.10)
    learner = ThresholdLearner(cfg)
    r = StrategyRouter(cfg, threshold_learner=learner)
    sig = lambda: r.model_edge_signal(edge=0.04, size=5, fill_ok=True)
    assert r.route(model_edge=sig()).tier == int(Tier.MODEL_EDGE)  # 0.04 >= 0.02
    learner.update(-1.0)  # cutoff -> 0.07
    assert r.route(model_edge=sig()).selected is None  # 0.04 < 0.07


def test_exploration_pnl_separated_from_validation():
    attr = PnLAttribution()
    attr.record("bregman", 2.0, tier=1)
    attr.record("btc_pulse", -0.5, tier=2)
    attr.record("exploration", 0.3, tier=4, is_exploration=True)
    s = attr.summary()
    assert s["validation_pnl"] == 1.5
    assert s["exploration_pnl"] == 0.3
    assert s["total_pnl"] == 1.8
    assert s["n_validation"] == 2 and s["n_exploration"] == 1
    assert s["exploration_excluded_from_validation"] is True


def test_split_helper_matches_records():
    recs = [
        {"strategy": "bregman", "pnl": 1.0, "tier": 1},
        {"strategy": "exploration", "pnl": 5.0, "tier": 4, "is_exploration": True},
    ]
    s = split_exploration_validation(recs)
    assert s["validation_pnl"] == 1.0
    assert s["exploration_pnl"] == 5.0  # exploration never inflates validation
    assert s["by_strategy"]["bregman"] == 1.0


def test_decision_serializes():
    r = _router()
    d = r.route(bregman=[_Opp()])
    out = d.to_dict()
    assert out["tier"] == 1
    assert out["selected"]["source"] == "bregman"
