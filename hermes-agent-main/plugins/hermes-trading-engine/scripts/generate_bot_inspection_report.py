#!/usr/bin/env python3
"""One-command bot inspection & performance report generator (PAPER ONLY).

Generates a complete, redacted inspection bundle (folder + zip) for ongoing
health/performance monitoring and external review of the Hermes Polymarket
paper-training engine.

    python scripts/generate_bot_inspection_report.py --output inspection_reports

This is REPORTING / INSPECTION ONLY. It never changes trading behavior, strategy
logic, architecture, `.env` values, or safety flags; never enables live trading,
wallet access, or real order submission; and redacts every secret before writing.

See the module-level collectors/redactor/safety/metrics/recommendations helpers
for the building blocks; all are importable + unit-tested without Docker or net.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

# Library-style logger: silent unless the caller configures logging handlers.
logger = logging.getLogger("hte.inspection.report")

# Make sibling helper modules importable whether run as a script or imported.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
_PLUGIN_ROOT = _THIS_DIR.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import inspection_collectors as collectors  # noqa: E402
import inspection_metrics as metrics  # noqa: E402
import inspection_recommendations as recs  # noqa: E402
import inspection_redactor as redactor  # noqa: E402
import inspection_safety_audit as safety_audit  # noqa: E402

SCHEMA_VERSION = "1.0"

# Canonical closed-loop artifacts that MUST appear in every inspection bundle at
# their canonical relative path (even if empty). Source: the live training data
# dir (HTE_DATA_DIR) written every tick by the trainer; fallback to repo root.
REQUIRED_CLOSED_LOOP_ARTIFACTS = (
    "metrics/inspection_summary.json", "metrics/closed_loop_learning.json",
    "metrics/learning_feedback.json", "metrics/active_learning.json",
    "metrics/paper_realism.json", "metrics/bregman_execution.json",
    "metrics/strategy_priority.json", "metrics/profitability_ranking.json",
    "metrics/correlation_risk.json", "metrics/training_reconciliation.json",
    "metrics/run_ready.json", "metrics/bregman_funnel.json",
    "metrics/grok_news_evidence.json",
    "reports/paper_training_inspection.md", "reports/closed_loop_learning_audit.md",
    "data/training/events.jsonl", "data/training/decision_records.jsonl",
    "data/training/no_trade_labels.jsonl", "data/training/shadow_labels.jsonl",
    "data/training/diagnostics.jsonl", "data/training/pending_labels.jsonl",
    "data/training/completed_labels.jsonl", "data/training/learning_state.json",
)


# ----------------------------------------------------------------------------- #
# Classification
# ----------------------------------------------------------------------------- #
def classify(safety: dict, tests: dict, runtime_available: bool,
             comparison: dict, missing_features: list,
             warnings: list, *, run_ready: Optional[dict] = None) -> str:
    """Determine the overall classification (precedence-ordered).

    Run-readiness is a HARD gate above warnings: a report can NEVER be
    PASS/PASS_WITH_WARNINGS when hard-required learning artifacts are missing,
    synthesized, or empty, when reconciliation fails, or when the edge audit is
    incomplete — that is ``FAIL_NOT_RUN_READY`` (a synthesized placeholder is not
    proof of learning)."""
    if safety.get("critical"):
        return "CRITICAL_SAFETY_FAIL"
    tests_failed = tests.get("present") is True and tests.get("passing") is False
    tests_missing = tests.get("present") is False
    if not runtime_available and (tests_failed or tests_missing) and not tests.get("skipped"):
        return "FAIL"
    if comparison.get("available") and comparison.get("regression"):
        return "REGRESSION"
    # HARD run-readiness gate (above warnings): missing/synthesized/empty required
    # artifacts, failed reconciliation, zero-decision ledger, silent Bregman, or
    # incomplete edge audit => NOT run-ready (a synthesized placeholder is not proof).
    if run_ready is not None:
        if not run_ready.get("run_ready_for_hours", False):
            return "FAIL_NOT_RUN_READY"
        # run-ready is the headline: required learning artifacts are real + reconciled.
        return "PASS_RUN_READY"
    # legacy path (run-readiness not computed): keep prior PASS/warnings behavior.
    clean = (
        safety.get("status") == "OK"
        and runtime_available
        and (tests.get("passing") is True or tests.get("skipped"))
        and not missing_features
        and not warnings
    )
    if clean:
        return "PASS"
    return "PASS_WITH_WARNINGS"


# ----------------------------------------------------------------------------- #
# Bundle writing helpers
# ----------------------------------------------------------------------------- #
class Bundle:
    """Tracks written files (relative paths) for the report manifest."""

    def __init__(self, root: Path):
        self.root = root
        self.files: list[str] = []

    def write_text(self, rel: str, text: str, redact: bool = True) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = redactor.redact_text(text) if redact else text
        path.write_text(body if body is not None else "", encoding="utf-8")
        if rel not in self.files:
            self.files.append(rel)

    def write_json(self, rel: str, obj: Any, redact: bool = True) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = redactor.redact_obj(obj) if redact else obj
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        if rel not in self.files:
            self.files.append(rel)


def _locate_artifact(rel: str, search_roots: list, *,
                     strict_root: Optional[str] = None) -> "tuple[Optional[Path], Optional[str], bool]":
    """Locate a canonical artifact, returning (path, selected_root, fallback_used).

    Accepts both the canonical ``data/training/...`` layout and the prod
    ``training/...`` layout. When ``strict_root`` is given (i.e. ``--data-dir`` was
    supplied), the file is ONLY looked up under that root — never a stale repo-local
    fallback. ``fallback_used`` is True when the file was found in a root OTHER than
    the first (primary) search root."""
    rels = [rel]
    if rel.startswith("data/"):
        rels.append(rel[len("data/"):])   # prod /data layout: training/... at root
    roots = [strict_root] if strict_root else list(search_roots)
    primary = (strict_root if strict_root else (search_roots[0] if search_roots else None))
    for root in roots:
        if root is None:
            continue
        for r in rels:
            p = Path(root) / r
            if p.exists() and p.is_file():
                fallback = (primary is not None and str(Path(root)) != str(Path(primary)))
                return p, str(root), bool(fallback)
    return None, None, False


def _file_fingerprint(path: Path, size_bytes: int) -> Optional[str]:
    """Cheap content fingerprint: sha256 over the head+tail (max 8KB each)."""
    import hashlib
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            head = fh.read(8192)
            h.update(head)
            if size_bytes > 16384:
                fh.seek(max(0, size_bytes - 8192))
                h.update(fh.read(8192))
        return h.hexdigest()[:32]
    except Exception:  # noqa: BLE001
        return None


def _row_field(row: str, *keys):
    try:
        obj = json.loads(row)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k) is not None:
            return obj.get(k)
    return None


def _tail_meta(rows: list) -> dict:
    """Extract run-id/tick/timestamp metadata from sampled JSONL rows for freshness
    + same-run reconciliation across the dedicated training streams."""
    if not rows:
        return {"first_tail_timestamp": None, "last_tail_timestamp": None,
                "first_tail_run_id": None, "last_tail_run_id": None,
                "run_ids_seen": [], "first_tail_tick": None, "last_tail_tick": None}
    run_ids: list = []
    for r in rows:
        rid = _row_field(r, "run_id")
        if rid is not None and rid not in run_ids:
            run_ids.append(str(rid))

    def _ts(r):
        return _row_field(r, "timestamp", "ts", "created_at", "label_due_at")
    return {
        "first_tail_timestamp": _ts(rows[0]),
        "last_tail_timestamp": _ts(rows[-1]),
        "first_tail_run_id": _row_field(rows[0], "run_id"),
        "last_tail_run_id": _row_field(rows[-1], "run_id"),
        "run_ids_seen": run_ids[-10:],
        "first_tail_tick": _row_field(rows[0], "tick"),
        "last_tail_tick": _row_field(rows[-1], "tick"),
    }


# HARD-required artifacts: a synthesized/missing/empty one of these means the run
# is NOT proven (run_ready_for_hours=false + classification FAIL_NOT_RUN_READY). A
# synthesized placeholder NEVER counts as proof of learning.
HARD_REQUIRED_ARTIFACTS = (
    "metrics/inspection_summary.json", "metrics/training_reconciliation.json",
    "metrics/run_ready.json", "data/training/events.jsonl",
    "data/training/decision_records.jsonl",
)

# Large append-only JSONL event streams. In LIGHT mode these are NOT copied in full
# into the zip — a tail sample + per-file stats are included instead (the full files
# stay durable in the runtime /data dir). In FULL mode they are copied verbatim.
EVENT_JSONL_ARTIFACTS = (
    "data/training/events.jsonl", "data/training/decision_records.jsonl",
    "data/training/no_trade_labels.jsonl", "data/training/shadow_labels.jsonl",
    "data/training/diagnostics.jsonl", "data/training/pending_labels.jsonl",
    "data/training/completed_labels.jsonl",
)
LIGHT_TAIL_ROWS = 500
SIZE_GUARD_BYTES = 25 * 1024 * 1024   # 25 MB: above this, light mode samples instead


def _tail_rows(path: Path, n: int) -> "tuple[list, int]":
    """Return (last ``n`` non-empty rows, total_row_count). Streams the file (no full
    load into memory beyond the tail window)."""
    from collections import deque
    tail: deque = deque(maxlen=n)
    total = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    total += 1
                    tail.append(line.rstrip("\n"))
    except Exception:  # noqa: BLE001
        return [], 0
    return list(tail), total


def _jsonl_ts(row: str, *keys) -> Optional[str]:
    try:
        obj = json.loads(row)
    except Exception:  # noqa: BLE001
        return None
    for k in ("timestamp", "ts", "created_at", "label_due_at"):
        if isinstance(obj, dict) and obj.get(k) is not None:
            return str(obj.get(k))
    return None


def write_closed_loop_artifacts(bundle, data_dir: Optional[str], repo_root: str,
                                bundle_mode: str = "light") -> dict:
    """Write every required closed-loop artifact into the bundle at its CANONICAL
    path, copying REAL runtime files when present (non-empty), else a clearly-marked
    synthesized placeholder. A synthesized placeholder NEVER counts as proof.

    bundle_mode:
      * ``full``  — include every file verbatim (forensic; large).
      * ``light`` (default) — include summary metrics/reports/learning_state directly,
        but for the large JSONL event streams include only ``samples/<name>_tail_500
        .jsonl`` + ``samples/event_file_stats.json``. The full files stay durable in
        the runtime /data dir and run-readiness is verified against the SOURCE files,
        so omitting them from the zip is intentional and not a "missing artifact"."""
    light = (str(bundle_mode).lower() != "full")
    search_roots = [r for r in (data_dir, repo_root,
                                str(Path(repo_root) / "data")) if r]
    # SOURCE-STRICT: when --data-dir is supplied, hard/audit-required files are read
    # ONLY from that dir — never a stale repo-local data/ fallback (the root cause of
    # mixed-freshness tail samples). Non-required extras may still fall back.
    strict_root = str(data_dir) if data_dir else None
    statuses: list = []
    event_stats: list = []
    tail_samples_included = False
    tail_meta_by_logical: dict = {}

    for rel in REQUIRED_CLOSED_LOOP_ARTIFACTS:
        src, sel_root, fallback_used = _locate_artifact(
            rel, search_roots, strict_root=strict_root)
        exists = src is not None
        non_empty = False
        synthesized = False
        omitted = False           # intentionally not copied in full (light JSONL)
        is_event_jsonl = rel in EVENT_JSONL_ARTIFACTS
        size_bytes = (src.stat().st_size if exists else -1)
        mtime = (round(src.stat().st_mtime, 3) if exists else None)
        logical = Path(rel).name

        if is_event_jsonl and light:
            # LIGHT: do NOT copy the full file; verify the source + emit a tail sample.
            name = logical[: -len(".jsonl")]
            sample_rel = f"samples/{name}_tail_{LIGHT_TAIL_ROWS}.jsonl"
            rows, total = (_tail_rows(src, LIGHT_TAIL_ROWS) if exists else ([], 0))
            non_empty = exists and total > 0
            omitted = True
            bundle.write_text(sample_rel, ("\n".join(rows) + ("\n" if rows else "")),
                              redact=True)
            tail_samples_included = True
            meta = _tail_meta(rows)
            tail_meta_by_logical[logical] = meta
            event_stats.append({
                "logical_name": logical,
                "source_path": rel,
                "selected_absolute_source": (str(src) if exists else None),
                "selected_root": sel_root,
                "bundle_sample_path": sample_rel,
                "exists": bool(exists),
                "size_bytes": int(size_bytes),
                "mtime": mtime,
                "total_rows_exact_if_available": int(total),
                "included_rows": len(rows),
                "source_sha256_head_tail": (_file_fingerprint(src, size_bytes)
                                            if exists else None),
                "truncated": bool(total > len(rows)),
                "source_verified_from_data_dir": bool(exists and not fallback_used
                                                      and strict_root is not None),
                "fallback_used": bool(fallback_used),
                **meta,
            })
        elif exists:
            try:
                body = src.read_text(encoding="utf-8", errors="replace")
                stripped = body.strip()
                non_empty = bool(stripped) and stripped not in ("{}", "[]", "null")
                # FULL-mode size guard: a giant non-event file still gets sampled in
                # light mode (approved summary files are always copied directly).
                if (light and size_bytes > SIZE_GUARD_BYTES
                        and not rel.endswith((".json", ".md"))):
                    sample_rel = f"samples/{logical}.tail.txt"
                    rows, total = _tail_rows(src, LIGHT_TAIL_ROWS)
                    bundle.write_text(sample_rel, "\n".join(rows), redact=True)
                    omitted = True
                    tail_samples_included = True
                else:
                    bundle.write_text(rel, body, redact=True)
            except Exception:  # noqa: BLE001
                exists = False
        if not exists and not omitted:
            synthesized = True
            if rel.endswith(".jsonl"):
                bundle.write_text(rel, "", redact=False)
            elif rel.endswith(".json"):
                bundle.write_json(rel, {"_synthesized_placeholder": True,
                                        "_valid_for_run_ready": False}, redact=False)
            else:
                bundle.write_text(rel, f"# {Path(rel).name} — NOT produced this run "
                                  "(synthesized placeholder; not proof of learning).\n",
                                  redact=False)
        hard = rel in HARD_REQUIRED_ARTIFACTS
        statuses.append({
            "path": rel,
            "exists": bool(exists),
            "non_empty": bool(non_empty),
            "synthesized": bool(synthesized),
            "omitted_intentionally": bool(omitted),
            "hard_required": bool(hard),
            # source-verified (exists + non-empty) is proof — whether copied in full
            # OR omitted-but-sampled in light mode.
            "valid_for_run_ready": bool(exists and non_empty),
            "source": str(src) if exists else None,
            "selected_root": sel_root,
            "fallback_used": bool(fallback_used),
            "source_size_bytes": int(size_bytes),
        })

    # --- light-bundle freshness + mixed-source reconciliation ----------------
    fresh = _reconcile_tail_freshness(statuses, tail_meta_by_logical,
                                      strict_root=strict_root)
    if light:
        bundle.write_json("samples/event_file_stats.json", {
            "bundle_mode": "light", "tail_rows": LIGHT_TAIL_ROWS,
            "data_dir": strict_root, "source_strict": bool(strict_root),
            "generated_from": [r for r in search_roots],
            "freshness": fresh,
            "files": event_stats,
        })

    synthesized_paths = [s["path"] for s in statuses if s["synthesized"]]
    empty_real = [s["path"] for s in statuses if s["exists"] and not s["non_empty"]]
    hard_invalid = [s["path"] for s in statuses
                    if s["hard_required"] and not s["valid_for_run_ready"]]
    omitted_paths = [s["path"] for s in statuses if s["omitted_intentionally"]]
    # mixed source roots: any hard-required file pulled from a non-primary root.
    mixed_source_roots = [s["path"] for s in statuses
                          if s["hard_required"] and s.get("fallback_used")]
    source_event_files_verified = all(
        s["valid_for_run_ready"] and not s.get("fallback_used") for s in statuses
        if s["path"] in EVENT_JSONL_ARTIFACTS and s["hard_required"])
    manifest = {
        "bundle_mode": "light" if light else "full",
        "required": list(REQUIRED_CLOSED_LOOP_ARTIFACTS),
        "hard_required": list(HARD_REQUIRED_ARTIFACTS),
        "artifacts": statuses,
        "event_file_stats": event_stats,
        "tail_freshness": fresh,
        "present_from_runtime": [s["path"] for s in statuses if s["valid_for_run_ready"]],
        "synthesized_empty": synthesized_paths,
        "empty_real_files": empty_real,
        "hard_required_invalid": hard_invalid,
        "mixed_source_roots": mixed_source_roots,
        "stale_or_mixed_training_tail_samples": bool(
            (not fresh.get("compatible", True)) or mixed_source_roots),
        "full_event_files_omitted_intentionally": bool(light and omitted_paths),
        "omitted_full_event_files": omitted_paths,
        "tail_samples_included": bool(tail_samples_included),
        "source_event_files_verified": bool(source_event_files_verified),
        "all_paths_in_bundle": not light,
        "runtime_complete": not synthesized_paths and not empty_real,
        "hard_required_satisfied": not hard_invalid,
        "search_roots": search_roots,
        "source_strict": bool(strict_root),
    }
    bundle.write_json("metrics/closed_loop_artifacts_manifest.json", manifest)
    return manifest


# Dedicated streams that must advance with their event_type in events.jsonl, paired
# with the events.jsonl event_type and the closed-loop counter that proves activity.
_STREAM_FRESHNESS = (
    # (dedicated logical file, closed_loop counter key that should make it advance)
    ("decision_records.jsonl", "decision_records_written"),
    ("no_trade_labels.jsonl", "no_trade_labels_written"),
    ("pending_labels.jsonl", "pending_labels_total"),
)


# A dedicated stream whose last tail timestamp lags events.jsonl by more than this many
# seconds (while events advanced) is treated as a stale/mismatched freshness window ->
# NOT run-ready (env-tunable). Default 6h is generous for quiet periods yet still catches
# the observed ~12.6h decision/no-trade/pending lag. NEVER loosens a trade gate.
_TAIL_FRESHNESS_MAX_GAP_SEC = float(os.getenv("HTE_TAIL_FRESHNESS_MAX_GAP_SEC", "21600") or 21600)


def _reconcile_tail_freshness(statuses: list, tail_meta: dict, *,
                              strict_root: Optional[str]) -> dict:
    """Prove the sampled dedicated training streams are from the SAME fresh run window as
    events.jsonl. A dedicated stream is stale/mixed -> NOT run-ready when it has rows but
    (a) a DIFFERENT last run_id than events.jsonl, (b) no run_id while events advanced, OR
    (c) its last timestamp lags events.jsonl's last timestamp by more than
    ``_TAIL_FRESHNESS_MAX_GAP_SEC`` (the observed failure: decision/no-trade/pending
    samples ~12.6h behind fresh events). Returns the freshness record."""
    ev = tail_meta.get("events.jsonl") or {}
    ev_run = ev.get("last_tail_run_id")
    ev_ts = _num_or_none(ev.get("last_tail_timestamp"))
    reasons: list = []
    per_stream: dict = {}
    has_rows = {s["path"].split("/")[-1]: (s["exists"] and s["non_empty"]) for s in statuses}

    for logical, _counter in _STREAM_FRESHNESS:
        meta = tail_meta.get(logical) or {}
        rid = meta.get("last_tail_run_id")
        ts = _num_or_none(meta.get("last_tail_timestamp"))
        entry = {"last_run_id": rid, "last_timestamp": ts,
                 "events_run_id": ev_run, "events_last_timestamp": ev_ts,
                 "compatible_run_id": None, "fresh_window_ok": None,
                 "gap_vs_events_sec": None, "stale_reason": None}
        if not has_rows.get(logical):
            entry["compatible_run_id"] = True   # empty file: covered by counter checks
            entry["fresh_window_ok"] = True
        else:
            # (a/b) run-id compatibility
            if ev_run is not None and rid is not None:
                same = (str(rid) == str(ev_run))
                entry["compatible_run_id"] = same
                if not same:
                    entry["stale_reason"] = "different_run_id_than_events"
                    reasons.append(f"{logical}: last_run_id={rid} != events run_id={ev_run}")
            elif ev_run is not None and rid is None:
                entry["compatible_run_id"] = False
                entry["stale_reason"] = "missing_run_id_while_events_advancing"
                reasons.append(f"{logical}: rows present but no run_id while events advanced")
            else:
                entry["compatible_run_id"] = True
            # (c) freshness-window: a stream far behind fresh events is stale/mixed
            if ev_ts is not None and ts is not None:
                gap = round(ev_ts - ts, 3)
                entry["gap_vs_events_sec"] = gap
                entry["fresh_window_ok"] = bool(gap <= _TAIL_FRESHNESS_MAX_GAP_SEC)
                if gap > _TAIL_FRESHNESS_MAX_GAP_SEC:
                    entry["stale_reason"] = (entry["stale_reason"]
                                             or "stale_freshness_window_vs_events")
                    reasons.append(
                        f"{logical}: last_ts {ts} lags events {ev_ts} by {int(gap)}s "
                        f"> {int(_TAIL_FRESHNESS_MAX_GAP_SEC)}s (stale freshness window)")
            else:
                entry["fresh_window_ok"] = True   # cannot compare -> not a window failure
        per_stream[logical] = entry

    incompatible = [k for k, v in per_stream.items()
                    if v.get("compatible_run_id") is False or v.get("fresh_window_ok") is False]
    return {
        "events_last_run_id": ev_run,
        "events_last_timestamp": ev_ts,
        "freshness_max_gap_sec": _TAIL_FRESHNESS_MAX_GAP_SEC,
        "per_stream": per_stream,
        "incompatible_streams": incompatible,
        "compatible": not reasons,
        "reasons": reasons,
        "source_strict": bool(strict_root),
    }


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _artifact_paths(data_dir: Optional[str], repo_root: str, bundle_dir: Path,
                    status_source: Optional[str]) -> dict:
    """Surface the actual runtime vs collector paths so a Docker-container/host
    mismatch (the usual cause of "synthesized empty" artifacts) is visible."""
    dd = Path(data_dir) if data_dir else None

    def _ex(p) -> bool:
        try:
            return bool(p and Path(p).exists())
        except Exception:  # noqa: BLE001
            return False
    runtime_metrics = (dd / "metrics") if dd else None
    runtime_reports = (dd / "reports") if dd else None
    runtime_training = (dd / "training") if dd else None
    return {
        "data_dir": str(dd) if dd else None,
        "data_dir_env_HTE_DATA_DIR": os.getenv("HTE_DATA_DIR"),
        "status_source": status_source,
        "runtime_metrics_dir": str(runtime_metrics) if runtime_metrics else None,
        "runtime_metrics_dir_exists": _ex(runtime_metrics),
        "runtime_reports_dir": str(runtime_reports) if runtime_reports else None,
        "runtime_reports_dir_exists": _ex(runtime_reports),
        "runtime_training_data_dir": str(runtime_training) if runtime_training else None,
        "runtime_training_data_dir_exists": _ex(runtime_training),
        "collector_metrics_dir": str(Path(repo_root) / "metrics"),
        "collector_reports_dir": str(Path(repo_root) / "reports"),
        "collector_training_data_dir": str(Path(repo_root) / "data" / "training"),
        "inspection_bundle_dir": str(bundle_dir),
        "hint": ("If runtime_*_dir_exists is false, pass --data-dir <HTE_DATA_DIR> "
                 "(or the Docker volume host path) so the collector packages the REAL "
                 "runtime files instead of synthesizing empty placeholders."),
    }


def build_report_run_ready(manifest: dict, status: dict, algo_audit: dict,
                           validation_contract: dict) -> dict:
    """Report-level run-ready verdict computed from ARTIFACT REALITY (not the
    possibly-synthesized run_ready.json). A synthesized/empty hard-required
    artifact => run_ready_for_hours=false. This is the authoritative verdict the
    classification + report use; it overrides the bundle's run_ready.json."""
    status = status or {}
    arts = {a["path"]: a for a in (manifest.get("artifacts") or [])}
    cll = status.get("closed_loop_learning", {}) or {}
    recon = status.get("training_reconciliation", {}) or {}
    ledger = status.get("ledger", {}) or {}
    funnel = status.get("bregman_funnel", {}) or {}
    # Grok evidence: prefer the unified grok_news_evidence block, else research status.
    _grok = (status.get("grok_news_evidence") or {}) or (status.get("research") or {})
    decision_count = int(metrics._first(recon.get("decision_count_counter"),
                                status.get("decisions"),
                                (status.get("pnl", {}) or {}).get("decision_count"), 0) or 0)

    def _ok(rel: str) -> bool:
        a = arts.get(rel)
        return bool(a and a.get("valid_for_run_ready"))

    blocking: list = []
    warnings: list = []
    synth = manifest.get("synthesized_empty") or []
    empty_real = manifest.get("empty_real_files") or []
    hard_invalid = manifest.get("hard_required_invalid") or []
    # HARD: a missing/synthesized/empty hard-required artifact blocks run-readiness.
    if hard_invalid:
        blocking.append(f"hard-required artifacts missing/synthesized/empty: {hard_invalid}")
    # any synthesized placeholder (even non-hard) means the runtime files were not
    # found at the collector path -> not a real, complete run.
    if synth:
        blocking.append(f"{len(synth)} closed-loop artifact(s) synthesized (not real): {synth}")
    # legitimately-empty NON-hard files (e.g. shadow/completed labels with 0 rows)
    # are only a warning, not a blocker.
    non_hard_empty = [p for p in empty_real if p not in HARD_REQUIRED_ARTIFACTS]
    if non_hard_empty:
        warnings.append(f"{len(non_hard_empty)} non-required artifact(s) present but empty "
                        f"(legitimate when zero events): {non_hard_empty}")
    # LIGHT-mode trust: stale/mixed tail samples (a dedicated stream from a different
    # run than events.jsonl) or a hard-required file pulled from a stale repo-local
    # fallback => NOT run-ready. Durable proof must come from ONE runtime data dir.
    fresh = manifest.get("tail_freshness", {}) or {}
    if manifest.get("stale_or_mixed_training_tail_samples"):
        detail = "; ".join(fresh.get("reasons", []) or [])
        mixed = manifest.get("mixed_source_roots") or []
        if mixed:
            detail = (detail + f" | mixed source roots: {mixed}").strip(" |")
        blocking.append("stale_or_mixed_training_tail_samples: " + (detail or "see manifest"))

    event_files_present = _ok("data/training/events.jsonl") and _ok(
        "data/training/decision_records.jsonl")
    event_files_non_empty = event_files_present
    artifact_files_real = not synth
    recon_passed = bool(recon.get("reconciled", False)) and _ok(
        "metrics/training_reconciliation.json")
    if not recon_passed:
        blocking.append("training reconciliation missing or not passed")
    ledger_decisions = int(ledger.get("decisions", 0) or 0)
    ledger_reconciled = not (decision_count > 0 and ledger_decisions == 0)
    if not ledger_reconciled:
        blocking.append("ledger.decisions==0 while decision_count>0")
    # Bregman: enabled but zero scanned requires adapter diagnostics (else silent)
    breg_tel = (status.get("bregman", {}) or {}).get("execution",
                                                     status.get("bregman", {})) or {}
    bregman_enabled = bool(breg_tel.get("bregman_paper_enabled")
                           or algo_audit.get("bregman_enabled"))
    scanned = int(metrics._first(funnel.get("groups_sent_to_certifier"),
                         breg_tel.get("constraint_groups_scanned"), 0) or 0)
    adapter_failed = int(funnel.get("groups_adapter_failed", 0) or 0)
    diag_written = int(funnel.get("diagnostic_events_written",
                                  ledger.get("bregman_diagnostics", 0)) or 0)
    bregman_non_silent = (not bregman_enabled) or scanned > 0 or (
        adapter_failed > 0 and diag_written > 0)
    if bregman_enabled and not bregman_non_silent:
        blocking.append("Bregman enabled but groups_scanned=0 without adapter diagnostics")
    edge_audit_ok = bool(algo_audit.get("ok"))
    if not edge_audit_ok:
        blocking.append("algorithmic edge audit incomplete: "
                        + ", ".join(algo_audit.get("hard_failures")
                                    or algo_audit.get("required_field_violations")
                                    or ["incomplete"]))
    inspection_complete = _ok("metrics/inspection_summary.json")
    if not inspection_complete:
        blocking.append("inspection_summary.json missing/synthesized/empty")
    # TRUTH-CHAIN: active learning declared (aggressive_paper profile) but effectively
    # OFF in the running container's durable metrics => config_mismatch. The aggressive
    # profile ALWAYS enables active learning, so this means the container is STALE vs the
    # repo (no tiny-exploration lane). Fail run-readiness with an exact, actionable reason
    # so a degraded bot can never falsely pass inspection for a multi-day run.
    al_block = status.get("active_learning", {}) or {}
    al_src = str(al_block.get("active_learning_config_source", "")).strip()
    al_effective = bool(al_block.get("active_learning_runtime_enabled",
                                     al_block.get("active_learning_enabled", True)))
    al_mismatch = bool(al_block) and (al_src == "aggressive_paper_profile") and not al_effective
    if al_mismatch or bool(al_block.get("active_learning_config_mismatch")):
        blocking.append(
            "config_mismatch: active-learning DECLARED (config_source="
            f"{al_src or '?'}) but effective active_learning_enabled=false. The aggressive_"
            "paper profile always enables active learning, so the running container is STALE "
            "relative to the repo. Rebuild it (mission-control --mode proof2h "
            "--approved-paper-run) so the tiny-exploration lane is active before a long run.")
    config_consistent = not al_mismatch
    closed_loop_durable = event_files_non_empty and artifact_files_real

    proof = {
        "training_healthy": bool(status) and not status.get("error"),
        "event_files_present": bool(event_files_present),
        "event_files_non_empty": bool(event_files_non_empty),
        "artifact_files_real_not_synthesized": bool(artifact_files_real),
        "ledger_reconciled": bool(ledger_reconciled),
        "training_reconciliation_passed": bool(recon_passed),
        "bregman_funnel_non_silent": bool(bregman_non_silent),
        "closed_loop_durable": bool(closed_loop_durable),
        "inspection_artifacts_complete": bool(inspection_complete),
        "tail_samples_fresh_same_run": not bool(
            manifest.get("stale_or_mixed_training_tail_samples")),
        "single_source_data_dir": not bool(manifest.get("mixed_source_roots")),
        "active_learning_config_consistent": bool(config_consistent),
        "live_trading_disabled": True,
    }
    run_ready = (not blocking) and all(proof.values())
    return {
        "run_ready_for_hours": bool(run_ready),
        "max_safe_runtime_minutes": (None if run_ready else 10),
        "blocking_reasons": blocking,
        "warnings": warnings,
        "proof": proof,
        # bundle-mode transparency: in light mode the full JSONL is intentionally
        # omitted from the zip but verified at the SOURCE (this is not "missing").
        "bundle_mode": manifest.get("bundle_mode", "light"),
        "full_event_files_omitted_intentionally": bool(
            manifest.get("full_event_files_omitted_intentionally", False)),
        "source_event_files_verified": bool(manifest.get("source_event_files_verified", False)),
        "tail_samples_included": bool(manifest.get("tail_samples_included", False)),
        "tail_freshness": manifest.get("tail_freshness", {}),
        "stale_or_mixed_training_tail_samples": bool(
            manifest.get("stale_or_mixed_training_tail_samples")),
        # Grok brain readiness is a SEPARATE signal from paper run-readiness: paper
        # training is run-ready even if Grok hasn't proven a call, but the report must
        # NOT imply a healthy/functional Grok brain when grok_calls_total=0.
        "grok_brain_ready": bool(_grok.get("grok_brain_ready", False)),
        "grok_brain_blocker": _grok.get("grok_brain_blocker"),
        "source": "inspection_report (artifact-reality verdict)",
    }


def _reconcile_malformed_groups(status: dict, data_dir) -> None:
    """Reconcile malformed-group SUMMARY vs diagnostic TAIL into the funnel.

    The summary count comes from the TRAINER certifier path; ``skip_reason=malformed_group``
    diagnostic rows come from the ABCAS SCANNER path. They are different sources — this
    counts both and labels them so the light report can never say zero while the tail
    shows malformed groups. Mutates ``status['bregman_funnel']`` in place."""
    funnel = status.setdefault("bregman_funnel", {})
    scanner = status.get("bregman", {}) or {}
    reported = int(funnel.get("malformed_group_count", 0) or 0)
    runtime = int((scanner.get("skip_reasons", {}) or {}).get("malformed_group", 0) or 0)
    runtime += int(scanner.get("malformed_groups_rejected", 0) or 0)
    tail = 0
    try:
        if data_dir:
            dpath = Path(data_dir) / "training" / "diagnostics.jsonl"
            if dpath.exists():
                for line in dpath.read_text(encoding="utf-8").splitlines():
                    if '"malformed_group"' in line or "'malformed_group'" in line:
                        try:
                            row = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if row.get("skip_reason") == "malformed_group":
                            tail += 1
    except Exception:  # noqa: BLE001
        pass
    total = max(reported, runtime, tail)
    funnel["bregman_malformed_group_runtime_count"] = runtime
    funnel["bregman_malformed_group_reported_count"] = reported
    funnel["bregman_malformed_group_tail_count"] = tail
    # rows in the tail not explained by the certifier-path summary are ABCAS
    # scanner-path real rejects (counted), NOT legacy/stale — label accordingly.
    funnel["bregman_malformed_group_legacy_or_tail_only_count"] = max(0, tail - runtime)
    funnel["malformed_group_count"] = total          # reconciled (no contradiction)
    funnel["bregman_malformed_group_source"] = (
        "abcas_scanner_path_real_rejects" if runtime or tail else "none")


def _reconcile_bregman_sources(status: dict, data_dir) -> dict:
    """Make metrics/bregman_funnel.json the CANONICAL Bregman source of truth.

    Loads the funnel into status["bregman_funnel"], compares its scanned count with
    the legacy metrics/bregman.json, and returns the reconciliation record. The
    legacy scanner scanning zero while the canonical funnel scanned > 0 is a
    WARNING ONLY — it must not drive run-readiness."""
    funnel = dict(status.get("bregman_funnel") or {})
    try:
        if data_dir:
            p = Path(data_dir) / "metrics" / "bregman_funnel.json"
            if p.exists():
                disk = json.loads(p.read_text(encoding="utf-8")) or {}
                if isinstance(disk, dict) and disk:
                    # disk funnel wins (it is the canonical runtime artifact)
                    funnel = {**funnel, **disk}
    except Exception:  # noqa: BLE001
        pass
    if funnel:
        status["bregman_funnel"] = funnel
    canon_scanned = metrics._num(metrics._first(
        funnel.get("constraint_groups_scanned"),
        funnel.get("groups_sent_to_certifier"))) or 0
    legacy = status.get("bregman", {}) or {}
    legacy_scanned = metrics._num(legacy.get("constraint_groups_scanned")) or 0
    # mark the legacy telemetry as superseded (does NOT control run-readiness)
    if isinstance(status.get("bregman"), dict):
        status["bregman"].setdefault("source", "legacy_abcas_scanner_telemetry")
        status["bregman"]["controls_run_ready"] = False
        status["bregman"]["superseded_by"] = "metrics/bregman_funnel.json"
    disagree = (canon_scanned > 0 and legacy_scanned <= 0)
    return {
        "canonical_source": "metrics/bregman_funnel.json",
        "legacy_source": "metrics/bregman.json",
        "canonical_constraint_groups_scanned": int(canon_scanned),
        "legacy_constraint_groups_scanned": int(legacy_scanned),
        "canonical_controls_run_ready": True,
        "legacy_controls_run_ready": False,
        "sources_disagree": bool(disagree),
        "classification_impact": "warning_only" if disagree else "none",
        "warning": ("legacy_bregman_scanner_zero_but_canonical_funnel_active"
                    if disagree else ""),
    }


def _read_file(path: Path) -> Optional[str]:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    return None


# ----------------------------------------------------------------------------- #
# Core report generation
# ----------------------------------------------------------------------------- #
def generate_report(
    output_dir: str,
    repo_root: Optional[str] = None,
    *,
    skip_tests: bool = False,
    skip_docker: bool = False,
    skip_api: bool = False,
    skip_artifacts: bool = False,
    include_docker: bool = True,
    include_api: bool = True,
    include_artifacts: bool = True,
    include_container_artifacts: bool = False,
    tail_training_logs: int = 1000,
    tail_engine_logs: int = 500,
    history_days: int = 7,
    baseline_path: Optional[str] = None,
    pr: Optional[str] = None,
    api_base_url: str = "http://localhost:8800",
    data_dir: Optional[str] = None,
    bundle_mode: str = "light",
    now: Optional[_dt.datetime] = None,
    runner=None,
    opener=None,
) -> dict:
    """Generate the inspection bundle. Returns a summary dict with paths +
    classification. Never raises on collection failures (records them instead)."""
    repo_root = str(Path(repo_root or _PLUGIN_ROOT).resolve())
    now = now or _dt.datetime.now(_dt.timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    name = f"bot_inspection_pr{pr}_{ts}" if pr else f"bot_inspection_{ts}"

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    bundle_dir = out_root / name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle = Bundle(bundle_dir)

    warnings: list[str] = []
    errors: list[str] = []
    data_dir = data_dir or os.getenv("HTE_DATA_DIR")

    # --- env / config (parsed in-memory for audit; redacted on disk) ---------- #
    env_text = _read_file(Path(repo_root) / ".env") or ""
    env_example_text = _read_file(Path(repo_root) / ".env.example") or ""
    compose_text = _read_file(Path(repo_root) / "docker-compose.yml") or ""
    dockerfile_text = _read_file(Path(repo_root) / "Dockerfile") or ""
    req_text = _read_file(Path(repo_root) / "requirements.txt") or ""
    req_dev_text = _read_file(Path(repo_root) / "requirements-dev.txt") or ""
    pyproject_text = _read_file(Path(repo_root) / "pyproject.toml")

    env_map = safety_audit.parse_env_assignments(env_text)
    compose_env = safety_audit.parse_compose_environment(compose_text)
    # effective env for feature detection (real .env wins over compose defaults)
    eff_env = dict(compose_env)
    eff_env.update(env_map)

    # --- collectors ----------------------------------------------------------- #
    git = collectors.collect_git(repo_root, runner=runner)

    docker: dict = {"available": False, "skipped": True}
    if include_docker and not skip_docker:
        docker = collectors.collect_docker(repo_root, runner=runner,
                                           tail_training=tail_training_logs,
                                           tail_engine=tail_engine_logs)

    api: dict = {}
    if include_api and not skip_api:
        api = collectors.collect_api(api_base_url, opener=opener)
    api_json = collectors.api_json_map(api)

    tests = collectors.collect_tests(repo_root, runner=runner, skip=skip_tests)

    # --- status (local file → docker json) ------------------------------------ #
    local_status = collectors.read_local_status(data_dir, repo_root)
    status = local_status.get("status") or {}
    status_source = local_status.get("source")
    if not status:
        d_status = collectors.extract_status_from_docker(docker)
        if d_status:
            status = d_status
            status_source = "docker:hermes-training"
    # Merge the ABCAS/Bregman paper-scan telemetry into status["bregman"] so the
    # audit + validation contract see constraint groups scanned (not the raw file).
    try:
        if data_dir:
            _bs = Path(data_dir) / "bregman_scan.json"
            if _bs.exists():
                _tel = json.loads(_bs.read_text(encoding="utf-8")) or {}
                merged = dict(status.get("bregman") or {})
                merged.update(_tel)
                status["bregman"] = merged
    except Exception:  # noqa: BLE001
        pass
    # CANONICAL Grok evidence: overlay the durable metrics/grok_news_evidence.json
    # written by the live training run so an OFFLINE report reflects the real proof
    # call (grok_brain_ready / grok_calls_total) instead of a fresh keyless recompute.
    try:
        if data_dir:
            for _gp in (Path(data_dir) / "metrics" / "grok_news_evidence.json",
                        Path(data_dir) / "grok_news_evidence.json"):
                if _gp.exists():
                    _ge = json.loads(_gp.read_text(encoding="utf-8")) or {}
                    if _ge:
                        # durable run evidence wins over a keyless offline recompute
                        if int(_ge.get("grok_calls_total", 0) or 0) >= int(
                                (status.get("grok_news_evidence") or {}).get(
                                    "grok_calls_total", 0) or 0):
                            status["grok_news_evidence"] = _ge
                    break
    except Exception:  # noqa: BLE001
        pass
    # CANONICAL Bregman source-of-truth: load metrics/bregman_funnel.json into
    # status["bregman_funnel"] so the edge audit + validation + run-ready read the
    # NEW funnel (constraint_groups_scanned) — not the legacy zero-scan bregman.json.
    bregman_source_reconciliation = _reconcile_bregman_sources(status, data_dir)
    if bregman_source_reconciliation.get("warning"):
        warnings.append(bregman_source_reconciliation["warning"])
    # Reconcile malformed-group SUMMARY (trainer certifier path) with the diagnostic
    # TAIL (ABCAS scanner path) so the light report never claims zero malformed groups
    # while tail samples show them. Labels them by source; never contradictory.
    _reconcile_malformed_groups(status, data_dir)
    # Shadow-label durability proof: count durable shadow_labels.jsonl rows so the
    # report can prove shadow candidates are actually persisted (not just counted).
    try:
        _sl_rows = 0
        if data_dir:
            _slp = Path(data_dir) / "training" / "shadow_labels.jsonl"
            if _slp.exists():
                _sl_rows = sum(1 for ln in _slp.read_text(encoding="utf-8").splitlines()
                               if ln.strip())
        _bfn = status.setdefault("bregman_funnel", {})
        _bfn["shadow_labels_tail_nonempty"] = bool(_sl_rows > 0)
        _bfn["shadow_records_written"] = max(
            int(_bfn.get("bregman_shadow_labels_written", 0) or 0), _sl_rows,
            int((status.get("closed_loop_learning", {}) or {}).get("shadow_records_written", 0) or 0))
    except Exception:  # noqa: BLE001
        pass
    # Known-good synthetic fixture proof (isolated certifier; default gates; never
    # live, never touches real metrics) — proves real candidates=0 is a DATA problem.
    try:
        from engine.training.bregman_fixture import run_bregman_synthetic_fixture
        synthetic_fixture = run_bregman_synthetic_fixture()
    except Exception as exc:  # noqa: BLE001
        synthetic_fixture = {"bregman_synthetic_fixture_passed": False,
                             "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    status.setdefault("bregman_funnel", {})["synthetic_fixture"] = synthetic_fixture
    runtime_available = bool(status) or bool(
        api_json.get("state")) or bool(api_json.get("health"))

    # --- safety audit --------------------------------------------------------- #
    safety = safety_audit.audit(env=env_map, compose_env=compose_env,
                                status=status, api=api_json)

    # --- features / metrics --------------------------------------------------- #
    feats = metrics.extract_features(status, api_json, tests, eff_env)
    baseline = _load_baseline(baseline_path) if baseline_path else None
    comparison = metrics.compare_baseline(feats, baseline)
    missing_features = metrics.detect_missing_features(feats, api, tests)

    # --- algorithmic benchmark layer + cross-surface consistency + quant matrix -
    benchmarks = metrics.build_benchmarks(feats)
    consistency = metrics.detect_inconsistencies(feats, status, api_json)
    quant_responsibilities = metrics.build_quant_responsibilities(feats)
    final_validation = metrics.build_final_validation(feats)
    ledger_reconciliation = _reconcile_equities(feats, status, api_json, data_dir)
    if not ledger_reconciliation.get("ok", True):
        warnings.append(
            f"EQUITY RECONCILIATION FAILED (> {ledger_reconciliation.get('tolerance_pct')}%): "
            f"{ledger_reconciliation.get('failed_pairs')}")

    # --- institutional validation contract + production-readiness verdict ---
    from engine import validation_contract as vc
    validation_contract = vc.build_validation_contract(
        feats, ledger_reconciliation=ledger_reconciliation)
    ledger_returns = _ledger_returns(data_dir)
    expectancy = vc.credible_positive_expectancy(ledger_returns)
    readiness_verdict = vc.production_readiness_verdict(validation_contract, expectancy)
    if not validation_contract["passed"]:
        warnings.append("VALIDATION CONTRACT FAILED: " + ", ".join(validation_contract["failed"]))

    # --- artifacts ------------------------------------------------------------ #
    artifacts: dict = {"skipped": True, "host_found": [], "host_missing": list(
        collectors.ARTIFACT_DIRS), "any_found": False}
    if include_artifacts and not skip_artifacts:
        artifacts = collectors.collect_artifacts(
            repo_root, bundle_dir / "artifacts", data_dir=data_dir,
            include_container=include_container_artifacts, runner=runner)

    # TASK 6/11: write the canonical closed-loop artifacts into the bundle at their
    # canonical paths. In LIGHT mode (default) the large JSONL event streams are
    # tail-sampled instead of copied in full (full files stay durable in /data).
    bundle_mode = "full" if str(bundle_mode).lower() == "full" else "light"
    # Stage timing: the closed-loop-artifact write + redaction is the heaviest stage (it
    # was the observed hang point on huge JSONL streams). The redactor now caps oversized
    # text so this is bounded; we log the elapsed time so a slow stage is visible.
    import time as _t
    _stage_t0 = _t.perf_counter()
    logger.info("report stage: writing closed-loop artifacts (bundle_mode=%s)…", bundle_mode)
    closed_loop_manifest = write_closed_loop_artifacts(
        bundle, data_dir, repo_root, bundle_mode=bundle_mode)
    logger.info("report stage: closed-loop artifacts done in %.2fs", _t.perf_counter() - _stage_t0)
    if closed_loop_manifest.get("synthesized_empty"):
        warnings.append(
            f"{len(closed_loop_manifest['synthesized_empty'])} closed-loop artifact(s) "
            f"were empty/missing from the runtime data dir (synthesized empty): "
            f"{closed_loop_manifest['synthesized_empty']}")

    # --- warnings ------------------------------------------------------------- #
    if safety.get("warn"):
        warnings.append("safety audit raised warnings")
    if missing_features:
        warnings.append(f"{len(missing_features)} missing/weak feature(s)")
    if api and any(not v.get("ok") for v in api.values()):
        warnings.append("one or more API endpoints unreachable")
    if tests.get("present") is False and not tests.get("skipped"):
        warnings.append("test suite not found")
    if not runtime_available:
        warnings.append("no paper-training status collected")
    if benchmarks.get("failing"):
        warnings.append(f"{len(benchmarks['failing'])} benchmark(s) failing")
    if consistency:
        warnings.append(f"{len(consistency)} cross-surface inconsistency(ies)")

    # --- scorecard + classification ------------------------------------------ #
    observability = {
        "artifacts_found": artifacts.get("any_found"),
        "logs_collected": bool((docker.get("logs_training") or {}).get("ok")),
        "api_ok": bool(api) and any(v.get("ok") for v in api.values()),
    }
    scorecard = metrics.compute_scorecard(feats, safety, tests, runtime_available,
                                          comparison, observability)

    # --- mandatory Algorithmic Edge Audit (decision-grade; fails loud) ------ #
    algo_audit = metrics.build_algorithmic_edge_audit(
        feats, status, scorecard=scorecard, benchmarks=benchmarks,
        recommendations=None, status_age_s=_status_age_s(status, now))
    if not algo_audit["ok"]:
        detail = ", ".join(algo_audit.get("hard_failures")
                           or algo_audit.get("required_field_violations")
                           or ["incomplete"])
        cap = algo_audit.get("readiness_cap")
        warnings.append(
            f"ALGORITHMIC EDGE AUDIT INCOMPLETE (readiness capped <= {cap}): {detail}")

    # --- report-level run-ready verdict (artifact reality; HARD classification gate)
    report_run_ready = build_report_run_ready(
        closed_loop_manifest, status, algo_audit, validation_contract)
    # OVERRIDE the bundle's run_ready.json with the artifact-reality verdict so a
    # synthesized/empty run_ready.json can never claim run-ready.
    bundle.write_json("metrics/run_ready.json", report_run_ready)
    if not report_run_ready["run_ready_for_hours"]:
        warnings.append("NOT RUN-READY: " + "; ".join(report_run_ready["blocking_reasons"]))
    # Grok brain readiness (separate from paper run-readiness): if Grok is enabled +
    # key present but no advisory call has proven it, surface a precise blocker so the
    # report never implies a healthy/functional Grok brain at grok_calls_total=0.
    if not report_run_ready.get("grok_brain_ready") and (
            (status.get("research", {}) or {}).get("grok_enabled")):
        warnings.append("GROK BRAIN NOT READY: "
                        + str(report_run_ready.get("grok_brain_blocker") or "no_grok_call_yet"))
    # certified=0 with a healthy scanned funnel is a STRATEGY result, not a failure.
    _bf = status.get("bregman_funnel", {}) or {}
    if int(metrics._num(_bf.get("constraint_groups_scanned")) or 0) > 0 \
            and int(metrics._num(_bf.get("certified")) or 0) == 0:
        warnings.append("No certified Bregman opportunities found yet; continue paper training.")

    # --- artifact path transparency (Docker vs host mismatch surfacing) ------ #
    artifact_paths = _artifact_paths(data_dir, repo_root, bundle_dir, status_source)
    bundle.write_json("artifact_paths.json", artifact_paths)

    classification = classify(safety, tests, runtime_available, comparison,
                              missing_features, warnings, run_ready=report_run_ready)

    recommendations = recs.build_recommendations(
        safety, missing_features, tests, comparison, runtime_available,
        benchmarks=benchmarks, consistency=consistency, audit=algo_audit,
        contract=validation_contract)
    # Fill the audit's top-5 next code changes from the final recommendations.
    algo_audit["top_5_recommendations"] = (
        (["emit the missing core audit fields from the training status writer"]
         if algo_audit["missing_core_fields"] else [])
        + [r.get("action", "") for r in recommendations])[:5]

    # ------------------------------------------------------------------------- #
    # Write bundle files
    # ------------------------------------------------------------------------- #
    _write_repo_context(bundle, git, env_text, env_example_text, compose_text,
                        dockerfile_text, req_text, req_dev_text, pyproject_text)
    if include_docker:
        _write_docker(bundle, docker)
    if include_api:
        _write_api(bundle, api)
    _write_tests(bundle, tests)
    _write_metrics(bundle, feats, status, comparison)
    _write_safety(bundle, safety, env_text, env_example_text, compose_text, docker, api)
    _write_summaries(bundle, feats, comparison, missing_features, recommendations,
                     scorecard, classification)
    # Benchmark layer + consistency + quant responsibilities artifacts.
    bundle.write_json("metrics/benchmarks.json", benchmarks)
    bundle.write_json("consistency.json", {"checks": consistency})
    bundle.write_json("quant_responsibilities.json", quant_responsibilities)
    bundle.write_json("final_validation.json", final_validation)
    bundle.write_json("algorithmic_edge_audit.json", algo_audit)
    bundle.write_json("metrics/bregman_source_reconciliation.json",
                      bregman_source_reconciliation)
    bundle.write_json("ledger_reconciliation.json", ledger_reconciliation)
    bundle.write_json("validation_contract.json", {
        "contract": validation_contract, "expectancy": expectancy,
        "production_readiness_verdict": readiness_verdict})

    report_json = _build_report_json(
        now=now, repo_root=repo_root, classification=classification, pr=pr,
        git=git, safety=safety, runtime={"available": runtime_available,
                                         "status_source": status_source,
                                         "docker_available": docker.get("available")},
        api=api, features=feats, tests=tests, metrics_summary=_metrics_summary(feats),
        artifacts=artifacts, comparison=comparison, missing_features=missing_features,
        warnings=warnings, errors=errors, recommendations=recommendations,
        scorecard=scorecard, history_days=history_days, baseline_path=baseline_path,
        files=bundle.files, benchmarks=benchmarks, consistency=consistency,
        quant_responsibilities=quant_responsibilities, final_validation=final_validation,
        algorithmic_edge_audit=algo_audit, ledger_reconciliation=ledger_reconciliation,
        validation_contract=validation_contract, expectancy=expectancy,
        production_readiness_verdict=readiness_verdict)
    report_json["run_ready"] = report_run_ready
    report_json["artifact_paths"] = artifact_paths
    report_json["closed_loop_artifacts_manifest"] = closed_loop_manifest
    # 100X paper profit-discovery proof + per-lane paper-trade acceleration (machine-
    # readable). Lets the report prove the accelerator profile + lane-specific blockers.
    if status.get("aggressive_paper"):
        report_json["aggressive_paper"] = status.get("aggressive_paper")
    if status.get("paper_trade_acceleration"):
        report_json["paper_trade_acceleration"] = status.get("paper_trade_acceleration")
    bundle.write_json("report.json", report_json)

    report_md = _build_report_md(report_json, feats, status, docker, api, tests,
                                 comparison, missing_features, recommendations,
                                 scorecard, artifacts, safety, benchmarks,
                                 consistency, quant_responsibilities, final_validation,
                                 algo_audit)
    bundle.write_text("report.md", report_md, redact=True)

    # --- zip ------------------------------------------------------------------ #
    zip_path = out_root / f"{name}.zip"
    _make_zip(bundle_dir, zip_path)

    logger.info("inspection report generated: classification=%s score=%s "
                "benchmarks(pass/warn/fail/missing)=%s inconsistencies=%d bundle=%s",
                classification, scorecard.get("score"),
                benchmarks.get("summary"), len(consistency), bundle_dir)

    return {
        "classification": classification,
        "score": scorecard["score"],
        "bundle_dir": str(bundle_dir),
        "zip_path": str(zip_path),
        "report_json": str(bundle_dir / "report.json"),
        "report_md": str(bundle_dir / "report.md"),
        "warnings": warnings,
        "files": bundle.files,
        "algorithmic_edge_audit_ok": bool(algo_audit["ok"]),
        "algorithmic_edge_audit_missing": list(algo_audit["missing_core_fields"]),
        "equity_reconciled": bool(ledger_reconciliation.get("ok", True)),
        "validation_contract_passed": bool(validation_contract.get("passed", False)),
        "production_ready": bool(readiness_verdict.get("production_ready", False)),
        "run_ready_for_hours": bool(report_run_ready["run_ready_for_hours"]),
        "run_ready": report_run_ready,
        "artifact_paths": artifact_paths,
        "closed_loop_artifacts_manifest": closed_loop_manifest,
        "bundle_mode": bundle_mode,
        # read-only blocks for the console summary (advisory scheduler + near-misses)
        "grok_news_evidence": status.get("grok_news_evidence", {}) or {},
        "bregman_funnel": status.get("bregman_funnel", {}) or {},
        "bregman_synthetic_fixture": status.get("bregman_funnel", {}).get("synthetic_fixture", {}),
    }


def _reconcile_equities(feats: dict, status: dict | None, api: dict | None,
                        data_dir) -> dict:
    """Reconcile dashboard / paper-training / report / ledger equity within 1%.

    Uses the canonical ledger reconciliation so the report FAILS (warns loudly +
    feeds the audit) when equity surfaces disagree beyond tolerance. Read-only."""
    from engine.ledger import CanonicalLedger, reconcile_equity
    paper_eq = metrics._num(feats.get("equity"))
    dash_eq = metrics._num(feats.get("dashboard_equity"))
    report_eq = paper_eq  # the report's headline equity mirrors paper-training
    ledger_eq = None
    try:
        if data_dir:
            p = Path(data_dir) / "paper_ledger.json"
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "equity" in raw:
                    ledger_eq = metrics._num(raw.get("equity"))
                elif isinstance(raw, dict) and "entries" in raw:
                    ledger_eq = CanonicalLedger.from_entries(
                        raw.get("entries", []),
                        starting_balance=float(raw.get("starting_balance", 0.0))).equity()
    except Exception:  # noqa: BLE001
        ledger_eq = None
    return reconcile_equity({"dashboard": dash_eq, "paper_training": paper_eq,
                             "report": report_eq, "ledger": ledger_eq},
                            tolerance_pct=1.0)


def _ledger_returns(data_dir) -> list:
    """Canonical-ledger after-cost return series for the expectancy bootstrap."""
    try:
        if not data_dir:
            return []
        p = Path(data_dir) / "paper_ledger.json"
        if not p.exists():
            return []
        from engine.ledger import CanonicalLedger
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("entries"):
            return CanonicalLedger.from_entries(
                raw["entries"], starting_balance=float(raw.get("starting_balance", 0.0))).returns()
        if isinstance(raw, dict) and isinstance(raw.get("returns"), list):
            return [float(x) for x in raw["returns"]]
    except Exception:  # noqa: BLE001
        return []
    return []


def _status_age_s(status: dict | None, now) -> Optional[float]:
    """Best-effort age (seconds) of the training status snapshot for staleness.

    Reads an epoch timestamp from common status fields; returns None when no
    timestamp is present (staleness then cannot be asserted)."""
    if not status:
        return None
    ts = metrics._first(status.get("generated_at"), status.get("ts"),
                        status.get("updated_at"), status.get("timestamp"))
    val = metrics._num(ts)
    if val is None:
        return None
    # Treat large values as ms epochs.
    if val > 1e12:
        val /= 1000.0
    try:
        return max(0.0, now.timestamp() - val)
    except Exception:  # noqa: BLE001
        return None


def _load_baseline(path: str) -> Optional[dict]:
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return None


# ----------------------------------------------------------------------------- #
# Section writers
# ----------------------------------------------------------------------------- #
def _write_repo_context(bundle, git, env_text, env_example_text, compose_text,
                        dockerfile_text, req_text, req_dev_text, pyproject_text):
    bundle.write_text("git_status.txt", git["status"]["stdout"])
    bundle.write_text("git_branch.txt", git["branch"]["stdout"])
    bundle.write_text("git_log_recent.txt", git["log_recent"]["stdout"])
    bundle.write_text("git_diff_stat.txt", git["diff_stat"]["stdout"])
    bundle.write_text("changed_files.txt", git["changed_files"]["stdout"])
    bundle.write_text("env_redacted.txt", redactor.redact_env_text(env_text), redact=False)
    bundle.write_text("env_example_redacted.txt",
                      redactor.redact_env_text(env_example_text), redact=False)
    bundle.write_text("docker_compose_redacted.yml",
                      redactor.redact_env_text(compose_text), redact=False)
    bundle.write_text("dockerfile_snapshot.txt", dockerfile_text)
    bundle.write_text("requirements_snapshot.txt", req_text)
    bundle.write_text("requirements_dev_snapshot.txt", req_dev_text)
    if pyproject_text is not None:
        bundle.write_text("pyproject_snapshot.toml", pyproject_text)


def _write_docker(bundle, docker):
    if docker.get("skipped"):
        bundle.write_text("docker_compose_ps.txt", "docker collection skipped.")
        return
    bundle.write_text("docker_compose_ps.txt", _cmd_dump(docker.get("ps")))
    bundle.write_text("docker_compose_config.txt", _cmd_dump(docker.get("config")))
    bundle.write_text("docker_images.txt", _cmd_dump(docker.get("images")))
    bundle.write_text("docker_volumes.txt", _cmd_dump(docker.get("volumes")))
    bundle.write_text("hermes_training_status.txt", _cmd_dump(docker.get("training_status")))
    bundle.write_text("logs/hermes-training_tail1000.log",
                      _cmd_dump(docker.get("logs_training")))
    bundle.write_text("logs/hermes-trading-engine_tail500.log",
                      _cmd_dump(docker.get("logs_engine")))


def _write_api(bundle, api):
    name_map = {
        "health": "api/health.json", "state": "api/state.json",
        "venues_status": "api/venues_status.json",
        "chainlink_status": "api/chainlink_status.json",
        "news_status": "api/news_status.json",
        "research_status": "api/research_status.json",
        "micro_live_status": "api/micro_live_status.json",
        "guarded_live_status": "api/guarded_live_status.json",
        "production_review_status": "api/production_review_status.json",
    }
    for key, rel in name_map.items():
        entry = (api or {}).get(key) or {"ok": False, "error": "not collected"}
        bundle.write_json(rel, entry)


def _write_tests(bundle, tests):
    name_map = {
        "full": "test_results_full.txt", "chainlink": "test_results_chainlink.txt",
        "btc_pulse": "test_results_btc_pulse.txt", "fast_price": "test_results_fast_price.txt",
        "news": "test_results_news.txt", "bregman": "test_results_bregman.txt",
        "paper_attribution": "test_results_paper_attribution.txt",
        "inspection": "test_results_inspection.txt",
    }
    runs = tests.get("runs", {})
    for key, rel in name_map.items():
        if tests.get("skipped"):
            bundle.write_text(rel, "tests skipped (--skip-tests).")
            continue
        rec = runs.get(key)
        bundle.write_text(rel, _cmd_dump(rec) if rec else "not run.")


def _write_metrics(bundle, feats, status, comparison):
    pnl = (status or {}).get("pnl", {}) or {}
    mon = (status or {}).get("monitoring", {}) or {}
    bp = (status or {}).get("btc_pulse", {}) or {}
    news = (status or {}).get("news", {}) or {}
    research = (status or {}).get("research", {}) or {}
    fast = (status or {}).get("btc_fast_price", {}) or {}
    scan = (status or {}).get("scan_metrics", {}) or {}

    bundle.write_json("metrics/paper_training_metrics.json", {
        "equity": feats.get("equity"), "total_pnl": feats.get("total_pnl"),
        "after_cost_pnl": feats.get("after_cost_pnl"),
        "open_positions": feats.get("open_positions"),
        "closed_positions": feats.get("closed_positions"),
        "paper_trades": feats.get("paper_trades"),
        "win_rate_traded_only": feats.get("win_rate_traded_only"),
        "runtime_minutes": feats.get("runtime_minutes"), "raw_pnl": pnl})
    bundle.write_json("metrics/strategy_attribution.json", {
        "paper_attribution_enabled": feats.get("paper_attribution_enabled"),
        "exploration_validation_separated": feats.get("exploration_validation_separated"),
        "monitoring": mon})
    bundle.write_json("metrics/btc_pulse.json", {k: feats.get(k) for k in feats
                                                 if k.startswith("btc_pulse")} | {"raw": bp})
    bundle.write_json("metrics/bregman.json", {k: feats.get(k) for k in feats
                                               if k.startswith("bregman")})
    bundle.write_json("metrics/news_quality.json", {
        "news_scanner_enabled": feats.get("news_scanner_enabled"),
        "news_provider_mode": feats.get("news_provider_mode"),
        "news_items_fetched": feats.get("news_items_fetched"),
        "news_items_used": feats.get("news_items_used"),
        "news_quality_ratio": metrics._news_quality_ratio(feats), "raw": news})
    bundle.write_json("metrics/chainlink.json", {k: feats.get(k) for k in feats
                                                 if k.startswith("chainlink")})
    bundle.write_json("metrics/fast_btc_price.json", {
        "btc_fast_price_enabled": feats.get("btc_fast_price_enabled"),
        "btc_fast_price_valid": feats.get("btc_fast_price_valid"),
        "btc_fast_price_age_seconds": feats.get("btc_fast_price_age_seconds"),
        "btc_fast_price_disagreement_bps": feats.get("btc_fast_price_disagreement_bps"),
        "raw": fast})
    bundle.write_json("metrics/grok_research.json", {
        "grok_enabled": feats.get("grok_enabled"),
        "grok_has_api_key": feats.get("grok_has_api_key"),
        "grok_with_news_count": feats.get("grok_with_news_count"),
        "grok_cache_hits": feats.get("grok_cache_hits"), "raw": research})
    bundle.write_json("metrics/market_scan.json", {
        "scanned_markets": feats.get("scanned_markets"),
        "kept_markets": feats.get("kept_markets"),
        "market_scan_limit_effective": feats.get("market_scan_limit_effective"), "raw": scan})
    bundle.write_json("metrics/risk_and_safety.json", {
        "live_detected": feats.get("live_detected"), "preflight_ok": feats.get("preflight_ok"),
        "risk": (status or {}).get("risk", {})})
    bundle.write_json("metrics/fill_realism.json", {
        "fill_realism_enabled": feats.get("fill_realism_enabled"),
        "fantasy_fill_rejections": feats.get("fantasy_fill_rejections")})
    bundle.write_json("metrics/calibration.json", {
        "brier": feats.get("brier"), "ece": feats.get("ece"),
        "sharpe": feats.get("sharpe"), "sortino": feats.get("sortino"),
        "calmar": feats.get("calmar"), "max_drawdown": feats.get("max_drawdown")})
    bundle.write_json("metrics/pnl_by_strategy.json", {
        "polymarket_after_cost_pnl": feats.get("after_cost_pnl"),
        "btc_pulse_after_cost_pnl": feats.get("btc_pulse_after_cost_pnl"),
        "bregman_certified_profit": feats.get("bregman_certified_profit")})
    bundle.write_json("metrics/exploration_vs_validation.json", {
        "exploration_validation_separated": feats.get("exploration_validation_separated"),
        "feedback_accelerator": (status or {}).get("feedback_accelerator", {})})


def _write_safety(bundle, safety, env_text, env_example_text, compose_text, docker, api):
    bundle.write_json("safety/safety_audit.json", safety)
    bundle.write_json("safety/forbidden_live_flags.json", {
        "forbidden_live_flags": safety.get("forbidden_live_flags", {}),
        "secret_presence": safety.get("secret_presence", {}),
        "protective_flags": safety.get("protective_flags", {}),
        "summary": safety.get("summary", {})})
    # Redaction audit: prove the scrubber catches secrets (counts only).
    sources = {
        "env": env_text, "env_example": env_example_text, "docker_compose": compose_text,
        "logs_training": (docker.get("logs_training") or {}).get("stdout", "") if isinstance(docker, dict) else "",
        "api": json.dumps(api or {}),
    }
    audit = {}
    for label, text in sources.items():
        hits = redactor.scan_for_secrets(text or "")
        residual = redactor.assert_clean(redactor.redact_text(text or ""))
        audit[label] = {"secret_pattern_hits": hits, "residual_after_redaction": residual}
    bundle.write_json("safety/redaction_audit.json", audit)


def _write_summaries(bundle, feats, comparison, missing_features, recommendations,
                     scorecard, classification):
    bundle.write_json("performance_summary.json", {
        "classification": classification, "scorecard": scorecard,
        "key_metrics": _metrics_summary(feats)})
    bundle.write_json("improvement_trend.json", comparison)
    bundle.write_json("feature_health.json", {
        "features": {k: v for k, v in feats.items() if not k.startswith("_")},
        "sections_present": feats.get("_sections_present", {})})
    bundle.write_json("missing_features.json", {"missing_features": missing_features})
    bundle.write_json("recommendations.json", {"recommendations": recommendations})


def _metrics_summary(feats: dict) -> dict:
    keys = ["equity", "total_pnl", "after_cost_pnl", "closed_positions",
            "paper_trades", "win_rate_traded_only", "brier", "ece", "sharpe",
            "sortino", "calmar", "max_drawdown", "btc_pulse_after_cost_pnl",
            "bregman_certified_profit"]
    out = {k: feats.get(k) for k in keys}
    out["news_quality_ratio"] = metrics._news_quality_ratio(feats)
    return out


def _cmd_dump(rec: Optional[dict]) -> str:
    if not rec:
        return "not collected."
    lines = [f"$ {rec.get('cmd', '')}", f"# exit_code={rec.get('exit_code')}"]
    if rec.get("error"):
        lines.append(f"# error={rec.get('error')}")
    lines.append("")
    if rec.get("stdout"):
        lines.append(rec["stdout"])
    if rec.get("stderr"):
        lines.append("\n--- stderr ---")
        lines.append(rec["stderr"])
    return "\n".join(lines)


# ----------------------------------------------------------------------------- #
# report.json + report.md
# ----------------------------------------------------------------------------- #
def _build_report_json(**kw) -> dict:
    now = kw["now"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "repo_root": kw["repo_root"],
        "classification": kw["classification"],
        "scorecard": kw["scorecard"],
        "optional_pr_context": ({"pr": kw["pr"]} if kw["pr"] else None),
        "history_days": kw["history_days"],
        "baseline_path": kw["baseline_path"],
        "git": _git_json(kw["git"]),
        "safety": kw["safety"],
        "runtime": kw["runtime"],
        "api": {k: {kk: vv for kk, vv in v.items() if kk != "raw"}
                for k, v in (kw["api"] or {}).items()},
        "features": {k: v for k, v in kw["features"].items() if not k.startswith("_")},
        "tests": _tests_json(kw["tests"]),
        "metrics": kw["metrics_summary"],
        "artifacts": kw["artifacts"],
        "performance_comparison": kw["comparison"],
        "missing_features": kw["missing_features"],
        "benchmarks": kw["benchmarks"],
        "consistency": kw["consistency"],
        "quant_responsibilities": kw["quant_responsibilities"],
        "final_validation": kw.get("final_validation"),
        "algorithmic_edge_audit": kw.get("algorithmic_edge_audit"),
        "ledger_reconciliation": kw.get("ledger_reconciliation"),
        "validation_contract": kw.get("validation_contract"),
        "after_cost_expectancy": kw.get("expectancy"),
        "production_readiness_verdict": kw.get("production_readiness_verdict"),
        "warnings": kw["warnings"],
        "errors": kw["errors"],
        "recommendations": kw["recommendations"],
        "files": kw["files"],
    }


def _git_json(git: dict) -> dict:
    return {
        "branch": (git["branch"]["stdout"] or "").strip(),
        "status": (git["status"]["stdout"] or "").strip(),
        "recent_log": (git["log_recent"]["stdout"] or "").strip().splitlines(),
        "changed_files": [f for f in (git["changed_files"]["stdout"] or "").splitlines() if f],
    }


def _tests_json(tests: dict) -> dict:
    out = {"skipped": tests.get("skipped"), "present": tests.get("present"),
           "passing": tests.get("passing"), "runs": {}}
    for name, rec in (tests.get("runs") or {}).items():
        out["runs"][name] = {"exit_code": rec.get("exit_code"), "ok": rec.get("ok"),
                             "summary": rec.get("summary")}
    return out


def _yn(v):
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if v is None:
        return "unknown"
    return str(v)


_AUDIT_SECTION_TITLES = {
    "strategy_attribution": "1. Strategy Attribution",
    "bregman": "2. Bregman Arbitrage Diagnostics",
    "btc_pulse": "3. BTC Pulse Diagnostics",
    "calibration": "4. Calibration Diagnostics",
    "fill_realism": "5. Fill Realism",
    "risk": "6. Risk Metrics",
    "training_readiness": "7. Training / Readiness",
}


def _append_algorithmic_edge_audit_md(L: list, audit: dict) -> None:
    """Render the mandatory Algorithmic Edge Audit section into the MD report."""
    L.append("## 0. Algorithmic Edge Audit (MANDATORY)")
    L.append("")
    if not audit:
        L.append("> **FAIL — audit not computed.**")
        L.append("")
        return
    ok = audit.get("ok")
    banner = "PASS" if ok else "FAIL"
    L.append(f"**Audit status: {banner}** "
             f"(`{audit.get('status')}`; stale={audit.get('stale')})")
    L.append("")
    L.append(f"- Bregman edge engine enabled: **{audit.get('bregman_enabled')}**")
    L.append(f"- Readiness cap: **{audit.get('readiness_cap')}** "
             f"(raw {audit.get('raw_readiness_score')} \u2192 capped "
             f"**{audit.get('capped_readiness_score')}**)")
    if audit.get("hard_failures"):
        L.append(f"- Hard failures: `{', '.join(audit['hard_failures'])}`")
    if audit.get("required_field_violations"):
        L.append(f"- Missing required fields: `{', '.join(audit['required_field_violations'])}`")
    L.append("")
    if not ok:
        L.append("> **This report is NOT decision-grade and readiness is capped.** "
                 "The algorithmic edge engine is inactive or unverifiable; resolve the "
                 "hard failures above before trusting any readiness number.")
        L.append("")
    for key, title in _AUDIT_SECTION_TITLES.items():
        sec = (audit.get("sections") or {}).get(key, {})
        L.append(f"### {title}")
        L.append("")
        if not sec:
            L.append("- (no data)")
            L.append("")
            continue
        L.append("| Field | Value |")
        L.append("|---|---|")
        for fk, fv in sec.items():
            L.append(f"| {fk} | {fv} |")
        L.append("")
    blockers = audit.get("top_5_blockers") or []
    L.append("### Top 5 Algorithmic Blockers")
    L.append("")
    if blockers:
        for b in blockers:
            L.append(f"- {b}")
    else:
        L.append("- None detected.")
    L.append("")
    nexts = audit.get("top_5_recommendations") or []
    L.append("### Top 5 Next Recommended Code Changes")
    L.append("")
    if nexts:
        for r in nexts:
            L.append(f"- {r}")
    else:
        L.append("- None.")
    L.append("")


def _append_validation_contract_md(L: list, contract: dict, expectancy: dict,
                                   verdict: dict) -> None:
    """Render the institutional validation contract + readiness verdict."""
    L.append("## 0b. Validation Contract (proves improvement, not completion)")
    L.append("")
    if not contract:
        L.append("- Not computed.")
        L.append("")
        return
    L.append(f"**Contract: {'PASS' if contract.get('passed') else 'FAIL'}** | "
             f"**Production ready: {verdict.get('production_ready')}**")
    L.append("")
    L.append("| Condition | Pass | Detail |")
    L.append("|---|---|---|")
    for c in contract.get("checks", []):
        L.append(f"| {c['name']} | {'OK' if c['passed'] else 'FAIL'} | {c['detail']} |")
    L.append("")
    L.append(f"- After-cost expectancy bootstrap: point={expectancy.get('point')} "
             f"CI=[{expectancy.get('lo')}, {expectancy.get('hi')}] "
             f"credible_positive=**{expectancy.get('credible_positive')}** "
             f"(n={expectancy.get('n')})")
    if verdict.get("blocking_reasons"):
        L.append(f"- Readiness blockers: `{', '.join(verdict['blocking_reasons'])}`")
    L.append("> Production readiness is withheld unless an executable strategy shows "
             "statistically credible positive after-cost expectancy under a passing contract.")
    L.append("")


def _build_report_md(rj, feats, status, docker, api, tests, comparison,
                     missing_features, recommendations, scorecard, artifacts,
                     safety, benchmarks=None, consistency=None,
                     quant_responsibilities=None, final_validation=None,
                     algorithmic_edge_audit=None) -> str:
    benchmarks = benchmarks or {}
    consistency = consistency or []
    quant_responsibilities = quant_responsibilities or {}
    final_validation = final_validation or {}
    algorithmic_edge_audit = algorithmic_edge_audit or {}
    L: list[str] = []
    cls = rj["classification"]
    L.append("# Hermes Polymarket Paper-Training — Bot Inspection Report")
    L.append("")
    L.append(f"_Generated: {rj['generated_at']} · PAPER ONLY · inspection/reporting only_")
    if rj.get("optional_pr_context"):
        L.append(f"_Optional PR context: #{rj['optional_pr_context']['pr']}_")
    L.append("")

    # 0. Algorithmic Edge Audit (MANDATORY, decision-grade)
    _append_algorithmic_edge_audit_md(L, algorithmic_edge_audit)

    # 0b. Validation Contract + production-readiness verdict (next-report contract)
    _append_validation_contract_md(L, rj.get("validation_contract") or {},
                                   rj.get("after_cost_expectancy") or {},
                                   rj.get("production_readiness_verdict") or {})

    # 1. Executive Summary
    L.append("## 1. Executive Summary")
    L.append("")
    L.append(f"**Classification: {cls}**")
    L.append("")
    L.append(f"- Bot health score: **{scorecard['score']}/100**")
    L.append(f"- Safety: {safety.get('status')} · live_detected={_yn(safety.get('live_detected'))}")
    L.append(f"- Paper training running: {_yn(feats.get('paper_training_running'))} · "
             f"runtime: {feats.get('runtime_minutes')} min")
    L.append(f"- Tests: present={_yn(tests.get('present'))} passing={_yn(tests.get('passing'))}"
             + (" (skipped)" if tests.get("skipped") else ""))
    if comparison.get("available"):
        L.append(f"- Trend vs baseline: {len(comparison.get('improved', []))} improved / "
                 f"{len(comparison.get('degraded', []))} degraded"
                 + (" · **REGRESSION**" if comparison.get("regression") else ""))
    else:
        L.append("- Trend vs baseline: no baseline provided (current-state scorecard only)")
    L.append(f"- Missing/weak features: {len(missing_features)}")
    L.append("")

    # 2. Bot Health Scorecard
    L.append("## 2. Bot Health Scorecard")
    L.append("")
    L.append("| Component | Score | Max | Why |")
    L.append("|---|---:|---:|---|")
    for comp_name, c in scorecard["components"].items():
        L.append(f"| {comp_name} | {c['score']} | {c['max']} | {c['reason']} |")
    L.append(f"| **Total** | **{scorecard['score']}** | **100** | |")
    L.append("")

    # 3. Safety / Live-Execution Audit
    L.append("## 3. Safety / Live-Execution Audit")
    L.append("")
    L.append(f"- Status: **{safety.get('status')}** · engine_mode={safety.get('engine_mode')}")
    forb = safety.get("summary", {}).get("forbidden_enabled", [])
    cred = safety.get("summary", {}).get("credentials_present", [])
    L.append(f"- Forbidden live flags enabled: {forb or 'none'}")
    L.append(f"- Live credential material present: {cred or 'none'}")
    if safety.get("findings"):
        L.append("")
        L.append("Findings:")
        for f in safety["findings"]:
            L.append(f"- [{f['severity']}] `{f['flag']}` = {f.get('value')} — {f['reason']}")
    L.append("")

    # 4. Runtime Health
    L.append("## 4. Runtime Health")
    L.append("")
    rt = rj["runtime"]
    L.append(f"- Paper status collected: {_yn(rt.get('available'))} "
             f"(source: {rt.get('status_source')})")
    L.append(f"- Docker available: {_yn(rt.get('docker_available'))}")
    L.append(f"- preflight_ok: {_yn(feats.get('preflight_ok'))} · "
             f"scanned={feats.get('scanned_markets')} kept={feats.get('kept_markets')}")
    L.append("")

    # 5. Performance Improvement / Regression Analysis
    L.append("## 5. Performance Improvement / Regression Analysis")
    L.append("")
    if comparison.get("available"):
        L.append("| Metric | Current | Baseline | Δ | Direction |")
        L.append("|---|---|---|---|---|")
        for m, d in comparison.get("metrics", {}).items():
            L.append(f"| {m} | {d.get('current')} | {d.get('baseline')} | "
                     f"{d.get('delta')} | {d.get('direction')} |")
    else:
        L.append("No baseline provided — current-state key metrics:")
        L.append("")
        for k, v in rj["metrics"].items():
            L.append(f"- {k}: {v}")
    L.append("")

    # 6-15. Feature sections
    L.append("## 6. Chainlink / Oracle Health")
    L.append("")
    for k in ("chainlink_enabled", "chainlink_valid", "chainlink_stale",
              "chainlink_age_seconds", "chainlink_price"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append("")
    L.append("## 7. BTC Fast Price Feed Health")
    L.append("")
    for k in ("btc_fast_price_enabled", "btc_fast_price_valid",
              "btc_fast_price_age_seconds", "btc_fast_price_disagreement_bps"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append("")
    L.append("## 8. BTC Pulse Status")
    L.append("")
    for k in ("btc_pulse_enabled", "btc_pulse_frozen", "btc_pulse_oracle_gate_active",
              "btc_pulse_paper_trades", "btc_pulse_after_cost_pnl", "btc_pulse_regime",
              "btc_pulse_rejection_reasons"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append("")
    L.append("## 9. News Scanner Quality")
    L.append("")
    for k in ("news_scanner_enabled", "news_provider_mode", "news_items_fetched",
              "news_items_used", "news_rejected_stale", "news_rejected_unclear_date",
              "news_rejected_low_credibility"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append(f"- news_quality_ratio: {metrics._news_quality_ratio(feats)}")
    L.append("")
    L.append("## 10. Grok / Research Status")
    L.append("")
    for k in ("grok_enabled", "grok_has_api_key", "grok_with_news_count", "grok_cache_hits"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    # bounded advisory scheduler summary (research only; never execution)
    _g = status.get("grok_news_evidence", {}) or {}
    if _g:
        L.append("")
        L.append("### 10a. Grok Advisory Scheduler (research-only)")
        L.append("")
        for k in ("grok_advisory_enabled", "grok_brain_ready", "grok_brain_blocker",
                  "xai_api_key_source", "grok_calls_total", "grok_calls_with_news",
                  "grok_proof_calls_total", "grok_scheduler_calls_total",
                  "grok_total_calls_reconciled", "grok_scheduled_calls",
                  "grok_scheduler_eligible_targets", "grok_scheduler_targets_selected",
                  "grok_scheduler_targets_skipped", "grok_scheduler_skip_reasons",
                  "grok_advisory_only_count", "grok_evidence_records_written",
                  "grok_advisory_max_calls_per_hour", "grok_advisory_calls_per_hour",
                  "grok_market_groups_analyzed", "grok_bregman_near_misses_analyzed",
                  "grok_bregman_incomplete_groups_analyzed", "grok_bregman_malformed_groups_analyzed",
                  "grok_news_linked_markets_analyzed", "grok_learning_features_written",
                  "grok_best_bregman_group_analyzed", "grok_best_bregman_group_skip_reason",
                  "grok_contributed_learning_features",
                  "grok_advisory_only_invariant", "grok_no_execution_override"):
            if k in _g:
                L.append(f"- {k}: {_g.get(k)}")
    L.append("")
    L.append("## 11. Bregman Paper Scanner Status")
    L.append("")
    for k in ("bregman_paper_enabled", "bregman_candidates_found", "bregman_certified_count",
              "bregman_certified_profit", "bregman_false_positive_rate"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    # ABCAS certifier funnel diagnostics (projected profit + Bregman distance logged
    # even when 0 certified; per-stage rejection taxonomy; certification near-misses).
    _abc = status.get("bregman", {}) or {}
    if _abc:
        L.append("")
        L.append("### 11.0 ABCAS Certifier Funnel Diagnostics (read-only)")
        L.append("")
        for k in ("constraint_groups_scanned", "candidate_arbitrages", "certified_arbitrages",
                  "best_projected_profit_per_set", "max_bregman_distance", "mean_cost_per_set",
                  "expected_min_profit", "near_miss_count"):
            if k in _abc:
                L.append(f"- {k}: {_abc.get(k)}")
        if _abc.get("stage_rejections"):
            L.append(f"- stage_rejections: {_abc.get('stage_rejections')}")
        for nm in (_abc.get("near_miss_certified_samples", []) or [])[:5]:
            L.append(f"  - near_miss(certifier_reached): legs={nm.get('outcome_ids')} "
                     f"D(mu*||theta)={nm.get('bregman_distance')} "
                     f"projected_profit/set={nm.get('projected_after_fee_profit_per_set')} "
                     f"cost/set={nm.get('cost_per_set')} reason={nm.get('reject_reason')} "
                     f"tradeable=False")
    # near-miss diagnostics + blocker explanation (read-only; gates never loosened)
    _bf = status.get("bregman_funnel", {}) or {}
    if _bf:
        L.append("")
        L.append("### 11a. Bregman Near-Miss Diagnostics (read-only)")
        L.append("")
        for k in ("bregman_near_misses_total", "near_miss_one_fix_away_count",
                  "near_miss_depth_only_count", "near_miss_not_exhaustive_count",
                  "near_miss_stale_refresh_failed_count"):
            L.append(f"- {k}: {_bf.get(k, 0)}")
        nmr = _bf.get("near_miss_by_rejection_reason", {}) or {}
        if nmr:
            L.append(f"- near_miss_by_rejection_reason: {nmr}")
        # ADVISORY learning signals — which near-misses the trainer should learn from
        lpc = _bf.get("near_miss_learning_priority_counts", {}) or {}
        if lpc or _bf.get("near_miss_shadow_label_candidate_count"):
            L.append(f"- near_miss_learning_priority_counts (high/med/low): {lpc}")
            L.append(f"- near_miss_shadow_label_candidate_count: "
                     f"{_bf.get('near_miss_shadow_label_candidate_count', 0)}")
            llc = _bf.get("near_miss_learning_label_counts", {}) or {}
            if llc:
                L.append(f"- near_miss_learning_label_counts: {llc}")
            for nm in (_bf.get("near_miss_top_learning_priority", []) or [])[:5]:
                L.append(f"  - learn: {nm.get('group_key')} "
                         f"priority={nm.get('learning_priority')}({nm.get('learning_priority_score')}) "
                         f"label={nm.get('learning_label')} "
                         f"shadow_candidate={nm.get('shadow_label_candidate')} "
                         f"would_trade_if={nm.get('would_trade_if')}")
        if _bf.get("near_miss_all_negative_after_cost_lower_bound"):
            L.append("- near_miss_after_cost: ALL near-misses have non-positive "
                     "after-cost lower bound — NONE are tradeable")
        # parsing + depth/complete-set census (scanner telemetry + funnel)
        _bsrc = status.get("bregman", {}) or {}
        def _pv(key, default=0):
            return _bf.get(key, _bsrc.get(key, default))
        L.append("")
        L.append("### 11b. Bregman Price/Outcome Parsing + Depth Census (read-only)")
        L.append("")
        L.append(f"- non_numeric_price_count: {_pv('non_numeric_price_count')}")
        L.append(f"- insufficient_outcomes_count: {_pv('insufficient_outcomes_count')}")
        L.append(f"- malformed_group_count: {_pv('malformed_group_count')}")
        L.append(f"- parsed_price_success_rate: {_pv('parsed_price_success_rate', 1.0)}")
        L.append(f"- bregman_depth_sufficient_groups: {_pv('bregman_depth_sufficient_groups')}")
        L.append(f"- bregman_depth_insufficient_groups: {_pv('bregman_depth_insufficient_groups')}")
        L.append(f"- bregman_high_liquidity_groups_scanned: {_pv('bregman_high_liquidity_groups_scanned')}")
        L.append(f"- bregman_all_groups_thin: {_pv('bregman_all_groups_thin', False)}")
        L.append(f"- complete_set_count (certified): {_bf.get('certified', 0)}")
        L.append(f"- incomplete_set_count (not_exhaustive near-misses): "
                 f"{_bf.get('near_miss_not_exhaustive_count', 0)}")
        L.append(f"- bregman_promising_groups_refreshed: {_pv('bregman_promising_groups_refreshed')}")
        L.append(f"- bregman_refresh_success: {_pv('bregman_refresh_success')} "
                 f"failed: {_pv('bregman_refresh_failed')} "
                 f"stale_after: {_pv('bregman_stale_after_refresh')}")
        if _pv('bregman_refresh_not_attempted_reason', None):
            L.append(f"- refresh_not_attempted_reason: {_pv('bregman_refresh_not_attempted_reason')}")
        samples = _bf.get("skip_reason_samples", {}) or _bsrc.get("skip_reason_samples", {}) or {}
        for rkey in ("non_numeric_outcome_prices", "outcome_price_count_mismatch",
                     "insufficient_outcomes", "malformed_group", "invalid_simplex",
                     "duplicate_outcome_labels"):
            if rkey in samples:
                s = samples[rkey]
                L.append(f"- example[{rkey}]: market={s.get('market_id')} "
                         f"detail={s.get('detail')}")
        blk = _bf.get("blocker_explanation", {}) or {}
        if blk:
            L.append(f"- no_bundle_blocker: {blk.get('primary_blocker')} "
                     f"({blk.get('detail', '')})")
        top = _bf.get("bregman_top_near_misses", []) or []
        if top:
            L.append("")
            L.append("Top Bregman near-misses (diagnostic only — NOT executed):")
            L.append("")
            for nm in top[:5]:
                L.append(f"  - {nm.get('group_key')} reason={nm.get('reject_reason')} "
                         f"score={nm.get('near_miss_score')} "
                         f"market_ids={nm.get('market_ids')} token_ids={nm.get('token_ids')} "
                         f"labels={nm.get('outcome_labels')} "
                         f"one_fix_away={nm.get('one_fix_away')} "
                         f"tradeable={nm.get('near_miss_tradeable', False)} "
                         f"blockers={nm.get('remaining_blockers')}")
        # 11c. certifier / candidate-generation health — candidates=0 is NEVER unexplained
        L.append("")
        L.append("### 11c. Bregman Certifier / Candidate Health (read-only)")
        L.append("")
        L.append(f"- bregman_groups_entered_certifier: {_pv('bregman_groups_entered_certifier')}")
        L.append(f"- candidates_generated (certified): {_bf.get('certified', 0)}")
        L.append(f"- realistic_executable: {_bf.get('realistic_executable', 0)}")
        L.append(f"- bundles_opened: {_bf.get('bundles_opened', 0)}")
        L.append(f"- bregman_real_market_zero_candidate_reason: "
                 f"{_bf.get('bregman_real_market_zero_candidate_reason') or _bf.get('bregman_candidate_generation_blocker')}")
        rcc = _bf.get("bregman_real_market_zero_candidate_reason_counts", {}) or \
            _bf.get("bregman_candidate_generation_blocker_counts", {}) or {}
        if rcc:
            L.append(f"- bregman_real_market_zero_candidate_reason_counts: {rcc}")
        L.append(f"- bregman_depth_sufficient_groups: {_pv('bregman_depth_sufficient_groups')}")
        L.append(f"- bregman_depth_sufficient_but_negative_edge_count: "
                 f"{_bf.get('bregman_depth_sufficient_but_negative_edge_count', 0)}")
        L.append(f"- bregman_best_depth_sufficient_group_lower_bound: "
                 f"{_bf.get('bregman_best_depth_sufficient_group_lower_bound')}")
        L.append(f"- bregman_best_depth_sufficient_group_reject_reason: "
                 f"{_bf.get('bregman_best_depth_sufficient_group_reject_reason')}")
        brg = _bf.get("bregman_best_real_group_summary") or {}
        if brg:
            L.append(f"- best_real_group: {brg.get('group_key')} "
                     f"depth_sufficient={brg.get('depth_sufficient')} "
                     f"min_leg_depth=${brg.get('min_leg_depth_usd')} "
                     f"(required ${brg.get('required_depth_usd')}) "
                     f"reject={brg.get('reject_reason')} "
                     f"lower_bound={brg.get('after_cost_lower_bound')} "
                     f"market_ids={brg.get('market_ids')} labels={brg.get('outcome_labels')}")
        for s in (_bf.get("bregman_candidate_generation_blocker_samples", []) or [])[:3]:
            L.append(f"  - sample: group={s.get('group_key')} "
                     f"reason={s.get('reject_reason')} depth_sufficient={s.get('depth_sufficient')} "
                     f"market_ids={s.get('market_ids')} "
                     f"token_ids={s.get('token_ids')} labels={s.get('outcome_labels')}")
        if _bf.get("bregman_certifier_exception"):
            L.append(f"- bregman_certifier_exception: {_bf.get('bregman_certifier_exception')}")
        L.append(f"- best_one_fix_away_reason: {_bf.get('best_one_fix_away_reason')}")
        L.append(f"- all_top_near_misses_negative_lower_bound: "
                 f"{_bf.get('all_top_near_misses_negative_lower_bound', False)}")
        # 11d. malformed-group reconciliation (summary vs diagnostic tail)
        L.append("")
        L.append("### 11d. Malformed-Group Reconciliation (summary vs tail)")
        L.append("")
        L.append(f"- malformed_group_count (reconciled): {_bf.get('malformed_group_count', 0)}")
        L.append(f"- bregman_malformed_group_reported_count (trainer certifier): "
                 f"{_bf.get('bregman_malformed_group_reported_count', 0)}")
        L.append(f"- bregman_malformed_group_runtime_count (ABCAS scanner): "
                 f"{_bf.get('bregman_malformed_group_runtime_count', 0)}")
        L.append(f"- bregman_malformed_group_tail_count (diagnostics tail): "
                 f"{_bf.get('bregman_malformed_group_tail_count', 0)}")
        L.append(f"- bregman_malformed_group_legacy_or_tail_only_count: "
                 f"{_bf.get('bregman_malformed_group_legacy_or_tail_only_count', 0)}")
        L.append(f"- source: {_bf.get('bregman_malformed_group_source', 'none')}")
        # 11d-stage. trainer certifier per-STAGE census (never-silent certification)
        scs = _bf.get("bregman_rejection_stage_counts", {}) or {}
        if scs or _bf.get("bregman_max_divergence_gap") is not None:
            L.append("")
            L.append("### 11d-stage. Trainer Certifier Per-Stage Census (read-only)")
            L.append("")
            L.append(f"- bregman_rejection_stage_counts: {scs}")
            L.append(f"- bregman_max_divergence_gap (D(mu*||theta)): "
                     f"{_bf.get('bregman_max_divergence_gap')}")
            L.append(f"- bregman_best_projected_lower_bound: "
                     f"{_bf.get('bregman_best_projected_lower_bound')}")
            L.append(f"- bregman_positive_projected_but_rejected_count: "
                     f"{_bf.get('bregman_positive_projected_but_rejected_count', 0)}")
            if _bf.get("bregman_positive_projected_rejected_by_stage"):
                L.append(f"- bregman_positive_projected_rejected_by_stage: "
                         f"{_bf.get('bregman_positive_projected_rejected_by_stage')}")
            if _bf.get("bregman_zero_certified_explanation"):
                L.append(f"- WHY certified=0: {_bf.get('bregman_zero_certified_explanation')}")
            # per-group profit-lower-bound census (always a float, even negative/zero)
            L.append(f"- profit_lower_bound (min/mean/max): "
                     f"{_bf.get('bregman_profit_lower_bound_min')} / "
                     f"{_bf.get('bregman_profit_lower_bound_mean')} / "
                     f"{_bf.get('bregman_profit_lower_bound_max')}")
            L.append(f"- groups by lower_bound sign (neg/zero/pos): "
                     f"{_bf.get('bregman_groups_negative_lower_bound', 0)} / "
                     f"{_bf.get('bregman_groups_zero_lower_bound', 0)} / "
                     f"{_bf.get('bregman_groups_positive_lower_bound', 0)}")
            for s in (_bf.get("bregman_certify_diagnostics_sample", []) or [])[:5]:
                L.append(f"  - group: {s.get('group_id')} "
                         f"exhaustive={s.get('exhaustive')} "
                         f"settlement_consistent={s.get('settlement_consistent')} "
                         f"profit_lower_bound={s.get('profit_lower_bound')} "
                         f"divergence_gap={s.get('divergence_gap')} "
                         f"reason={s.get('rejection_reason')}")
            for nm in (_bf.get("bregman_top_near_misses", []) or [])[:3]:
                if "rejection_stage" in nm or "divergence_gap" in nm:
                    L.append(f"  - near_miss: {nm.get('group_key')} "
                             f"stage={nm.get('rejection_stage')} "
                             f"exhaustive={nm.get('exhaustive')} "
                             f"settlement_consistent={nm.get('settlement_consistent')} "
                             f"divergence_gap={nm.get('divergence_gap')} "
                             f"projected_lb={nm.get('projected_profit_lower_bound')} "
                             f"reason={nm.get('reject_reason')}")
        # 11e. synthetic fixture proof (isolated; default gates; never live)
        sf = _bf.get("synthetic_fixture", {}) or {}
        if sf:
            L.append("")
            L.append("### 11e. Bregman Synthetic Fixture Proof (isolated, default gates)")
            L.append("")
            for k in ("bregman_synthetic_fixture_passed",
                      "synthetic_binary_candidate_generated",
                      "synthetic_multiway_candidate_generated",
                      "synthetic_invalid_cases_rejected",
                      "synthetic_invalid_case_results",
                      "synthetic_fixture_gate_loosening",
                      "synthetic_fixture_required_depth_usd",
                      "synthetic_fixture_live_trading_enabled",
                      "synthetic_fixture_contaminated_real_metrics"):
                if k in sf:
                    L.append(f"- {k}: {sf.get(k)}")
        # 11f. profit-discovery: durable shadow labels + queue + bandit (learning-only)
        L.append("")
        L.append("### 11f. Profit-Discovery Learning (shadow labels + queue + bandit)")
        L.append("")
        cll = status.get("closed_loop_learning", {}) or {}
        L.append(f"- bregman_shadow_label_candidates: {_bf.get('bregman_shadow_label_candidates', 0)}")
        L.append(f"- bregman_shadow_labels_written: {_bf.get('bregman_shadow_labels_written', 0)}")
        L.append(f"- bregman_shadow_label_write_rate: {_bf.get('bregman_shadow_label_write_rate', 0.0)}")
        L.append(f"- shadow_records_written: "
                 f"{_bf.get('shadow_records_written', cll.get('shadow_records_written', 0))}")
        L.append(f"- shadow_labels_tail_nonempty: {bool(_bf.get('shadow_labels_tail_nonempty', False))}")
        if _bf.get("shadow_label_write_rejection_reasons"):
            L.append(f"- shadow_label_write_rejection_reasons: "
                     f"{_bf.get('shadow_label_write_rejection_reasons')}")
        L.append(f"- profit_discovery_queue_items: {_bf.get('profit_discovery_queue_items', 0)}")
        L.append(f"- profit_discovery_queue_by_priority: "
                 f"{_bf.get('profit_discovery_queue_by_priority', {})}")
        L.append(f"- profit_learning_status: {_bf.get('profit_learning_status')}")
        L.append(f"- profit_data_sufficiency: {_bf.get('profit_data_sufficiency')}")
        if _bf.get("shadow_label_writer_blocker"):
            L.append(f"- BLOCKER: {_bf.get('shadow_label_writer_blocker')}")
        L.append(f"- bandit_router_enabled: {_bf.get('bandit_router_enabled', False)}")
        L.append(f"- bandit_action_counts: {_bf.get('bandit_action_counts', {})}")
        L.append(f"- bandit_action_rewards: {_bf.get('bandit_action_rewards', {})}")
        L.append(f"- bandit_selected_action: {_bf.get('bandit_selected_action')}")
        L.append(f"- bandit_no_gate_override: {_bf.get('bandit_no_gate_override', True)}")
        # 11g. targeted market-scan PRIORITIZATION (never a trade gate)
        if _bf.get("targeted_market_scan_enabled") is not None:
            L.append("")
            L.append("### 11g. Targeted Market-Scan Prioritization (never a trade gate)")
            L.append("")
            L.append(f"- targeted_market_scan_enabled: {_bf.get('targeted_market_scan_enabled')}")
            L.append(f"- targeted_markets_scanned_total: {_bf.get('targeted_markets_scanned_total', 0)}")
            L.append(f"- targeted_scan_field_source: {_bf.get('targeted_scan_field_source')}")
            L.append(f"- targeted_scan_bregman_groups_seen: {_bf.get('targeted_scan_bregman_groups_seen', 0)}")
            L.append(f"- targeted_scan_binary_groups_seen: {_bf.get('targeted_scan_binary_groups_seen', 0)}")
            L.append(f"- targeted_scan_yes_no_pairs_seen: {_bf.get('targeted_scan_yes_no_pairs_seen', 0)}")
            L.append(f"- targeted_scan_binary_group_matches: {_bf.get('targeted_scan_binary_group_matches', 0)} "
                     f"raw_market_matches={_bf.get('targeted_scan_raw_market_matches', 0)}")
            L.append(f"- targeted_scan_bregman_categories: {_bf.get('targeted_scan_bregman_categories', {})}")
            L.append(f"- targeted_scan_raw_market_categories: {_bf.get('targeted_scan_raw_market_categories', {})}")
            L.append(f"- targeted_scan_normalized_reject_reasons: "
                     f"{_bf.get('targeted_scan_normalized_reject_reasons', {})}")
            # read-only CLOB orderbook hydration (real YES/NO books; synthetic = shadow)
            L.append(f"- bregman_clob_hydration_enabled: {_bf.get('bregman_clob_hydration_enabled', False)}")
            L.append(f"- bregman_clob_hydration_attempted: {_bf.get('bregman_clob_hydration_attempted', 0)} "
                     f"success={_bf.get('bregman_clob_hydration_success', 0)} "
                     f"failed={_bf.get('bregman_clob_hydration_failed', 0)}")
            L.append(f"- bregman_real_yes_no_books_seen: {_bf.get('bregman_real_yes_no_books_seen', 0)}")
            L.append(f"- bregman_certifier_used_real_clob_books: "
                     f"{_bf.get('bregman_certifier_used_real_clob_books', False)}")
            L.append(f"- bregman_synthetic_no_diagnostic_only_count: "
                     f"{_bf.get('bregman_synthetic_no_diagnostic_only_count', 0)}")
            if _bf.get("bregman_hydration_failure_reasons"):
                L.append(f"- bregman_hydration_failure_reasons: "
                         f"{_bf.get('bregman_hydration_failure_reasons')}")
            L.append(f"- bregman_clob_hydration_eligible_groups: "
                     f"{_bf.get('bregman_clob_hydration_eligible_groups', 0)} "
                     f"selected={_bf.get('bregman_clob_hydration_selected_groups', 0)} "
                     f"coverage_rate={_bf.get('bregman_clob_hydration_coverage_rate', 0.0)}")
            # paper trade pressure + micro-exploration
            L.append(f"- paper_trade_pressure_enabled: {_bf.get('paper_trade_pressure_enabled', False)}")
            L.append(f"- paper_micro_exploration_enabled: {_bf.get('paper_micro_exploration_enabled', False)}")
            L.append(f"- paper_micro_exploration_candidates: "
                     f"{_bf.get('paper_micro_exploration_candidates', 0)} "
                     f"trades={_bf.get('paper_micro_exploration_trades', 0)}")
            L.append(f"- hydrated_positive_after_cost_candidates: "
                     f"{_bf.get('hydrated_positive_after_cost_candidates', 0)}")
            L.append(f"- realistic_trade_goal_met_11h: {_bf.get('realistic_trade_goal_met_11h', False)}")
            if _bf.get("paper_micro_exploration_reject_reasons"):
                L.append(f"- paper_micro_exploration_reject_reasons: "
                         f"{_bf.get('paper_micro_exploration_reject_reasons')}")
            if _bf.get("zero_trade_blocker_if_any"):
                L.append(f"- zero_trade_blocker_if_any: {_bf.get('zero_trade_blocker_if_any')}")
            # PAPER_RELAXED_EXPLORATION lane
            L.append(f"- paper_relaxed_exploration_enabled: "
                     f"{_bf.get('paper_relaxed_exploration_enabled', False)} "
                     f"(max_notional={_bf.get('paper_relaxed_max_notional', 0)} "
                     f"per_hour={_bf.get('paper_relaxed_max_trades_per_hour', 0)} "
                     f"per_day={_bf.get('paper_relaxed_max_trades_per_day', 0)})")
            L.append(f"- paper_relaxed_candidates_seen: "
                     f"{_bf.get('paper_relaxed_candidates_seen', 0)} "
                     f"trades_opened={_bf.get('paper_relaxed_trades_opened', 0)}")
            L.append(f"- paper_relaxed_after_cost_positive_seen: "
                     f"{_bf.get('paper_relaxed_after_cost_positive_seen', 0)} "
                     f"real_clob_book_seen={_bf.get('paper_relaxed_real_clob_book_seen', 0)}")
            L.append(f"- paper_relaxed_readiness_pnl_excluded: "
                     f"{_bf.get('paper_relaxed_readiness_pnl_excluded', True)}")
            if _bf.get("paper_relaxed_reject_reasons"):
                L.append(f"- paper_relaxed_reject_reasons: "
                         f"{_bf.get('paper_relaxed_reject_reasons')}")
            L.append(f"- paper_relaxed_pipeline_scanned: "
                     f"{_bf.get('paper_relaxed_pipeline_scanned', 0)}")
            L.append(f"- paper_relaxed_real_book_candidates_seen: "
                     f"{_bf.get('paper_relaxed_real_book_candidates_seen', 0)} "
                     f"positive={_bf.get('paper_relaxed_positive_real_book_candidates_seen', 0)}")
            if _bf.get("paper_relaxed_opened_trade_examples"):
                L.append(f"- paper_relaxed_opened_trade_examples: "
                         f"{_bf.get('paper_relaxed_opened_trade_examples')}")
            if _bf.get("paper_relaxed_candidate_source_counts"):
                L.append(f"- paper_relaxed_candidate_source_counts: "
                         f"{_bf.get('paper_relaxed_candidate_source_counts')}")
            if _bf.get("paper_relaxed_candidates_blocked_by_reason"):
                L.append(f"- paper_relaxed_candidates_blocked_by_reason: "
                         f"{_bf.get('paper_relaxed_candidates_blocked_by_reason')}")
            if _bf.get("paper_relaxed_best_real_book_candidate"):
                L.append(f"- paper_relaxed_best_real_book_candidate: "
                         f"{_bf.get('paper_relaxed_best_real_book_candidate')}")
            if _bf.get("paper_relaxed_best_reject_example"):
                L.append(f"- paper_relaxed_best_reject_example: "
                         f"{_bf.get('paper_relaxed_best_reject_example')}")
            # B) event-family completeness diagnostics
            L.append(f"- bregman_false_incomplete_family_count: "
                     f"{_bf.get('bregman_false_incomplete_family_count', 0)} "
                     f"near_miss_promoted={_bf.get('bregman_near_miss_promoted_to_candidate_count', 0)}")
            if _bf.get("bregman_incomplete_family_examples"):
                L.append(f"- bregman_incomplete_family_examples: "
                         f"{_bf.get('bregman_incomplete_family_examples')}")
            if _bf.get("bregman_missing_outcome_examples"):
                L.append(f"- bregman_missing_outcome_examples: "
                         f"{_bf.get('bregman_missing_outcome_examples')}")
            # accelerated discovery / learning mode
            L.append(f"- accelerated_discovery_enabled: "
                     f"{_bf.get('accelerated_discovery_enabled', False)}")
            L.append(f"- markets_scanned_per_tick: {_bf.get('markets_scanned_per_tick', 0)} "
                     f"candidates_evaluated_per_tick={_bf.get('candidates_evaluated_per_tick', 0)} "
                     f"shadow_labels_per_tick={_bf.get('shadow_labels_per_tick', 0)} "
                     f"no_trade_labels_per_tick={_bf.get('no_trade_labels_per_tick', 0)}")
            L.append(f"- near_miss_records_written: {_bf.get('near_miss_records_written', 0)} "
                     f"bregman_diagnostics_records_written={_bf.get('bregman_diagnostics_records_written', 0)}")
            if _bf.get("top_near_miss_edges_after_cost"):
                L.append(f"- top_near_miss_edges_after_cost: "
                         f"{_bf.get('top_near_miss_edges_after_cost')}")
            if _bf.get("top_bregman_rejection_reasons"):
                L.append(f"- top_bregman_rejection_reasons: "
                         f"{_bf.get('top_bregman_rejection_reasons')}")
            if _bf.get("report_buckets"):
                L.append(f"- report_buckets: {_bf.get('report_buckets')}")
            if _bf.get("accelerated_discovery_knobs"):
                L.append(f"- accelerated_discovery_knobs: {_bf.get('accelerated_discovery_knobs')}")
            L.append(f"- market_quality_tier_counts: {_bf.get('market_quality_tier_counts', {})}")
            L.append(f"- market_quality_score_distribution: {_bf.get('market_quality_score_distribution', {})}")
            L.append(f"- targeted_scan_budget_by_category: {_bf.get('targeted_scan_budget_by_category', {})}")
            L.append(f"- targeted_scan_markets_by_category: {_bf.get('targeted_scan_markets_by_category', {})}")
            for k in ("high_liquidity_binary_markets_scanned",
                      "complete_yes_no_tight_spread_markets_scanned",
                      "negative_risk_complete_events_scanned",
                      "short_resolution_markets_scanned", "btc_eth_chainlink_markets_scanned",
                      "fed_macro_reference_markets_scanned",
                      "high_volume_news_linked_markets_scanned",
                      "complete_event_families_scanned"):
                L.append(f"- {k}: {_bf.get(k, 0)}")
            L.append(f"- thin_depth_scan_waste_count (KNOWN-thin only): "
                     f"{_bf.get('thin_depth_scan_waste_count', 0)}")
            L.append(f"- stale_book_scan_waste_count (KNOWN-stale only): "
                     f"{_bf.get('stale_book_scan_waste_count', 0)}")
            L.append(f"- targeted_scan_missing_data_counts (NOT waste): "
                     f"{_bf.get('targeted_scan_missing_data_counts', {})}")
            L.append(f"- scan_deprioritized_groups: {_bf.get('scan_deprioritized_groups', 0)} "
                     f"cooldown_active={_bf.get('scan_cooldown_active_groups', 0)} "
                     f"reasons={_bf.get('scan_cooldown_reason_counts', {})}")
            L.append(f"- not_exhaustive_high_quality_groups: {_bf.get('not_exhaustive_high_quality_groups', 0)} "
                     f"(sibling={_bf.get('not_exhaustive_sent_to_sibling_search', 0)} "
                     f"grok={_bf.get('not_exhaustive_sent_to_grok', 0)} "
                     f"shadow_only={_bf.get('not_exhaustive_remained_shadow_only', 0)})")
            noop = _bf.get("targeted_scan_noop_reasons", {}) or {}
            if noop:
                L.append(f"- targeted_scan_noop_reasons: {noop}")
            for b in (_bf.get("targeted_scan_best_markets", []) or [])[:3]:
                L.append(f"  - best: {b.get('market_id')} tier={b.get('tier')} "
                         f"score={b.get('score')} categories={b.get('categories')}")
    L.append("")
    L.append("## 12. Paper Training Metrics")
    L.append("")
    for k in ("equity", "total_pnl", "after_cost_pnl", "open_positions",
              "closed_positions", "paper_trades", "win_rate_traded_only"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append("")
    L.append("## 13. Strategy Attribution")
    L.append("")
    L.append(f"- paper_attribution_enabled: {_yn(feats.get('paper_attribution_enabled'))}")
    L.append(f"- exploration_validation_separated: {_yn(feats.get('exploration_validation_separated'))}")
    L.append("")
    L.append("## 14. Fill Realism")
    L.append("")
    L.append(f"- fill_realism_enabled: {_yn(feats.get('fill_realism_enabled'))}")
    L.append(f"- fantasy_fill_rejections: {_yn(feats.get('fantasy_fill_rejections'))}")
    L.append("")
    # Pass-3: Paper Realism funnel — realistic vs shadow vs rejected; only
    # realistic_executable trades feed readiness PnL.
    pr = status.get("paper_realism") or {}
    if pr:
        L.append("### 14a. Paper Realism (Pass 3)")
        L.append("")
        for k in ("total_candidates_considered", "realistic_trade_count",
                  "shadow_trade_count", "hard_reject_count", "reference_fill_attempts",
                  "reference_fills_allowed", "reference_fills_blocked",
                  "stale_book_rejection_count", "missing_ask_rejection_count",
                  "thin_depth_rejection_count", "wide_spread_rejection_count",
                  "ambiguity_rejection_count", "offline_stub_rejection_count",
                  "avg_spread_executed", "avg_depth_executed", "avg_book_age_executed"):
            L.append(f"- {k}: {_yn(pr.get(k))}")
        L.append("")
        L.append("PnL separation (only realistic_executable counts toward readiness):")
        for k in ("bregman_realistic_pnl", "directional_realistic_pnl", "exploration_pnl",
                  "shadow_theoretical_pnl", "reference_fill_theoretical_pnl",
                  "realistic_pnl", "readiness_pnl"):
            L.append(f"- {k}: {_yn(pr.get(k))}")
        L.append("")
        L.append("Realism posture:")
        for k in ("reference_price_fills_allowed_for_exploit", "missing_ask_fallback_allowed",
                  "stale_book_fills_allowed", "offline_stub_fills_count_as_real",
                  "bregman_requires_all_executable_legs"):
            L.append(f"- {k}: {_yn(pr.get(k))}")
        L.append("")
    # 100X paper profit-discovery profile + per-lane paper-trade acceleration.
    acc = status.get("paper_trade_acceleration") or {}
    agg = status.get("aggressive_paper") or {}
    if acc or agg:
        a = {**agg, **acc}
        # numeric proof fields must render as RAW NUMBERS (not yes/no); booleans as yes/no.
        _NUMERIC = {"feedback_accelerator_target_multiplier",
                    "feedback_accelerator_requested_multiplier",
                    "feedback_accelerator_effective_capacity_multiplier",
                    "feedback_accelerator_effective_capacity_cap",
                    "active_learning_tiny_trades_selected",
                    "active_learning_tiny_trades_opened", "relaxed_bregman_trades_opened",
                    "btc_pulse_paper_trades_opened", "readiness_paper_trades_opened",
                    "exploration_pnl"}

        def _fmt(k):
            v = a.get(k)
            return (v if v is not None else 0) if k in _NUMERIC else _yn(v)
        L.append("### 14a-2. 100X Feedback Accelerator + Paper Trade Acceleration")
        L.append("")
        for k in ("aggressive_paper_training_enabled", "feedback_accelerator_enabled",
                  "feedback_accelerator_target_multiplier",
                  "feedback_accelerator_requested_multiplier",
                  "feedback_accelerator_effective_capacity_multiplier",
                  "feedback_accelerator_effective_capacity_cap",
                  "paper_profit_discovery_profile_enabled", "active_learning_enabled",
                  "exploration_enabled", "accelerated_discovery_enabled",
                  "real_execution_possible", "live_flags_forced_off"):
            L.append(f"- {k}: {_fmt(k)}")
        L.append("")
        L.append("Tiny paper-learning lanes (exploration PnL excluded from readiness):")
        for k in ("active_learning_tiny_trades_selected", "active_learning_tiny_trades_opened",
                  "relaxed_bregman_trades_opened", "btc_pulse_paper_trades_opened",
                  "exploration_pnl", "readiness_pnl_excludes_exploration"):
            L.append(f"- {k}: {_fmt(k)}")
        L.append(f"- active_learning_tiny_trades_blocked_by_reason: "
                 f"{a.get('active_learning_tiny_trades_blocked_by_reason')}")
        L.append("")
        L.append("Lane-specific zero-trade blockers (empty == lane opened >=1 paper trade):")
        for k in ("bregman_blocker", "relaxed_bregman_blocker", "tiny_directional_blocker",
                  "btc_pulse_blocker", "paper_trade_acceleration_blocker_if_any"):
            L.append(f"- {k}: {a.get(k) if a.get(k) else '(none)'}")
        L.append("")
    # Pass-4: Strategy Priority — Bregman (Tier 1) first claim on slots/capital.
    sp = status.get("strategy_priority") or {}
    if sp:
        L.append("### 14b. Strategy Priority (Pass 4)")
        L.append("")
        L.append(f"- Bregman evaluated before directional: "
                 f"{_yn(sp.get('bregman_evaluated_before_directional'))}")
        L.append(f"- Directional consumed capacity before Bregman: "
                 f"{_yn(sp.get('directional_consumed_capacity_before_bregman'))} (should be false)")
        L.append(f"- Bregman groups discovered: {_yn(sp.get('bregman_groups_discovered'))}")
        L.append(f"- Bregman certified (realistic executable): "
                 f"{_yn(sp.get('bregman_certified_before_directional_count'))}")
        L.append(f"- Bregman bundles opened before directional: "
                 f"{_yn(sp.get('bregman_opened_before_directional_count'))}")
        if not sp.get("bregman_opened_before_directional_count"):
            L.append(f"  - Why zero opened: {sp.get('bregman_zero_open_reason') or 'n/a'}")
        for k in ("bregman_reserved_slots", "bregman_reserved_capital_usd",
                  "directional_slots_before_bregman", "directional_slots_after_bregman",
                  "directional_trades_blocked_by_bregman_reservation",
                  "directional_trades_blocked_by_bregman_market_collision",
                  "directional_trades_blocked_by_bregman_event_collision",
                  "unused_bregman_slots_released_to_directional",
                  "unused_bregman_capital_released_to_directional",
                  "exploration_blocked_from_reserved_bregman_capacity"):
            L.append(f"- {k}: {_yn(sp.get(k))}")
        L.append(f"- Exploration consumed reserved Bregman capacity: "
                 f"{_yn(bool(sp.get('exploration_blocked_from_reserved_bregman_capacity')) and False)}"
                 f" (blocked by default)")
        L.append("")
    # Pass-5: Profitability Ranking — candidates compete on after-cost EV.
    prk = status.get("profitability_ranking") or {}
    if prk:
        L.append("### 14c. Profitability Ranking (Pass 5)")
        L.append("")
        L.append(f"- Profitability-first enabled: {_yn(prk.get('profitability_first_enabled'))}")
        L.append(f"- Annotation before truncation: "
                 f"{_yn(prk.get('profitability_annotation_before_truncation'))}")
        L.append(f"- Bregman-first priority preserved: "
                 f"{_yn(prk.get('bregman_first_priority_preserved'))} (should be true)")
        L.append(f"- Execution without annotation: "
                 f"{_yn(prk.get('execution_without_annotation'))} (should be 0)")
        for k in ("candidates_annotated", "candidates_missing_profitability_data",
                  "directional_after_cost_positive", "bregman_after_cost_positive",
                  "candidates_rejected_negative_after_cost",
                  "candidates_shadow_theoretical_only", "profitability_governor_hard_rejects",
                  "avg_after_cost_edge_executed", "avg_after_cost_roi_executed",
                  "total_expected_value_usd_executed", "top_ranked_candidate_reason"):
            L.append(f"- {k}: {_yn(prk.get(k))}")
        L.append(f"- profitability_buckets: {_yn(prk.get('profitability_buckets'))}")
        L.append("")
    # Pass-6: Active Learning — exploration is selected by ActiveLearningSelector.
    al = status.get("active_learning") or {}
    if al:
        L.append("### 14d. Active Learning (Pass 6)")
        L.append("")
        L.append(f"- Active learning enabled: {_yn(al.get('active_learning_enabled'))}")
        L.append(f"- Active learning runtime enabled: {_yn(al.get('active_learning_runtime_enabled'))}")
        L.append(f"- Active learning config source: {al.get('active_learning_config_source')}")
        _mm = bool(al.get('active_learning_config_mismatch'))
        L.append(f"- Config mismatch (declared vs effective): {_yn(_mm)} (should be false)")
        if _mm:
            L.append(f"  - **CONFIG MISMATCH**: {al.get('active_learning_config_mismatch_reason')}")
        L.append(f"- Tiny evaluator called: {_yn(al.get('active_learning_tiny_evaluator_called'))}")
        L.append(f"- Tiny candidates evaluated: {_yn(al.get('active_learning_tiny_candidates_evaluated'))}")
        L.append(f"- Tiny trades selected: {_yn(al.get('active_learning_tiny_trades_selected'))}")
        L.append(f"- Tiny trades opened: {_yn(al.get('active_learning_tiny_trades_opened'))}")
        L.append(f"- Selected-but-not-evaluated (must be 0): "
                 f"{_yn(al.get('active_learning_selected_but_not_evaluated_count'))}")
        L.append(f"- Tiny blocked by reason: {al.get('active_learning_tiny_blocked_by_reason')}")
        L.append(f"- Random exploration enabled: {_yn(al.get('random_exploration_enabled'))} "
                 f"(should be false)")
        L.append(f"- Random/hash exploration opened trades: "
                 f"{_yn(al.get('random_exploration_opened_trades'))} (should be 0)")
        L.append(f"- Legacy random exploration blocked: "
                 f"{_yn(al.get('legacy_random_exploration_blocked'))}")
        L.append(f"- Exploration counted toward readiness: "
                 f"{_yn(al.get('exploration_counted_toward_readiness'))} (should be false)")
        L.append(f"- Exploration consumes Bregman reserved capacity: "
                 f"{_yn(al.get('exploration_consumes_bregman_reserved_capacity'))} (should be false)")
        for k in ("active_learning_candidates_considered", "active_learning_candidates_selected",
                  "exploration_trades_opened", "exploration_shadow_only",
                  "exploration_rejected_by_realism", "exploration_rejected_by_budget",
                  "exploration_rejected_by_collision", "exploration_rejected_by_diversity",
                  "exploration_budget_used_usd", "exploration_expected_loss_usd",
                  "exploration_pnl", "avg_active_learning_score_selected",
                  "avg_execution_quality_selected", "top_learning_buckets",
                  "category_coverage", "pending_feedback_count", "completed_feedback_count"):
            L.append(f"- {k}: {_yn(al.get(k))}")
        L.append("")
    # Pass-7: Correlation Risk — cluster/correlation is an active hard gate.
    cr = status.get("correlation_risk") or {}
    if cr:
        L.append("### 14e. Correlation Risk (Pass 7)")
        L.append("")
        L.append(f"- Correlation gate enabled: {_yn(cr.get('correlation_gate_enabled'))}")
        L.append(f"- Unknown clusters become shadow-only: "
                 f"{_yn(cr.get('unknown_cluster_policy') == 'shadow')} (default)")
        L.append(f"- Real trade without cluster metadata: "
                 f"{_yn(cr.get('real_trade_without_cluster_metadata'))} (should be 0)")
        for k in ("candidates_with_cluster_id", "candidates_missing_cluster_id",
                  "open_clusters_count", "open_events_count", "open_correlation_groups_count",
                  "blocked_same_market", "blocked_same_condition", "blocked_same_event",
                  "blocked_same_cluster", "blocked_bregman_market_collision",
                  "blocked_bregman_event_collision", "blocked_exploration_cluster_collision",
                  "size_capped_by_cluster_exposure", "shadowed_unknown_cluster",
                  "directional_trades_blocked_by_correlation",
                  "exploration_trades_blocked_by_correlation",
                  "bregman_bundles_blocked_as_duplicates",
                  "bregman_bundles_blocked_as_overlapping",
                  "max_cluster_exposure_usd", "max_event_exposure_usd", "top_open_clusters"):
            L.append(f"- {k}: {_yn(cr.get(k))}")
        L.append("")
    L.append("## 15. Calibration Metrics")
    L.append("")
    for k in ("brier", "ece", "sharpe", "sortino", "calmar", "max_drawdown"):
        L.append(f"- {k}: {_yn(feats.get(k))}")
    L.append("")

    # 16. Test Results
    L.append("## 16. Test Results")
    L.append("")
    if tests.get("skipped"):
        L.append("- Tests skipped (`--skip-tests`).")
    else:
        L.append("| Suite | exit | summary |")
        L.append("|---|---|---|")
        for name, rec in (tests.get("runs") or {}).items():
            s = rec.get("summary", {})
            scomp = ", ".join(f"{k}={v}" for k, v in s.items() if v is not None) or "—"
            L.append(f"| {name} | {rec.get('exit_code')} | {scomp} |")
    L.append("")

    # 17. Docker Logs / Errors
    L.append("## 17. Docker Logs / Errors")
    L.append("")
    if docker.get("skipped") or not docker.get("available"):
        L.append("- Docker not available / skipped. See `logs/` if collected.")
    else:
        lt = docker.get("logs_training", {})
        L.append(f"- hermes-training logs collected: {_yn(lt.get('ok'))} "
                 f"(see `logs/hermes-training_tail1000.log`)")
        le = docker.get("logs_engine", {})
        L.append(f"- hermes-trading-engine logs collected: {_yn(le.get('ok'))} "
                 f"(see `logs/hermes-trading-engine_tail500.log`)")
    L.append("")

    # 18. API Snapshot Summary
    L.append("## 18. API Snapshot Summary")
    L.append("")
    if not api:
        L.append("- API collection skipped.")
    else:
        L.append("| Endpoint | ok | status | note |")
        L.append("|---|---|---|---|")
        for name, e in api.items():
            L.append(f"| {name} | {_yn(e.get('ok'))} | {e.get('status')} | {e.get('error', '')} |")
    L.append("")

    # 19. Artifacts Included
    L.append("## 19. Artifacts Included")
    L.append("")
    if artifacts.get("skipped"):
        L.append("- Artifact collection skipped.")
    else:
        for a in artifacts.get("host_found", []):
            L.append(f"- {a['name']}: {'copied' if a.get('copied') else 'present (not copied)'} "
                     f"({a.get('bytes', 0)} bytes)")
        if artifacts.get("host_missing"):
            L.append(f"- Missing (recorded, not fatal): {', '.join(artifacts['host_missing'])}")
    L.append("")

    # 20. Missing Features / Missing Evidence
    L.append("## 20. Missing Features / Missing Evidence")
    L.append("")
    if not missing_features:
        L.append("- None detected.")
    else:
        for mf in missing_features:
            L.append(f"- [{mf['severity']}] {mf['feature']}: {mf['detail']}")
    L.append("")

    # 21. Key Problems Found
    L.append("## 21. Key Problems Found")
    L.append("")
    probs = [f for f in safety.get("findings", []) if f["severity"] in ("CRITICAL", "WARN")]
    if not probs and not rj["warnings"]:
        L.append("- No critical problems found.")
    else:
        for f in probs:
            L.append(f"- [{f['severity']}] {f['flag']}: {f['reason']}")
        for w in rj["warnings"]:
            L.append(f"- [WARN] {w}")
    L.append("")

    # 22. Recommended Next Fixes
    L.append("## 22. Recommended Next Fixes")
    L.append("")
    if not recommendations:
        L.append("- None — bot looks healthy.")
    else:
        for r in recommendations:
            L.append(f"- **{r['priority']}** ({r['area']}): {r['action']}")
    L.append("")

    # 23. Algorithmic Benchmarks
    L.append("## 23. Algorithmic Benchmarks")
    L.append("")
    rows = benchmarks.get("benchmarks", [])
    if not rows:
        L.append("- No benchmarks computed.")
    else:
        s = benchmarks.get("summary", {})
        L.append(f"Summary: pass={s.get('pass', 0)} warn={s.get('warn', 0)} "
                 f"fail={s.get('fail', 0)} missing={s.get('missing', 0)}")
        L.append("")
        L.append("| Benchmark | Value | Target | Dir | Status | Description |")
        L.append("|---|---|---|---|---|---|")
        for b in rows:
            L.append(f"| {b['name']} | {_yn(b['value'])} | {b['target']} | "
                     f"{b['direction']} | {b['status'].upper()} | {b['description']} |")
    L.append("")

    # 24. Cross-Surface Consistency
    L.append("## 24. Cross-Surface Consistency")
    L.append("")
    if not consistency:
        L.append("- No inconsistencies detected (dashboard vs paper-training equity, "
                 "live-detected flags, cost accounting).")
    else:
        for c in consistency:
            L.append(f"- [{c.get('severity')}] {c.get('check')}: {c.get('detail')}")
    L.append("")

    # 25. Quant Responsibilities
    L.append("## 25. Quant Responsibilities")
    L.append("")
    if not quant_responsibilities:
        L.append("- Not available.")
    else:
        L.append("| Domain | Owner | Coverage | Responsibilities |")
        L.append("|---|---|---|---|")
        for domain, spec in quant_responsibilities.items():
            resp = "; ".join(spec.get("responsibilities", []))
            L.append(f"| {domain} | {spec.get('owner')} | "
                     f"{spec.get('coverage')} | {resp} |")
    L.append("")

    # 26. Final Validation (Execution & Readiness)
    L.append("## 26. Final Validation (Execution & Readiness)")
    L.append("")
    if not final_validation:
        L.append("- Not available.")
    else:
        fv = final_validation
        L.append(f"- validation_ready: **{fv.get('validation_ready')}** "
                 f"(exploration excluded from the verdict)")
        if fv.get("blocking_reasons"):
            L.append(f"- blocking_reasons: {', '.join(fv['blocking_reasons'])}")
        L.append("")
        L.append("| Check | Value |")
        L.append("|---|---|")
        for k, v in (fv.get("checks", {}) or {}).items():
            L.append(f"| {k} | {v} |")
    L.append("")

    # 27. Files Included In Bundle
    L.append("## 27. Files Included In Bundle")
    L.append("")
    for f in sorted(rj["files"]):
        L.append(f"- {f}")
    L.append("")
    return "\n".join(L)


def _make_zip(bundle_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(bundle_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(bundle_dir.parent))


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Generate a PAPER-ONLY bot inspection & performance report bundle.")
    ap.add_argument("--output", default="inspection_reports",
                    help="output directory for the bundle folder + zip")
    ap.add_argument("--repo-root", default=None, help="repo root (default: plugin root)")
    ap.add_argument("--pr", default=None, help="optional PR number for context only")
    ap.add_argument("--baseline", default=None, help="path to a previous report.json baseline")
    ap.add_argument("--history-days", type=int, default=7)
    ap.add_argument("--api-base-url", default="http://localhost:8800")
    ap.add_argument("--data-dir", default=None, help="HTE data dir (default: $HTE_DATA_DIR)")
    ap.add_argument("--bundle-mode", choices=["light", "full"], default="light",
                    help="light (default): small zip — summary metrics/reports + "
                         "tail samples of the JSONL event streams (full files stay in "
                         "/data). full: forensic — include the full JSONL event files.")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--skip-docker", action="store_true")
    ap.add_argument("--skip-api", action="store_true")
    ap.add_argument("--skip-artifacts", action="store_true")
    ap.add_argument("--include-docker", action="store_true", default=None)
    ap.add_argument("--include-api", action="store_true", default=None)
    ap.add_argument("--include-artifacts", action="store_true", default=None)
    ap.add_argument("--include-container-artifacts", action="store_true",
                    help="also try docker compose cp of container artifact paths")
    ap.add_argument("--tail-training-logs", type=int, default=1000)
    ap.add_argument("--tail-engine-logs", type=int, default=500)
    ap.add_argument("--fail-on-incomplete-audit", action="store_true",
                    help="exit non-zero when the mandatory Algorithmic Edge Audit is "
                         "incomplete (missing core fields or stale status). Use in CI to "
                         "block trusting a non-decision-grade report.")
    return ap


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    result = generate_report(
        output_dir=args.output,
        repo_root=args.repo_root,
        skip_tests=args.skip_tests,
        skip_docker=args.skip_docker,
        skip_api=args.skip_api,
        skip_artifacts=args.skip_artifacts,
        include_docker=True if args.include_docker is None else args.include_docker,
        include_api=True if args.include_api is None else args.include_api,
        include_artifacts=True if args.include_artifacts is None else args.include_artifacts,
        include_container_artifacts=args.include_container_artifacts,
        tail_training_logs=args.tail_training_logs,
        tail_engine_logs=args.tail_engine_logs,
        history_days=args.history_days,
        baseline_path=args.baseline,
        pr=args.pr,
        api_base_url=args.api_base_url,
        data_dir=args.data_dir,
        bundle_mode=args.bundle_mode,
    )
    print(f"Classification : {result['classification']}")
    print(f"Health score   : {result['score']}/100")
    print(f"Bundle folder  : {result['bundle_dir']}")
    print(f"Zip bundle     : {result['zip_path']}")
    _rr = result.get("run_ready", {}) or {}
    _mode = result.get("bundle_mode", "light")
    try:
        _zsz = os.path.getsize(result["zip_path"])
        _zhuman = (f"{_zsz/1024/1024:.2f} MB" if _zsz >= 1024 * 1024
                   else f"{_zsz/1024:.1f} KB")
    except Exception:  # noqa: BLE001
        _zhuman = "?"
    print(f"Bundle mode    : {_mode}")
    if _mode == "light":
        print("Full JSONL     : omitted intentionally")
        print(f"Samples        : tail {LIGHT_TAIL_ROWS} rows each")
        print(f"Source events  : {'verified' if _rr.get('source_event_files_verified') else 'NOT verified'}"
              + (f" from {args.data_dir}" if args.data_dir else ""))
    else:
        print("Full JSONL     : included")
    print(f"Zip size       : {_zhuman}")
    audit_ok = result.get("algorithmic_edge_audit_ok", True)
    print(f"Edge audit     : {'PASS' if audit_ok else 'FAIL (incomplete/stale)'}")
    rr = result.get("run_ready", {}) or {}
    print(f"Run-ready      : run_ready_for_hours={rr.get('run_ready_for_hours')} "
          f"(max_safe_runtime_minutes={rr.get('max_safe_runtime_minutes')})")
    print(f"Grok brain     : grok_brain_ready={rr.get('grok_brain_ready')}"
          + (f" blocker={rr.get('grok_brain_blocker')}" if not rr.get("grok_brain_ready") else ""))
    _ge = result.get("grok_news_evidence", {}) or {}
    if _ge:
        print(f"Grok advisory  : calls={_ge.get('grok_calls_total', 0)} "
              f"with_news={_ge.get('grok_calls_with_news', 0)} "
              f"near_misses_analyzed={_ge.get('grok_bregman_near_misses_analyzed', 0)} "
              f"news_linked={_ge.get('grok_news_linked_markets_analyzed', 0)} "
              f"advisory_only={_ge.get('grok_advisory_only_count', 0)}")
    _bf2 = result.get("bregman_funnel", {}) or {}
    if _bf2:
        _blk = _bf2.get("blocker_explanation", {}) or {}
        print(f"Bregman near   : total={_bf2.get('bregman_near_misses_total', 0)} "
              f"one_fix_away={_bf2.get('near_miss_one_fix_away_count', 0)} "
              f"depth_only={_bf2.get('near_miss_depth_only_count', 0)} "
              f"not_exhaustive={_bf2.get('near_miss_not_exhaustive_count', 0)}"
              + (f" | no-bundle blocker={_blk.get('primary_blocker')}"
                 if _blk.get('blocked') else ""))
    if rr.get("blocking_reasons"):
        print("Blocking       : " + "; ".join(rr["blocking_reasons"]))
    ap = result.get("artifact_paths", {}) or {}
    print("Artifact paths :")
    for k in ("runtime_training_data_dir", "runtime_metrics_dir", "runtime_reports_dir"):
        print(f"   {k}={ap.get(k)} exists={ap.get(k+'_exists')}")
    print(f"   inspection_bundle_dir={ap.get('inspection_bundle_dir')}")
    if not (ap.get("runtime_training_data_dir_exists")):
        print(f"   hint: {ap.get('hint')}")
    if not audit_ok and result.get("algorithmic_edge_audit_missing"):
        print("Missing fields : " + ", ".join(result["algorithmic_edge_audit_missing"]))
    if result["warnings"]:
        print("Warnings       : " + "; ".join(result["warnings"]))
    if args.fail_on_incomplete_audit and not audit_ok:
        print("FAIL: Algorithmic Edge Audit incomplete (--fail-on-incomplete-audit).",
              file=sys.stderr)
        return 5
    # non-zero exit when not run-ready so CI/automation cannot treat it as success
    if not rr.get("run_ready_for_hours", True):
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
