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
    # FULL (forensic) mode bundles the complete JSONL event files.
    res = gen.generate_report(output_dir=str(out), repo_root=str(tmp_path),
                              data_dir=str(tmp_path), skip_tests=True, bundle_mode="full",
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


# --- TASK 8: reproduce the EXACT broken command output ----------------------

def test_report_with_synthesized_artifacts_is_fail_not_run_ready(tmp_path):
    """Simulates `generate_bot_inspection_report.py` with the reported failure:
    healthy-looking status (decision_count>0, Bregman enabled, groups scanned=0)
    but NO real closed-loop artifacts on disk -> 23 synthesized placeholders.
    Must become FAIL_NOT_RUN_READY, run_ready_for_hours=false, with explicit
    blocking reasons (NOT PASS_WITH_WARNINGS)."""
    import json as _json
    import scripts.generate_bot_inspection_report as gen
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "mode": "paper_train", "runtime_seconds": 240, "decisions": 900,
        "pnl": {"decision_count": 900, "equity": 500.0, "trades_closed": 0,
                "win_rate": None},
        "scan_metrics": {"scanned": 900, "kept": 80, "groups_detected": 259},
        "closed_loop_learning": {"closed_loop_enabled": True,
                                 "decision_records_written": 900,
                                 "no_trade_labels_written": 900,
                                 "pending_labels_total": 900,
                                 "learning_growth_status": "collecting"},
        "bregman": {"execution": {"bregman_paper_enabled": True,
                                  "constraint_groups_scanned": 0,
                                  "groups_discovered": 0}},
        "ledger": {"decisions": 0},
        "safety": {"ok": True, "live_detected": False},
    }
    (data_dir / "polymarket_training.json").write_text(_json.dumps(status), encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(data_dir))
    assert res["classification"] == "FAIL_NOT_RUN_READY"
    assert res["run_ready_for_hours"] is False
    blockers = " ".join(res["run_ready"]["blocking_reasons"]).lower()
    # explicit blocking reasons cover the failure modes
    assert "synthesized" in blockers or "missing" in blockers or "empty" in blockers
    assert ("reconcil" in blockers) or ("ledger" in blockers) or ("bregman" in blockers) \
        or ("edge audit" in blockers)
    # synthesized placeholders are flagged not-valid-for-run-ready
    man = res["closed_loop_artifacts_manifest"]
    assert man["hard_required_satisfied"] is False


# --- active-learning config_mismatch truth-chain gate -----------------------

def test_run_ready_blocks_on_active_learning_config_mismatch():
    """Aggressive profile declared but active learning effectively OFF (stale container)
    => run-ready FAILS with an exact config_mismatch blocker (no silent degraded run)."""
    import scripts.generate_bot_inspection_report as gen
    manifest = {"artifacts": [], "synthesized_empty": [], "empty_real_files": [],
                "hard_required_invalid": []}
    status = {"active_learning": {
        "active_learning_config_source": "aggressive_paper_profile",
        "active_learning_runtime_enabled": False,
        "active_learning_enabled": False,
        "active_learning_config_mismatch": True}}
    rr = gen.build_report_run_ready(manifest, status, {"ok": True}, {})
    assert rr["run_ready_for_hours"] is False
    assert any("config_mismatch" in b for b in rr["blocking_reasons"])
    assert rr["proof"]["active_learning_config_consistent"] is False


def test_run_ready_no_mismatch_when_active_learning_on():
    """Aggressive profile declared AND active learning effectively ON => consistent."""
    import scripts.generate_bot_inspection_report as gen
    manifest = {"artifacts": [], "synthesized_empty": [], "empty_real_files": [],
                "hard_required_invalid": []}
    status = {"active_learning": {
        "active_learning_config_source": "aggressive_paper_profile",
        "active_learning_runtime_enabled": True,
        "active_learning_enabled": True}}
    rr = gen.build_report_run_ready(manifest, status, {"ok": True}, {})
    assert rr["proof"]["active_learning_config_consistent"] is True
    assert not any("config_mismatch" in b for b in rr["blocking_reasons"])


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


# --- Bregman source-of-truth unification (canonical funnel > legacy scanner) ---

def _write(p, obj):
    import json as _j
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_j.dumps(obj) if isinstance(obj, (dict, list)) else str(obj),
                 encoding="utf-8")


def _runtime_dir_with_canonical_funnel(tmp_path, monkeypatch):
    """Build a REAL runtime data dir (all durable artifacts written by the trainer),
    then overlay the exact reported Bregman case: canonical funnel scanned=272,
    certified=0, while the legacy metrics/bregman.json scanned=0."""
    dd = _run(tmp_path, monkeypatch).data_dir  # real trainer run writes everything
    t = _trainer(tmp_path, monkeypatch, signal=True, min_net_edge=0.5)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(2):
        t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(dd)
    md = Path(dd) / "metrics"
    funnel = {"raw_catalog_markets_scanned": 864, "eligible_raw_markets": 864,
              "raw_groups_discovered": 272, "groups_adapter_success": 272,
              "groups_adapter_failed": 0, "groups_sent_to_certifier": 272,
              "constraint_groups_scanned": 272, "candidates_generated": 0,
              "certified": 0, "certified_opportunities": 0, "realistic_executable": 0,
              "bundles_opened": 0, "internally_consistent": True,
              "rejected_by_reason": {"depth_too_thin": 5669, "spread_too_wide": 37,
                                     "stale_book": 153, "no_positive_edge": 74}}
    _write(md / "bregman_funnel.json", funnel)
    _write(md / "bregman.json", {"source": "legacy_abcas_scanner_telemetry",
                                 "constraint_groups_scanned": 0, "groups_skipped": 1129})
    # patch the status file's bregman_funnel + legacy bregman to the reported case
    st = json.loads((Path(dd) / "polymarket_training.json").read_text())
    st["bregman_funnel"] = funnel
    st["bregman"] = {"execution": {"constraint_groups_scanned": 0, "groups_skipped": 1129,
                                   "bregman_paper_enabled": True}}
    st.setdefault("scan_metrics", {})["groups_detected"] = 272
    _write(Path(dd) / "polymarket_training.json", st)
    return Path(dd)


def test_canonical_funnel_overrides_legacy_zero_scan(tmp_path, monkeypatch):
    """The exact reported case: funnel scanned=272 but legacy bregman=0 must NOT
    produce bregman_zero_groups_scanned; classification != FAIL_NOT_RUN_READY."""
    import scripts.generate_bot_inspection_report as gen
    dd = _runtime_dir_with_canonical_funnel(tmp_path, monkeypatch)
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False, data_dir=str(dd))
    rj = json.loads(Path(res["report_json"]).read_text())
    audit = rj["algorithmic_edge_audit"]
    assert "bregman_zero_groups_scanned" not in audit["hard_failures"], audit["hard_failures"]
    assert audit["sections"]["bregman"]["constraint_groups_scanned"] == 272
    assert res["classification"] != "FAIL_NOT_RUN_READY"
    assert res["classification"] in ("PASS_RUN_READY", "PASS_WITH_WARNINGS")
    # run_ready and classification agree
    assert res["run_ready_for_hours"] is True
    # source reconciliation + warning surfaced
    recon = json.loads((Path(res["bundle_dir"]) / "metrics"
                        / "bregman_source_reconciliation.json").read_text())
    assert recon["canonical_constraint_groups_scanned"] == 272
    assert recon["legacy_constraint_groups_scanned"] == 0
    assert recon["sources_disagree"] is True
    assert recon["warning"] == "legacy_bregman_scanner_zero_but_canonical_funnel_active"
    warns = " ".join(rj["warnings"])
    assert "legacy_bregman_scanner_zero_but_canonical_funnel_active" in warns
    assert "certified" in warns.lower()


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


# --- freshness-window: stale decision/label tails fail run-readiness ----------

def _rewrite_tail_timestamps(path: Path, *, offset_sec: float) -> None:
    """Shift every row's timestamp BACK by ``offset_sec`` (simulating a dedicated
    stream that stopped advancing while events.jsonl stayed fresh)."""
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for r in rows:
        for k in ("timestamp", "ts", "created_at", "label_due_at"):
            if isinstance(r.get(k), (int, float)):
                r[k] = r[k] - offset_sec
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _stale_decision_runtime(tmp_path, monkeypatch, offset_sec=45_000.0) -> Path:
    """Real runtime dir where events.jsonl is fresh but decision_records/no_trade/
    pending tails are ~12.6h behind (same run_id) — the exact reported failure."""
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    train = Path(tmp_path) / "training"
    for name in ("decision_records.jsonl", "no_trade_labels.jsonl", "pending_labels.jsonl"):
        p = train / name
        if p.exists() and p.read_text(encoding="utf-8").strip():
            _rewrite_tail_timestamps(p, offset_sec=offset_sec)
    return Path(tmp_path)


def test_stale_decision_tail_fails_validate_runtime(tmp_path, monkeypatch):
    dd = _stale_decision_runtime(tmp_path, monkeypatch)
    import time
    v = validate_runtime(_load_status_json(dd), data_dir=str(dd), status_mtime=time.time())
    assert v["safe_to_run"] is False
    assert "training_tail_streams_fresh_window" in v["blocking"], v["blocking"]


def test_stale_decision_tail_blocks_report_run_ready(tmp_path, monkeypatch):
    import scripts.generate_bot_inspection_report as gen
    dd = _stale_decision_runtime(tmp_path, monkeypatch)
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False, data_dir=str(dd))
    assert res["run_ready_for_hours"] is False
    manifest = json.loads((Path(res["bundle_dir"]) / "metrics"
                           / "closed_loop_artifacts_manifest.json").read_text())
    assert manifest["stale_or_mixed_training_tail_samples"] is True
    fresh = manifest["tail_freshness"]
    assert fresh["compatible"] is False
    assert "decision_records.jsonl" in fresh["incompatible_streams"]
    # event_file_stats proves the selected source + freshness metadata per file
    stats = json.loads((Path(res["bundle_dir"]) / "samples"
                        / "event_file_stats.json").read_text())
    assert stats["source_strict"] is True
    by_name = {f["logical_name"]: f for f in stats["files"]}
    assert by_name["decision_records.jsonl"]["selected_absolute_source"] is not None
    assert by_name["decision_records.jsonl"]["last_tail_timestamp"] is not None


def test_fresh_run_freshness_window_compatible(tmp_path, monkeypatch):
    import scripts.generate_bot_inspection_report as gen
    t = _run(tmp_path, monkeypatch)
    t.write_inspection_artifacts(tmp_path)
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False, data_dir=str(tmp_path))
    manifest = json.loads((Path(res["bundle_dir"]) / "metrics"
                           / "closed_loop_artifacts_manifest.json").read_text())
    assert manifest["tail_freshness"]["compatible"] is True
    assert manifest["stale_or_mixed_training_tail_samples"] is False


def _load_status_json(dd: Path) -> dict:
    p = Path(dd) / "polymarket_training.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
