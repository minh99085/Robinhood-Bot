"""P0 canonical training event stream regression tests.

These reproduce the live failure (decision_count>0 but decision_records=0) and
assert the event-sourcing invariants: every evaluated candidate emits a durable
event + a learning object (no-trade/shadow/diagnostic), counters reconcile with
the event stream, the ledger records non-trade decisions, Bregman adapter
failures become diagnostics, and zero-trade runs keep non-null core metrics.
PAPER ONLY.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.closed_loop import ClosedLoopLearning, classify_rejection

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch

_NOW = 1_000_000.0


def _cll(tmp_path, **cfg):
    return ClosedLoopLearning("run-x", tmp_path, TrainingConfig(mode="paper_train", **cfg),
                              now=_NOW)


def _rec(mid="m0", *, end_ts=None):
    return SimpleNamespace(market_id=mid, group_key=f"market:{mid}", cluster_id="sem:x",
                           category="crypto", question="Will X?", top_depth_usd=2000.0,
                           book_age_s=2.0, end_ts=end_ts, raw={"conditionId": "c0"})


def _est():
    return SimpleNamespace(p_market_mid=0.5, spread=0.02, ambiguity_score=0.05,
                           calibrated_probability=None)


def _edge(net=0.005, px=0.51):
    return SimpleNamespace(net_edge=net, executable_price=px, p_final=0.55)


# --- 1. rejected candidate emits a training event ---------------------------

def test_rejected_candidate_emits_training_event(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    r = cl.record(_rec(end_ts=_NOW + 86400), _est(), _edge(net=-0.02),
                  decision="rejected_hard_gate", reason="negative_after_cost", tick=1, now=_NOW)
    assert r is not None
    assert cl.counts["decision_records_written"] == 1
    assert cl.counts["no_trade_labels_written"] == 1     # economic reject -> no-trade label
    assert cl.counts["pending_labels_created"] == 1      # has end_ts -> pending label
    # durable files written
    assert (tmp_path / "training" / "events.jsonl").exists() or cl.counts["events_written"] == 1


# --- 2. rejection counter reconciles with event stream ----------------------

def test_rejection_counter_reconciles_with_event_stream(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    for i in range(5):
        cl.record(_rec(f"m{i}"), _est(), _edge(net=-0.02), decision="rejected_hard_gate",
                  reason="negative_after_cost", tick=1, now=_NOW)
    rec = cl.reconcile(decision_count=5, rejection_count=5, candidate_evaluated=5)
    assert rec["reconciled"] is True
    assert rec["decision_events_written"] == 5
    # a diverged counter is flagged
    bad = cl.reconcile(decision_count=99, rejection_count=99, candidate_evaluated=99)
    assert bad["reconciled"] is False and bad["missing_event_callsite"]


# --- 5. bregman adapter failure -> diagnostic -------------------------------

def test_bregman_adapter_failure_writes_diagnostic(tmp_path):
    cl = _cll(tmp_path)
    cl.begin_tick()
    cl.record(_rec(), _est(), _edge(), decision="rejected_hard_gate",
              reason="bregman_non_numeric_price", tick=1, now=_NOW)
    assert cl.counts["diagnostic_records_written"] == 1
    assert cl.counts["diagnostic_without_label_target"] == 1
    assert classify_rejection("bregman_non_numeric_price") == "bregman_diagnostic"
    assert classify_rejection("malformed_group") == "diagnostic"
    assert classify_rejection("negative_after_cost") == "no_trade_label"
    assert classify_rejection("missing_ask") == "shadow_label"


# --- trainer integration: the exact live failure (no signal) ----------------

def _trainer(tmp_path, monkeypatch, signal=False, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("trade_candidate_limit", 20)
    cfg.setdefault("shortlist_limit", 20)
    sm = FakeResearch(fair=0.55, conf=0.9) if signal else None
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg),
                                  data_dir=tmp_path, signal_model=sm)


def test_no_signal_run_still_emits_events_and_reconciles(tmp_path, monkeypatch):
    # reproduces the live failure: candidates rejected as offline_stub (no model
    # probability). Previously decision_records=0; now every decision emits a
    # diagnostic event and the counters reconcile.
    t = _trainer(tmp_path, monkeypatch)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(2):
        t.run_tick(cat, now=_NOW)
    m = t.closed_loop.metrics()
    assert t.decision_count > 0
    assert m["decision_records_written"] == t.decision_count   # NOT zero
    rec = t.closed_loop.reconcile(decision_count=t.decision_count,
                                  rejection_count=t.rejection_count)
    assert rec["reconciled"] is True


def test_economic_reject_run_creates_no_trade_labels_and_pending(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, signal=True, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(2):
        t.run_tick(cat, now=_NOW)
    m = t.closed_loop.metrics()
    assert m["no_trade_labels_written"] > 0
    assert m["pending_labels_total"] > 0
    assert m["active_learning_shadow_selected"] > 0


# --- 6. zero-trade run -> non-null core metrics + validation passes ---------

def test_zero_trade_run_validation_passes_with_learning(tmp_path, monkeypatch):
    import time
    from scripts.validate_training_runtime import validate_runtime
    t = _trainer(tmp_path, monkeypatch, signal=True, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(2):
        t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(tmp_path)
    v = validate_runtime(t.status(), data_dir=str(tmp_path), status_mtime=time.time())
    assert v["safe_to_run"] is True, v["blocking"]


# --- 7. inspection artifacts incl. event files ------------------------------

def test_inspection_artifacts_include_event_files(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, signal=True, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(10)]
    t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(tmp_path)
    for f in ("training/events.jsonl", "training/decision_records.jsonl",
              "training/no_trade_labels.jsonl", "training/shadow_labels.jsonl",
              "training/diagnostics.jsonl", "training/pending_labels.jsonl",
              "training/completed_labels.jsonl", "training/learning_state.json",
              "metrics/training_reconciliation.json"):
        assert (tmp_path / f).is_file(), f


def test_reconciliation_metric_written(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, signal=True, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(10)]
    t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(tmp_path)
    rec = json.loads((tmp_path / "metrics" / "training_reconciliation.json").read_text())
    for k in ("decision_count_counter", "decision_events_written", "rejection_count_counter",
              "rejection_events_written", "candidate_evaluated_counter",
              "candidate_events_written", "reconciled", "divergence_reason",
              "missing_event_callsite"):
        assert k in rec
    assert rec["reconciled"] is True
