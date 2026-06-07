"""Inspection bundle modes: light (default, sampled) vs full (forensic).

Light mode keeps the zip small by tail-sampling the large JSONL event streams
(+ event_file_stats.json) instead of copying them in full, while still being
run-ready when the SOURCE files are verified in the runtime data dir. Full mode
includes the complete JSONL. Durable /data event files are never affected.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import scripts.generate_bot_inspection_report as gen
from engine.training import PolymarketPaperTrainer, TrainingConfig

from tests._pmtrain_helpers import clean_live_env, market, FakeResearch

_NOW = 1_000_000.0


def _runtime_dir(tmp_path, monkeypatch, big_rows=2000):
    """Build a real runtime data dir (all durable artifacts), with a bloated
    events.jsonl so the full-vs-light size difference is meaningful."""
    clean_live_env(monkeypatch, tmp_path)
    dd = tmp_path / "runtime_data"
    dd.mkdir(parents=True, exist_ok=True)
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", min_net_edge=0.5, trade_candidate_limit=20,
                       shortlist_limit=20),
        data_dir=dd, signal_model=FakeResearch(fair=0.55, conf=0.9))
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=_NOW) for i in range(15)]
    for _ in range(3):
        t.run_tick(cat, now=_NOW)
    t.write_inspection_artifacts(dd)
    ev = dd / "training" / "events.jsonl"
    rid = t.run_id                                 # realistic: events carry the run_id
    with ev.open("a", encoding="utf-8") as fh:
        for i in range(big_rows):
            fh.write(json.dumps({"event_type": "decision", "run_id": rid, "tick": 99,
                                 "timestamp": _NOW + i, "i": i, "blob": f"row-{i}"}) + "\n")
    (dd / "polymarket_training.json").write_text(json.dumps(t.status(), default=str),
                                                 encoding="utf-8")
    return dd


def _report(tmp_path, dd, **kw):
    out = tmp_path / "out"
    return gen.generate_report(
        output_dir=str(out), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(dd), **kw)


def _zip_names(res):
    return zipfile.ZipFile(res["zip_path"]).namelist()


def _has(names, suffix):
    return any(n.endswith(suffix) for n in names)


# 1. light omits the full events.jsonl
def test_light_bundle_omits_full_events_jsonl(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="light")
    names = _zip_names(res)
    assert not _has(names, "data/training/events.jsonl")
    assert not _has(names, "data/training/decision_records.jsonl")


# 2. light includes the tail sample
def test_light_bundle_includes_tail_sample(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="light")
    names = _zip_names(res)
    assert _has(names, "samples/events_tail_500.jsonl")
    assert _has(names, "samples/decision_records_tail_500.jsonl")


# 3. light includes event_file_stats.json with the required fields
def test_light_bundle_includes_event_file_stats(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="light")
    bundle = Path(res["bundle_dir"])
    stats_p = bundle / "samples" / "event_file_stats.json"
    assert stats_p.is_file()
    stats = json.loads(stats_p.read_text())
    ev = next(f for f in stats["files"] if f["source_path"].endswith("events.jsonl"))
    for k in ("logical_name", "source_path", "selected_absolute_source", "selected_root",
              "bundle_sample_path", "exists", "size_bytes", "mtime",
              "total_rows_exact_if_available", "included_rows", "first_tail_timestamp",
              "last_tail_timestamp", "first_tail_run_id", "last_tail_run_id",
              "run_ids_seen", "first_tail_tick", "last_tail_tick",
              "source_sha256_head_tail", "truncated", "source_verified_from_data_dir",
              "fallback_used"):
        assert k in ev, k
    assert ev["exists"] is True
    assert ev["included_rows"] <= 500
    assert ev["truncated"] is True              # we wrote >500 rows
    assert ev["total_rows_exact_if_available"] > ev["included_rows"]
    # selected source is an absolute path under the supplied --data-dir
    assert ev["selected_absolute_source"] and ev["fallback_used"] is False


# 4. light passes run-ready when source event files verified
def test_light_bundle_run_ready_from_source(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="light")
    assert res["classification"] == "PASS_RUN_READY"
    assert res["run_ready_for_hours"] is True
    rr = res["run_ready"]
    assert rr["bundle_mode"] == "light"
    assert rr["full_event_files_omitted_intentionally"] is True
    assert rr["source_event_files_verified"] is True
    assert rr["tail_samples_included"] is True


# 5. full includes the full event files
def test_full_bundle_includes_full_event_files(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="full")
    names = _zip_names(res)
    assert _has(names, "data/training/events.jsonl")
    assert _has(names, "data/training/decision_records.jsonl")
    assert res["bundle_mode"] == "full"
    assert res["run_ready"]["full_event_files_omitted_intentionally"] is False


# 6. full bundle is larger than light (full JSONL included) + still run-ready
def test_full_bundle_larger_than_light(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch, big_rows=6000)
    light = _report(tmp_path / "l", dd, bundle_mode="light")
    full = _report(tmp_path / "f", dd, bundle_mode="full")
    light_sz = Path(light["zip_path"]).stat().st_size
    full_sz = Path(full["zip_path"]).stat().st_size
    assert full_sz > light_sz
    assert full["classification"] == "PASS_RUN_READY"


# 7. default bundle mode is light
def test_default_bundle_mode_is_light(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd)                 # no bundle_mode arg
    assert res["bundle_mode"] == "light"
    names = _zip_names(res)
    assert not _has(names, "data/training/events.jsonl")
    assert _has(names, "samples/events_tail_500.jsonl")


# --- source-strict + freshness reconciliation (light-bundle trust) ----------

def test_data_dir_never_falls_back_to_repo_local(tmp_path, monkeypatch):
    # repo-local data/training has a STALE decision_records.jsonl; --data-dir is a
    # DIFFERENT dir that is MISSING it. Source-strict must NOT use the repo fallback.
    dd = _runtime_dir(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    (repo / "data" / "training").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "training" / "decision_records.jsonl").write_text(
        json.dumps({"run_id": "pmtrain-STALE", "tick": 1}) + "\n", encoding="utf-8")
    (dd / "training" / "decision_records.jsonl").unlink()   # missing in the data dir
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(repo), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(dd), bundle_mode="light")
    stats = json.loads((Path(res["bundle_dir"]) / "samples" / "event_file_stats.json").read_text())
    dr = next(f for f in stats["files"] if f["logical_name"] == "decision_records.jsonl")
    assert dr["exists"] is False                  # not pulled from the repo fallback
    assert dr["fallback_used"] is False
    assert res["run_ready_for_hours"] is False    # hard-required missing -> not ready


def test_stale_decision_records_fails_run_ready(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    drp = dd / "training" / "decision_records.jsonl"
    mut = [json.dumps({**json.loads(l), "run_id": "pmtrain-OLD-999"})
           for l in drp.read_text().splitlines() if l.strip()]
    drp.write_text("\n".join(mut) + "\n", encoding="utf-8")
    res = _report(tmp_path, dd, bundle_mode="light")
    assert res["run_ready_for_hours"] is False
    assert res["run_ready"]["stale_or_mixed_training_tail_samples"] is True
    assert any("stale_or_mixed_training_tail_samples" in b
               for b in res["run_ready"]["blocking_reasons"])


def test_compatible_run_ids_pass_run_ready(tmp_path, monkeypatch):
    dd = _runtime_dir(tmp_path, monkeypatch)
    res = _report(tmp_path, dd, bundle_mode="light")
    assert res["classification"] == "PASS_RUN_READY"
    assert res["run_ready"]["proof"]["tail_samples_fresh_same_run"] is True
    assert res["run_ready"]["proof"]["single_source_data_dir"] is True


# 8. CLI console output shows bundle mode + source-event verification
def test_cli_console_shows_bundle_mode(tmp_path, monkeypatch, capsys):
    dd = _runtime_dir(tmp_path, monkeypatch)
    gen.main(["--output", str(tmp_path / "out"), "--repo-root", str(tmp_path),
              "--data-dir", str(dd), "--skip-tests", "--bundle-mode", "light"])
    out = capsys.readouterr().out
    assert "Bundle mode    : light" in out
    assert "Full JSONL     : omitted intentionally" in out
    assert "Samples        : tail 500 rows each" in out
    assert "Source events  : verified" in out
    assert "Zip size       :" in out
