"""OnlineLearner + FeedbackLoop: explainable, bucketed online learning."""

from __future__ import annotations

from engine.training.online_learner import OnlineLearner
from engine.training.feedback_loop import FeedbackLoop


def _learner(tmp_path):
    return OnlineLearner(path=tmp_path / "learner.json", min_bucket_samples=2)


def test_online_learner_records_decisions_and_no_trade_reasons(tmp_path):
    L = _learner(tmp_path)
    L.record_decision(traded=True)
    L.record_decision(traded=False, reason="spread_too_wide")
    L.record_decision(traded=False, reason="spread_too_wide")
    assert L.trades == 1 and L.no_trades == 2
    assert L.no_trade_reasons["spread_too_wide"] == 2


def test_online_learner_calibration_table_and_error(tmp_path):
    L = _learner(tmp_path)
    # predicted ~0.6 but always loses -> calibration gap
    for _ in range(4):
        L.record_outcome(predicted_prob=0.6, win=False, realized_pnl=-0.1,
                         category="politics", net_edge=0.05)
    table = L.calibration_table()
    assert table and table[0]["n"] == 4
    assert L.calibration_error() > 0.0


def test_online_learner_category_reliability_and_bias(tmp_path):
    L = _learner(tmp_path)
    for _ in range(5):
        L.record_outcome(predicted_prob=0.6, win=True, realized_pnl=0.1, category="sports")
    rel = L.category_reliability()
    assert rel["sports"] > 0.5
    # winning category -> positive (small) learned model bias
    assert L.category_bias("sports") > 0.0
    assert L.category_bias("unseen") == 0.0


def test_online_learner_edge_buckets_and_markouts(tmp_path):
    L = _learner(tmp_path)
    L.record_outcome(predicted_prob=0.55, win=True, realized_pnl=0.2, net_edge=0.05,
                     markouts={"5s": 0.01, "1m": 0.03})
    assert L.edge_buckets, "edge bucket pnl recorded"
    ms = L.markout_summary()
    assert "5s" in ms and "1h" in ms


def test_online_learner_persists_and_reloads(tmp_path):
    L = _learner(tmp_path)
    L.record_decision(traded=True)
    L.persist()
    L2 = OnlineLearner(path=tmp_path / "learner.json")
    assert L2.trades == 1


def test_feedback_loop_updates_bucket_stats(tmp_path):
    L = _learner(tmp_path)
    fb = FeedbackLoop(L, interval_seconds=0.0, enabled=True)
    for _ in range(6):
        fb.record_outcome(predicted_prob=0.6, predicted_edge=0.05, realized_pnl=0.1,
                          size_usd=5.0, win=True, category="politics", net_edge=0.05)
    summary = fb.maybe_update(force=True)
    assert summary["learner"]["closed"] == 6
    assert summary["updates"] == 1


def test_feedback_edge_adjustment_responds_to_outcomes(tmp_path):
    # winners widen the gate (>1), losers tighten it (<1)
    Lw = _learner(tmp_path / "w")
    fbw = FeedbackLoop(Lw, enabled=True)
    for _ in range(8):
        fbw.record_outcome(predicted_prob=0.6, predicted_edge=0.05, realized_pnl=0.1,
                           size_usd=5.0, win=True)
    Ll = _learner(tmp_path / "l")
    fbl = FeedbackLoop(Ll, enabled=True)
    for _ in range(8):
        fbl.record_outcome(predicted_prob=0.6, predicted_edge=0.05, realized_pnl=-0.1,
                           size_usd=5.0, win=False)
    assert fbw.edge_adjustment() > fbl.edge_adjustment()


def test_feedback_disabled_is_neutral(tmp_path):
    L = _learner(tmp_path)
    fb = FeedbackLoop(L, enabled=False)
    assert fb.edge_adjustment() == 1.0
