"""P0: closed-loop paper-training learning — every candidate becomes a structured
record + pending label + feedback; active learning selects shadow examples even
when nothing is executable; learning state persists and proves growth. PAPER ONLY.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.closed_loop import ClosedLoopLearning

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch

_NOW = 1_000_000.0

_REQUIRED_CLL_KEYS = {
    "closed_loop_enabled", "decision_records_written", "candidate_records_written",
    "rejection_records_written", "shadow_records_written", "no_trade_labels_written",
    "active_learning_shadow_selected", "active_learning_tiny_trades_selected",
    "pending_labels_created", "pending_labels_total", "completed_labels_created",
    "completed_labels_total", "labels_resolved_per_day", "feedback_records_written",
    "feedback_per_hour", "calibration_updates", "brier_before", "brier_after",
    "ece_before", "ece_after", "category_reliability_updated",
    "active_learning_used_feedback", "learning_state_loaded", "learning_state_saved",
    "learning_growth_score", "learning_growth_status", "top_learning_bottlenecks",
    "zero_selection_reason",
}


def _cll(tmp_path, **cfgkw):
    cfg = TrainingConfig(mode="paper_train", **cfgkw)
    return ClosedLoopLearning("run-test", tmp_path, cfg, now=_NOW)


def _rec(mid="m0", *, end_ts=None, question="Will event 0 resolve YES?"):
    return SimpleNamespace(market_id=mid, group_key=f"market:{mid}", cluster_id="sem:x",
                           category="crypto", question=question, top_depth_usd=2000.0,
                           book_age_s=2.0, end_ts=end_ts, raw={"conditionId": "c0"})


def _est(mid_p=0.50):
    return SimpleNamespace(p_market_mid=mid_p, spread=0.02, ambiguity_score=0.05,
                           calibrated_probability=None)


def _edge(net=0.005, px=0.51, pf=0.55):
    return SimpleNamespace(net_edge=net, executable_price=px, p_final=pf)


# --- record creation --------------------------------------------------------

def test_rejected_candidate_writes_training_record(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    r = cl.record(_rec(), _est(), _edge(), decision="no_trade_label", reason="edge_too_low",
                  tick=1)
    assert r is not None
    assert cl.counts["decision_records_written"] == 1
    assert cl.counts["no_trade_labels_written"] == 1
    assert r["label_status"] == "pending"


def test_offline_stub_reject_recorded_as_diagnostic(tmp_path):
    # data/adapter rejects (offline stub, no model probability) are NO LONGER
    # dropped — they emit a diagnostic event (with no label target). This is the
    # exact case that left the live event stream empty.
    cl = _cll(tmp_path)
    cl.begin_tick()
    r = cl.record(_rec(), _est(), _edge(), decision="rejected_hard_gate",
                  reason="offline_stub_blocked", tick=1)
    assert r is not None
    assert cl.counts["decision_records_written"] == 1
    assert cl.counts["diagnostic_records_written"] == 1
    assert cl.counts["diagnostic_without_label_target"] == 1
    assert r["label_status"] == "none"


def test_shadow_label_for_non_executable_informative(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    r = cl.record(_rec(), _est(), _edge(), decision="shadow_only", reason="missing_executable_ask",
                  active_learning={"learning_bucket": "near_miss_positive_edge",
                                   "active_learning_score": 2.0}, tick=1)
    assert r is not None and r["decision"] == "shadow_only"
    assert cl.counts["shadow_records_written"] == 1


# --- active learning selects shadow even with no executable fills -----------

def test_active_learning_selects_shadow_without_fill(tmp_path):
    cl = _cll(tmp_path, active_learning_allow_shadow_without_fill=True)
    cl.begin_tick()
    for i in range(5):
        cl.record(_rec(f"m{i}"), _est(), _edge(), decision="no_trade_label",
                  reason="edge_too_low", tick=1)
    assert cl.counts["active_learning_shadow_selected"] == 5   # NOT zero
    assert cl.metrics()["zero_selection_reason"] is None


def test_zero_selection_reason_when_shadow_disabled(tmp_path):
    cl = _cll(tmp_path, active_learning_allow_shadow_without_fill=False)
    cl.begin_tick()
    cl.record(_rec(), _est(), _edge(), decision="no_trade_label", reason="edge_too_low", tick=1)
    m = cl.metrics()
    assert m["active_learning_shadow_selected"] == 0
    assert m["zero_selection_reason"] == "shadow_learning_disabled"


# --- pending + completed labels persist + resolve ---------------------------

def test_pending_label_created_and_persisted(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(end_ts=_NOW + 86400), _est(), _edge(), decision="no_trade_label",
              reason="edge_too_low", tick=1)
    assert cl.counts["pending_labels_created"] == 1
    assert (tmp_path / "training" / "pending_labels.jsonl").is_file()
    rows = [json.loads(x) for x in
            (tmp_path / "training" / "pending_labels.jsonl").read_text().splitlines()]
    assert rows[0]["label_type"] == "final_settlement"


def test_proxy_label_resolves_into_completed_feedback(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    # no end_ts -> proxy label with a short due window (same simulated clock)
    cl.record(_rec(end_ts=None), _est(0.50), _edge(), decision="no_trade_label",
              reason="edge_too_low", tick=1, now=_NOW)
    assert cl.counts["pending_labels_created"] == 1
    # resolve after the proxy window with a favorable mid
    resolved = cl.resolve_labels({"m0": 0.62}, now=_NOW + 600.0)
    assert resolved == 1
    assert cl.counts["completed_labels_created"] == 1
    # a short-horizon momentum proxy resolves into the DIRECTIONAL-proxy Brier only — it is
    # NOT settlement calibration, so the settlement Brier stays None (honest metric).
    m = cl.metrics()
    assert m["brier_after"] is None                       # settlement-only, no real outcome yet
    assert m["proxy_directional_brier"] is not None        # momentum proxy recorded separately
    assert m["proxy_directional_samples"] == 1
    assert m["settlement_calibration_samples"] == 0
    comp = (tmp_path / "training" / "completed_labels.jsonl")
    assert comp.is_file()
    import json as _json
    row = _json.loads(comp.read_text().strip().splitlines()[-1])
    assert row["counts_for_calibration"] is False and row["metric_basis"] == "directional_proxy"


def _settle_fetch(by_id):
    def _f(market_id, condition_id=None):
        return by_id.get(str(market_id))
    return _f


def test_final_settlement_resolves_real_outcome_and_trains_calibration(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    # final_settlement label (end_ts in the past => due now); base cfg has fast_proxy OFF.
    cl.record(_rec(end_ts=_NOW - 10), _est(0.30), _edge(), decision="no_trade_label",
              reason="edge_too_low", tick=1, now=_NOW)
    assert cl.counts["pending_labels_created"] == 1
    fetch = _settle_fetch({"m0": {"market_id": "m0", "closed": True, "resolved": True,
                                  "winning_outcome": "YES", "settlement_source": "gamma"}})
    n = cl.resolve_labels({}, now=_NOW + 1, settlement_fetcher=fetch)
    assert n == 1
    m = cl.metrics()
    assert m["brier_after"] is not None                  # REAL settlement Brier
    assert m["settlement_calibration_samples"] == 1
    assert m["proxy_directional_brier"] is None           # no momentum proxy here
    comps = cl._settlement_completions
    assert len(comps) == 1 and comps[0]["realized"] == 1  # market settled YES
    assert not cl.pending                                 # terminal: dropped from pending


def test_final_settlement_unresolved_market_stays_pending(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(end_ts=_NOW - 10), _est(), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    fetch = _settle_fetch({"m0": {"market_id": "m0", "closed": False, "resolved": False}})
    n = cl.resolve_labels({}, now=_NOW + 1, settlement_fetcher=fetch)
    assert n == 0 and len(cl.pending) == 1                # not closed yet -> retry later
    assert cl.metrics()["settlement_calibration_samples"] == 0


def test_final_settlement_dirty_is_dropped_not_trained(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(end_ts=_NOW - 10), _est(), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    # closed but ambiguous (no clean winner) -> non-trainable -> recorded + dropped
    fetch = _settle_fetch({"m0": {"market_id": "m0", "closed": True, "resolved": True,
                                  "winning_outcome": None, "ambiguity_score": 0.6}})
    n = cl.resolve_labels({}, now=_NOW + 1, settlement_fetcher=fetch)
    assert n == 1 and not cl.pending                      # terminal drop
    assert cl.metrics()["settlement_calibration_samples"] == 0
    assert not cl._settlement_completions


def test_final_settlement_without_fetcher_stays_pending(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(end_ts=_NOW - 10), _est(), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    n = cl.resolve_labels({}, now=_NOW + 1, settlement_fetcher=None)
    assert n == 0 and len(cl.pending) == 1                # never fabricated


def test_learner_observe_settlement_updates_calibration(tmp_path):
    from engine.training.online_learner import OnlineLearner
    lr = OnlineLearner()
    assert lr.observe_settlement(0.8, 1, category="crypto") is True
    assert lr.observe_settlement(0.2, 0) is True
    assert lr.live_settlement_samples == 2
    assert lr.prob_buckets                                # calibration buckets populated
    assert lr.observe_settlement("bad", 1) is False       # invalid input rejected


def test_learning_state_persists(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(), _est(), _edge(), decision="no_trade_label", reason="edge_too_low", tick=1)
    cl.persist()
    assert cl.state_saved is True
    assert (tmp_path / "training" / "learning_state.json").is_file()
    # reload picks up prior state
    cl2 = ClosedLoopLearning("run-test", tmp_path, cl.cfg, now=_NOW)
    assert cl2.state_loaded is True


# --- metrics + growth -------------------------------------------------------

def test_metrics_has_all_required_keys(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    m = cl.metrics()
    assert _REQUIRED_CLL_KEYS <= set(m.keys())
    assert m["closed_loop_enabled"] is True


def test_growth_status_collecting_then_growing(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    assert cl.growth_score()["learning_growth_status"] == "broken"   # nothing yet
    cl.record(_rec(end_ts=None), _est(), _edge(), decision="no_trade_label",
              reason="edge_too_low", tick=1, now=_NOW)
    assert cl.growth_score()["learning_growth_status"] == "collecting"
    cl.resolve_labels({"m0": 0.62}, now=_NOW + 600.0)
    assert cl.growth_score()["learning_growth_status"] == "growing"


def test_audit_classifies_stages(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(), _est(), _edge(), decision="no_trade_label", reason="edge_too_low", tick=1)
    a = cl.audit()
    assert a["stages"]["no_trade_label_recorded"] == "active_controls_learning"
    assert a["stages"]["label_resolved"] == "configured_but_zero_events"


# --- trainer integration ----------------------------------------------------

def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("max_open_trades", 5)
    return PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path,
        signal_model=FakeResearch(fair=0.55, conf=0.9))


def test_trainer_rejects_produce_learning_records(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(8)]
    for _ in range(2):
        t.run_tick(cat, now=_NOW)
    m = t.closed_loop.metrics()
    assert t.decision_count > 0
    assert m["decision_records_written"] > 0           # rejects are now learning examples
    assert m["active_learning_shadow_selected"] > 0    # NOT silently zero
    assert m["pending_labels_total"] > 0


def test_inspection_summary_includes_closed_loop(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t._begin_correlation_phase()
    s = t.inspection_summary()
    assert "closed_loop_learning" in s
    assert s["closed_loop_learning"]["closed_loop_enabled"] is True


def test_zero_trades_still_non_null_metrics(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t._begin_correlation_phase()
    pr = t.paper_realism_report()
    assert pr["realistic_pnl"] == 0.0                  # not null
    assert pr["readiness_pnl"] == 0.0
    # fill-realism posture fields present + non-null
    assert pr["reference_price_fills_allowed_for_exploit"] is False


def test_artifact_dirs_include_metrics():
    from scripts import inspection_collectors as ic
    assert "metrics" in ic.ARTIFACT_DIRS
    assert "data" in ic.ARTIFACT_DIRS


def test_write_inspection_artifacts_includes_closed_loop(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(6)]
    t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(tmp_path)
    assert (tmp_path / "metrics" / "closed_loop_learning.json").is_file()
    assert (tmp_path / "metrics" / "learning_feedback.json").is_file()
    assert (tmp_path / "reports" / "closed_loop_learning_audit.md").is_file()
    assert (tmp_path / "training" / "learning_state.json").is_file()
