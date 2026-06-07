"""Tests for absolute artifact-dir resolution + durable-write verification (P0).

Reproduces the failure where in-memory counters were positive but no durable
files existed: the verifier must flag it (run_ready_for_hours=false) and the
resolver must honor POLYMARKET_*_DIR env + create real, writable dirs. PAPER ONLY.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.training.artifact_dirs import (resolve_artifact_dirs, ensure_dirs,
                                            startup_report, verify_durable_writes,
                                            training_dir, proof_lines)


def test_resolve_defaults_relative_to_data_dir(tmp_path, monkeypatch):
    for v in ("POLYMARKET_METRICS_DIR", "POLYMARKET_REPORTS_DIR",
              "POLYMARKET_TRAINING_DATA_DIR", "POLYMARKET_EVENT_STREAM_PATH"):
        monkeypatch.delenv(v, raising=False)
    d = resolve_artifact_dirs(tmp_path)
    assert Path(d["metrics_dir"]) == (tmp_path / "metrics").resolve()
    assert Path(d["reports_dir"]) == (tmp_path / "reports").resolve()
    assert Path(d["training_data_dir"]) == (tmp_path / "training").resolve()
    assert Path(d["event_stream_path"]) == (tmp_path / "training" / "events.jsonl").resolve()


def test_resolve_honors_env_overrides(tmp_path, monkeypatch):
    m = tmp_path / "app" / "metrics"
    monkeypatch.setenv("POLYMARKET_METRICS_DIR", str(m))
    monkeypatch.setenv("POLYMARKET_TRAINING_DATA_DIR", str(tmp_path / "app" / "data" / "training"))
    d = resolve_artifact_dirs(tmp_path)
    assert Path(d["metrics_dir"]) == m.resolve()
    assert Path(d["training_data_dir"]) == (tmp_path / "app" / "data" / "training").resolve()
    # closed-loop training dir resolver agrees with the env override
    assert training_dir(tmp_path) == (tmp_path / "app" / "data" / "training").resolve()


def test_ensure_dirs_creates_real_writable_dirs(tmp_path, monkeypatch):
    for v in ("POLYMARKET_METRICS_DIR", "POLYMARKET_REPORTS_DIR",
              "POLYMARKET_TRAINING_DATA_DIR", "POLYMARKET_EVENT_STREAM_PATH"):
        monkeypatch.delenv(v, raising=False)
    d = resolve_artifact_dirs(tmp_path)
    ensure_dirs(d)
    for k in ("metrics_dir", "reports_dir", "training_data_dir"):
        assert Path(d[k]).is_dir()
    rep = startup_report(d)
    assert "exists=true writable=true" in rep


def test_verify_fails_when_counters_positive_but_files_missing(tmp_path, monkeypatch):
    for v in ("POLYMARKET_METRICS_DIR", "POLYMARKET_REPORTS_DIR",
              "POLYMARKET_TRAINING_DATA_DIR", "POLYMARKET_EVENT_STREAM_PATH"):
        monkeypatch.delenv(v, raising=False)
    d = resolve_artifact_dirs(tmp_path)
    ensure_dirs(d)
    # decision_count=480 but NO event files written -> not run-ready (the exact bug)
    res = verify_durable_writes(d, decision_count=480, pending_count=480)
    assert res["ok"] is False
    assert "durable_artifact_files_not_written" in res["blocking_reasons"] \
        or "durable_event_files_not_written" in res["blocking_reasons"]


def test_verify_passes_when_files_written_with_rows(tmp_path, monkeypatch):
    for v in ("POLYMARKET_METRICS_DIR", "POLYMARKET_REPORTS_DIR",
              "POLYMARKET_TRAINING_DATA_DIR", "POLYMARKET_EVENT_STREAM_PATH"):
        monkeypatch.delenv(v, raising=False)
    d = resolve_artifact_dirs(tmp_path)
    ensure_dirs(d)
    td = Path(d["training_data_dir"])
    md = Path(d["metrics_dir"])
    for f in ("decision_records.jsonl",):
        (td / f).write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    Path(d["event_stream_path"]).write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    (td / "pending_labels.jsonl").write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    (td / "learning_state.json").write_text("{}", encoding="utf-8")
    for f in ("inspection_summary.json", "training_reconciliation.json", "run_ready.json"):
        (md / f).write_text("{}", encoding="utf-8")
    res = verify_durable_writes(d, decision_count=480, pending_count=480)
    assert res["ok"] is True, res
    # proof lines report absolute paths with exists + rows
    lines = "\n".join(proof_lines(d))
    assert "exists=true" in lines and "rows=1" in lines


def test_end_to_end_trainer_writes_real_files_to_resolved_dirs(tmp_path, monkeypatch):
    # the trainer + entrypoint path: write to resolved metrics/reports dirs and the
    # closed-loop sink writes to the resolved training dir (all real, non-empty).
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env, market, FakeResearch
    for v in ("POLYMARKET_METRICS_DIR", "POLYMARKET_REPORTS_DIR",
              "POLYMARKET_TRAINING_DATA_DIR", "POLYMARKET_EVENT_STREAM_PATH"):
        monkeypatch.delenv(v, raising=False)
    clean_live_env(monkeypatch, tmp_path)
    d = resolve_artifact_dirs(tmp_path)
    ensure_dirs(d)
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", min_net_edge=0.5, trade_candidate_limit=20,
                       shortlist_limit=20),
        data_dir=tmp_path, signal_model=FakeResearch(fair=0.55, conf=0.9))
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=1_000_000.0)
           for i in range(15)]
    for _ in range(2):
        t.run_tick(cat, now=1_000_000.0)
    t.write_inspection_artifacts(tmp_path, metrics_dir=Path(d["metrics_dir"]),
                                 reports_dir=Path(d["reports_dir"]))
    assert (Path(d["metrics_dir"]) / "inspection_summary.json").stat().st_size > 0
    assert (Path(d["reports_dir"]) / "paper_training_inspection.md").stat().st_size > 0
    assert Path(d["event_stream_path"]).stat().st_size > 0
    assert (Path(d["training_data_dir"]) / "decision_records.jsonl").stat().st_size > 0
    res = verify_durable_writes(d, decision_count=int(t.decision_count))
    assert res["ok"] is True, res
