"""P0 runtime-wiring repair: paper_training_running detection, non-null core audit
fields, active-learning selection visibility, closed-loop artifact emission, and
the pre-long-run validation gate. PAPER ONLY."""

from __future__ import annotations

from pathlib import Path

from engine.training import PolymarketPaperTrainer, TrainingConfig

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch

import scripts.inspection_metrics as im
from scripts.validate_training_runtime import REQUIRED_ARTIFACTS, validate_runtime

_NOW = 1_000_000.0


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("min_net_edge", 0.5)        # reject ~all -> exercise no-trade learning
    cfg.setdefault("trade_candidate_limit", 20)
    cfg.setdefault("shortlist_limit", 20)
    return PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path,
        signal_model=FakeResearch(fair=0.55, conf=0.9))


def _run(tmp_path, monkeypatch, ticks=2, n=20):
    t = _trainer(tmp_path, monkeypatch)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(n)]
    for _ in range(ticks):
        t.run_tick(cat, now=_NOW)
    return t


# --- paper_training_running (mode=paper_train must read true) ---------------

def test_paper_training_running_true_for_paper_train(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch, ticks=1)
    feats = im.extract_features(t.status(), {}, {})
    assert feats["paper_training_running"] is True


def test_paper_training_running_none_without_status():
    feats = im.extract_features({}, {}, {})
    assert feats["paper_training_running"] in (None, False)


# --- core audit fields non-null ---------------------------------------------

def test_core_audit_fields_non_null_with_zero_trades(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch)
    feats = im.extract_features(t.status(), {}, {})
    assert feats["after_cost_pnl"] is not None and feats["after_cost_pnl"] == 0.0
    assert feats["after_cost_pnl_sample_count"] == 0
    assert feats["fill_realism_enabled"] is True
    assert feats["fantasy_fill_rejections"] is not None
    assert feats["clob_v2_executable"] is not None
    assert feats["fill_realism_rejection_rate"] is not None
    # zero-trade win rate is null ONLY with an explicit zero sample count
    assert feats["win_rate_traded_only"] is None and feats["win_rate_sample_count"] == 0


def test_closed_loop_metrics_surfaced_non_null(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch)
    feats = im.extract_features(t.status(), {}, {})
    assert feats["closed_loop_enabled"] is True
    assert feats["decision_records_written"] > 0
    assert feats["active_learning_shadow_selected"] > 0
    assert feats["learning_growth_status"] in ("collecting", "growing")


def test_grok_evidence_metrics_non_null(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch, ticks=1)
    feats = im.extract_features(t.status(), {}, {})
    for k in ("grok_calls_total", "grok_calls_with_news", "grok_advisory_only_count",
              "grok_evidence_records_written", "grok_with_news_count"):
        assert feats[k] is not None


# --- active learning selection visibility -----------------------------------

def test_active_learning_report_selected_includes_shadow(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch)
    al = t.active_learning_report()
    # considered > 0 and zero trades -> selected must be > 0 (shadow learning)
    assert al["active_learning_candidates_considered"] > 0
    assert al["active_learning_candidates_selected"] > 0
    assert al["active_learning_shadow_selected"] > 0


# --- artifact emission ------------------------------------------------------

def test_write_inspection_artifacts_emits_full_set(tmp_path, monkeypatch):
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    for name in ("metrics/inspection_summary.json", "metrics/closed_loop_learning.json",
                 "metrics/learning_feedback.json", "metrics/active_learning.json",
                 "metrics/paper_realism.json", "metrics/bregman_execution.json",
                 "metrics/strategy_priority.json", "metrics/profitability_ranking.json",
                 "metrics/correlation_risk.json", "reports/paper_training_inspection.md",
                 "reports/closed_loop_learning_audit.md"):
        assert (tmp_path / name).is_file(), name
    assert (tmp_path / "training" / "learning_state.json").is_file()
    assert (tmp_path / "training" / "pending_labels.jsonl").is_file()


# --- runtime validation gate ------------------------------------------------

def test_validation_blocks_empty_status():
    v = validate_runtime({}, data_dir=None)
    assert v["safe_to_run"] is False
    assert "paper_training_running" in v["blocking"]


def test_validation_passes_after_real_run(tmp_path, monkeypatch):
    import time
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    v = validate_runtime(t.status(), data_dir=str(tmp_path), status_mtime=time.time())
    assert v["safe_to_run"] is True, v["blocking"]


def test_validation_detects_bregman_inconsistency():
    status = {"mode": "paper_train", "paper_realism": {"realistic_pnl": 0.0},
              "closed_loop_learning": {"closed_loop_enabled": True,
                                       "decision_records_written": 5,
                                       "no_trade_labels_written": 5,
                                       "pending_labels_created": 5, "pending_labels_total": 5,
                                       "learning_state_saved": True},
              "active_learning": {"active_learning_candidates_considered": 5},
              "bregman": {"execution": {"raw_groups_discovered": 0,
                                        "certified_opportunities": 3}}}  # inconsistent
    v = validate_runtime(status, data_dir=None)
    assert "bregman_metrics_consistent" in v["blocking"]


def test_required_artifacts_list_complete():
    for a in ("metrics/inspection_summary.json", "metrics/closed_loop_learning.json",
              "metrics/learning_feedback.json", "data/training/learning_state.json"):
        assert a in REQUIRED_ARTIFACTS
