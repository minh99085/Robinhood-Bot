"""Calibration-weighted Grok trust (advisory-only).

Grok's probability is blended into p_raw with a weight scaled by its MEASURED
calibration (rolling Brier of its directional probability vs realized outcomes).
Well-calibrated Grok earns up to full weight; poorly-calibrated Grok is floored;
until min_samples outcomes exist the default (1.0) is used. Grok never gates a trade.
"""

from __future__ import annotations

from engine.training.grok_calibration import GrokCalibration


def _cal(tmp_path, **kw):
    kw.setdefault("min_samples", 10)
    kw.setdefault("trust_min", 0.2)
    return GrokCalibration(path=str(tmp_path / "grok_cal.json"), **kw)


def test_only_grok_sources_are_recorded(tmp_path):
    c = _cal(tmp_path)
    c.record(predicted_prob=0.9, won=True, source="market_only")
    c.record(predicted_prob=0.9, won=True, source="simulated")
    assert c.sample_count() == 0
    c.record(predicted_prob=0.9, won=True, source="grok_online")
    assert c.sample_count() == 1


def test_directional_prob_uses_taken_side():
    assert GrokCalibration.directional_prob(0.8, "YES") == 0.8
    assert abs(GrokCalibration.directional_prob(0.8, "NO") - 0.2) < 1e-9


def test_insufficient_samples_returns_default_trust(tmp_path):
    c = _cal(tmp_path, min_samples=10, trust_default=1.0)
    for _ in range(5):
        c.record(predicted_prob=0.9, won=True, source="grok_online")
    assert c.sample_count() == 5
    assert c.trust_weight() == 1.0          # < min_samples -> behave as before


def test_well_calibrated_grok_earns_high_trust(tmp_path):
    c = _cal(tmp_path, min_samples=10, trust_min=0.2)
    # confident + correct every time -> Brier ~0 -> trust ~1.0
    for _ in range(20):
        c.record(predicted_prob=0.95, won=True, source="grok_online")
    assert c.brier() is not None and c.brier() < 0.05
    assert c.trust_weight() >= 0.9


def test_poorly_calibrated_grok_is_floored(tmp_path):
    c = _cal(tmp_path, min_samples=10, trust_min=0.2)
    # confident but WRONG every time -> Brier ~0.9 -> trust floored at trust_min
    for _ in range(20):
        c.record(predicted_prob=0.95, won=False, source="grok_online")
    assert c.brier() is not None and c.brier() > 0.5
    assert c.trust_weight() == 0.2          # floored, never zero


def test_uninformative_grok_gets_low_trust(tmp_path):
    c = _cal(tmp_path, min_samples=10, trust_min=0.2)
    # always predicts 0.5 -> Brier 0.25 (uninformative) -> trust at the floor
    for i in range(20):
        c.record(predicted_prob=0.5, won=(i % 2 == 0), source="grok_online")
    assert abs(c.brier() - 0.25) < 0.05
    assert c.trust_weight() <= 0.3


def test_record_position_maps_side_and_outcome(tmp_path):
    c = _cal(tmp_path, min_samples=1)
    # bought NO, won -> resolved NO -> Grok's directional prob = 1-0.7 = 0.3, correct(1)
    c.record_position(p_research=0.7, side="NO", won=True, source="grok_cache")
    assert c.sample_count() == 1
    assert abs(c._records[-1]["p"] - 0.3) < 1e-9 and c._records[-1]["won"] == 1


def test_persistence_round_trip(tmp_path):
    c = _cal(tmp_path, min_samples=5)
    for _ in range(8):
        c.record(predicted_prob=0.9, won=True, source="grok_online")
    w = c.trust_weight()
    c2 = _cal(tmp_path, min_samples=5)       # reload from disk
    assert c2.sample_count() == 8
    assert c2.trust_weight() == w


def test_disabled_returns_full_trust(tmp_path):
    c = _cal(tmp_path, min_samples=1, enabled=False)
    for _ in range(20):
        c.record(predicted_prob=0.95, won=False, source="grok_online")   # terrible
    assert c.trust_weight() == 1.0          # disabled -> no scaling


class _StubCal:
    """Minimal calibration stub returning a fixed trust weight."""

    def __init__(self, trust):
        self._t = trust

    def is_grok(self, source):
        return str(source or "") in ("grok_online", "grok_cache")

    def trust_weight(self, *, source=None, category=None):
        return self._t


def test_probability_stack_scales_research_blend_by_trust(tmp_path, monkeypatch):
    from engine.markets import universe_manager as um
    from engine.training import TrainingConfig
    from engine.training.probability_stack import ProbabilityStack
    from tests._pmtrain_helpers import FakeResearch, market, clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    raw = market(0, bid=0.39, ask=0.41, depth=2000, category="crypto", now=1_000_000.0)
    rec = um.MarketRecord.from_raw(raw, now=1_000_000.0)
    sig = FakeResearch(fair=0.80, conf=0.9, source="grok_cache")    # fair well above mid
    cfg = TrainingConfig(mode="paper_train")
    hi = ProbabilityStack(cfg, grok_calibration=_StubCal(1.0)).estimate(
        rec, sig, now=1_000_000.0)
    lo = ProbabilityStack(cfg, grok_calibration=_StubCal(0.2)).estimate(
        rec, sig, now=1_000_000.0)
    mid = hi.p_market_mid
    # higher Grok trust pulls p_raw FURTHER toward Grok's fair value (0.80 > mid)
    assert hi.p_raw > lo.p_raw
    assert abs(hi.p_raw - mid) > abs(lo.p_raw - mid)


def test_metrics_block_shape(tmp_path):
    c = _cal(tmp_path)
    m = c.metrics()
    for k in ("grok_calibration_enabled", "grok_calibration_samples", "grok_brier_score",
              "grok_trust_weight", "grok_trust_min", "grok_calibration_measured",
              "advisory_only"):
        assert k in m
    assert m["advisory_only"] is True
