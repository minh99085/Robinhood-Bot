"""P0 run-readiness contract regression tests.

These reproduce the "healthy status counters but empty/missing durable artifacts"
failure state and assert it is now caught: durable event files are the source of
truth, the canonical ledger records non-trade decisions, the 4-surface
reconciliation + run-ready gate fail when artifacts are missing, Bregman skipped
groups become diagnostics, audit false/zero/null-with-sample-count is not
"missing", Grok zero-call ambiguity is explained, and the inspection zip bundles
the required event files. PAPER ONLY.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.closed_loop import ClosedLoopLearning
from engine.training.inspection_summary import (build_run_ready, build_grok_news_evidence,
                                                 build_bregman_funnel)
from scripts.validate_training_runtime import validate_runtime

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch

_NOW = 1_000_000.0


def _trainer(tmp_path, monkeypatch, signal=True, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("trade_candidate_limit", 20)
    cfg.setdefault("shortlist_limit", 20)
    sm = FakeResearch(fair=0.55, conf=0.9) if signal else None
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg),
                                  data_dir=tmp_path, signal_model=sm)


def _run(tmp_path, monkeypatch, ticks=3, **cfg):
    t = _trainer(tmp_path, monkeypatch, min_net_edge=0.5, **cfg)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(ticks):
        t.run_tick(cat, now=_NOW)
    return t


# --- 1. status counters without event files FAIL validation -----------------

def test_status_counters_without_event_files_fail_validation():
    # the exact broken report: decision_records_written=900 but no files on disk.
    status = {
        "mode": "paper_train", "decisions": 900,
        "closed_loop_learning": {"closed_loop_enabled": True,
                                 "decision_records_written": 900,
                                 "no_trade_labels_written": 900,
                                 "pending_labels_total": 900,
                                 "candidate_evaluated_events": 900,
                                 "pending_labels_created": 900,
                                 "learning_growth_status": "collecting"},
        "training_reconciliation": {"decision_count_counter": 900, "reconciled": True,
                                    "decision_events": 900},
        "ledger": {"decisions": 0},
        "pnl": {"decision_count": 900},
    }
    import tempfile
    empty = tempfile.mkdtemp()   # no data/training files written here
    v = validate_runtime(status, data_dir=empty)
    assert v["safe_to_run"] is False
    # the durable-file checks must be among the blockers
    assert any("event_file" in b or "matches_file" in b or "artifacts_present" in b
               or "ledger" in b for b in v["blocking"]), v["blocking"]


# --- 2. decision_count without ledger decisions fails validation ------------

def test_decision_count_without_ledger_decisions_fails_validation(tmp_path):
    status = {"mode": "paper_train", "decisions": 50,
              "training_reconciliation": {"decision_count_counter": 50, "reconciled": True,
                                          "decision_events": 50},
              "closed_loop_learning": {"closed_loop_enabled": True,
                                       "decision_records_written": 50},
              "ledger": {"decisions": 0}}
    v = validate_runtime(status)
    assert "ledger_records_decisions" in v["blocking"]


# --- 3. pending labels counter without file fails validation ----------------

def test_pending_labels_counter_without_pending_labels_file_fails_validation():
    import tempfile
    dd = tempfile.mkdtemp()
    status = {"mode": "paper_train", "decisions": 10,
              "closed_loop_learning": {"closed_loop_enabled": True,
                                       "decision_records_written": 10,
                                       "pending_labels_total": 10}}
    v = validate_runtime(status, data_dir=dd)
    assert "pending_labels_counter_matches_file" in v["blocking"]


# --- 4. bregman groups detected but zero scanned requires diagnostics -------

def test_bregman_groups_detected_but_zero_scanned_requires_adapter_diagnostics():
    # detected>0, adapter produced nothing, and NO adapter failures recorded ->
    # the funnel is internally inconsistent (silent zero) and is flagged.
    funnel = build_bregman_funnel(
        {"groups_discovered": 0, "constraint_groups_scanned": 0, "groups_skipped": 0},
        market_groups_detected=259, diagnostic_events_written=0)
    assert funnel["internally_consistent"] is False
    # with adapter failures recorded, it reconciles
    funnel2 = build_bregman_funnel(
        {"groups_discovered": 0, "constraint_groups_scanned": 0, "groups_skipped": 259,
         "skip_reasons": {"non_numeric_price": 259}},
        market_groups_detected=259, diagnostic_events_written=259)
    assert funnel2["internally_consistent"] is True
    assert funnel2["groups_adapter_failed"] == 259


def test_bregman_adapter_parses_dollar_and_percent_prices():
    from engine.arbitrage.constraint_graph import _to_float
    assert _to_float("$0.42") == pytest.approx(0.42)
    assert _to_float("42%") == pytest.approx(0.42)
    assert _to_float("42.0%") == pytest.approx(0.42)
    assert _to_float("0.41") == pytest.approx(0.41)
    assert _to_float("1,234.5") == pytest.approx(1234.5)
    assert _to_float("not-a-price") is None


# --- 5. audit false/zero/null-with-sample-count is NOT missing --------------

def test_audit_false_field_is_not_missing(tmp_path, monkeypatch):
    import scripts.inspection_metrics as im
    t = _run(tmp_path, monkeypatch)
    st = t.status()
    feats = im.extract_features(st, {}, {"skipped": True}, {})
    audit = im.build_algorithmic_edge_audit(feats, st, scorecard={"score": 0})
    v = audit["required_field_violations"]
    assert "strategy_attribution.win_rate" not in v
    assert "execution.clob_v2_executable" not in v
    assert "training_readiness.production_readiness_score" not in v
    sa = audit["sections"]["strategy_attribution"]
    assert sa["win_rate"] == 0.0 and sa["win_rate_sample_count"] == 0
    assert audit["sections"]["execution"]["clob_v2_executable"] is False


# --- 6. grok enabled, zero calls -> reason present --------------------------

def test_grok_enabled_zero_calls_requires_reason():
    ev = build_grok_news_evidence(
        {"grok_enabled": True, "grok_has_api_key": True, "research_mode": "offline_cache",
         "grok_calls_total": 0}, news_items_used=315)
    assert ev["grok_calls_total"] == 0
    assert ev["grok_zero_call_reason"]   # non-empty
    # if calls happened, no zero reason
    ev2 = build_grok_news_evidence({"grok_enabled": True, "grok_calls_total": 5})
    assert ev2["grok_zero_call_reason"] is None


# --- 7. inspection zip contains the training event files --------------------

def test_inspection_zip_requires_training_event_files(tmp_path, monkeypatch):
    import scripts.generate_bot_inspection_report as gen
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    (tmp_path / "polymarket_training.json").write_text(
        json.dumps(t.status(), default=str), encoding="utf-8")
    out = tmp_path / "out"
    res = gen.generate_report(output_dir=str(out), repo_root=str(tmp_path),
                              data_dir=str(tmp_path), skip_tests=True,
                              skip_docker=True, skip_api=True, skip_artifacts=True)
    zf = zipfile.ZipFile(res["zip_path"])
    names = zf.namelist()
    for rel in ("data/training/events.jsonl", "data/training/decision_records.jsonl",
                "data/training/no_trade_labels.jsonl", "data/training/pending_labels.jsonl",
                "metrics/training_reconciliation.json", "metrics/run_ready.json",
                "metrics/inspection_summary.json"):
        assert any(n.endswith(rel) for n in names), f"{rel} missing from zip"
    # decision_records non-empty in the zip
    dr = [n for n in names if n.endswith("data/training/decision_records.jsonl")][0]
    assert len(zf.read(dr)) > 0


# --- 8. run_ready false when reconciliation missing -------------------------

def test_run_ready_false_when_reconciliation_missing():
    rr = build_run_ready(
        reconciliation={}, ledger={"decisions": 100}, bregman_funnel={},
        missing_event_files=[], missing_report_files=[],
        live_trading_disabled=True, decision_count=100, bregman_enabled=False)
    assert rr["run_ready_for_hours"] is False
    assert rr["max_safe_runtime_minutes"] == 10
    # missing durable files also blocks
    rr2 = build_run_ready(
        reconciliation={"reconciled": True, "decision_events": 100},
        ledger={"decisions": 100}, bregman_funnel={},
        missing_event_files=["events.jsonl"], missing_report_files=[],
        live_trading_disabled=True, decision_count=100, bregman_enabled=False)
    assert rr2["run_ready_for_hours"] is False


# --- end-to-end: a healthy run is run-ready ---------------------------------

def test_healthy_run_is_run_ready_and_ledger_records_decisions(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    import time
    v = validate_runtime(t.status(), data_dir=str(tmp_path), status_mtime=time.time())
    assert v["safe_to_run"] is True, v["blocking"]
    led = t.closed_loop.ledger_summary()
    assert led["decisions"] > 0 and led["trades"] == 0
    rr = t.status()["run_ready"]
    assert rr["run_ready_for_hours"] is True
