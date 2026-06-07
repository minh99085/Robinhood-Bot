"""Absolute artifact directory resolution + durable-write verification (PAPER ONLY).

The trainer's durable artifacts (metrics / reports / training event stream) must
land in REAL, writable, absolute directories — and the logs must print where they
actually are (not misleading relative paths). This module resolves those dirs from
env (with data-dir-relative defaults), creates them, reports their absolute path /
exists / writable state at startup, and verifies on-disk files after each tick so a
positive in-memory counter can never be printed while the file is missing/empty.

Env (all optional; default to ``<HTE_DATA_DIR>/{metrics,reports,training}``):
* ``POLYMARKET_METRICS_DIR``
* ``POLYMARKET_REPORTS_DIR``
* ``POLYMARKET_TRAINING_DATA_DIR``
* ``POLYMARKET_EVENT_STREAM_PATH``

The defaults are RELATIVE to the training data dir so artifacts persist on the
mounted data volume (and stay visible to the dashboard) — no Docker topology change.
"""

from __future__ import annotations

import os
from pathlib import Path

# Required durable training-data files (relative to the training data dir).
TRAINING_FILES = (
    "events.jsonl", "decision_records.jsonl", "no_trade_labels.jsonl",
    "shadow_labels.jsonl", "diagnostics.jsonl", "pending_labels.jsonl",
    "completed_labels.jsonl", "learning_state.json",
)
# Required metric files (relative to the metrics dir).
METRIC_FILES = (
    "inspection_summary.json", "closed_loop_learning.json", "learning_feedback.json",
    "active_learning.json", "paper_realism.json", "bregman_execution.json",
    "strategy_priority.json", "profitability_ranking.json", "correlation_risk.json",
    "training_reconciliation.json", "run_ready.json", "bregman_funnel.json",
    "grok_news_evidence.json",
)
REPORT_FILES = ("paper_training_inspection.md", "closed_loop_learning_audit.md")


def training_dir(data_dir) -> Path:
    """The absolute training-data dir: ``POLYMARKET_TRAINING_DATA_DIR`` if set, else
    ``<data_dir>/training``. Single source used by the trainer, the event sink, and
    the closed-loop store so they all write to the SAME real directory."""
    v = os.getenv("POLYMARKET_TRAINING_DATA_DIR")
    return Path(v).resolve() if v else (Path(data_dir).resolve() / "training")


def resolve_artifact_dirs(data_dir) -> dict:
    """Resolve the absolute metrics/reports/training dirs + event stream path.

    Env overrides win; defaults are ``<data_dir>/{metrics,reports,training}`` so the
    artifacts persist on the same volume as the training status file."""
    base = Path(data_dir).resolve()

    def _d(env_name: str, default: Path) -> Path:
        v = os.getenv(env_name)
        return Path(v).resolve() if v else default

    metrics = _d("POLYMARKET_METRICS_DIR", base / "metrics")
    reports = _d("POLYMARKET_REPORTS_DIR", base / "reports")
    training = _d("POLYMARKET_TRAINING_DATA_DIR", base / "training")
    ev = os.getenv("POLYMARKET_EVENT_STREAM_PATH")
    events = Path(ev).resolve() if ev else (training / "events.jsonl")
    return {"data_dir": str(base), "metrics_dir": metrics, "reports_dir": reports,
            "training_data_dir": training, "event_stream_path": events}


def ensure_dirs(dirs: dict) -> None:
    """Create the metrics/reports/training dirs (parents, idempotent)."""
    for key in ("metrics_dir", "reports_dir", "training_data_dir"):
        try:
            Path(dirs[key]).mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001 — startup must report, not crash
            pass


def _writable(p: Path) -> bool:
    try:
        return p.is_dir() and os.access(str(p), os.W_OK)
    except Exception:  # noqa: BLE001
        return False


def startup_report(dirs: dict) -> str:
    """Human-readable startup block proving each artifact dir's absolute path /
    existence / writability (printed once at startup)."""
    lines = ["artifact_dirs:"]
    for label, key in (("metrics", "metrics_dir"), ("reports", "reports_dir"),
                       ("training_data", "training_data_dir")):
        p = Path(dirs[key])
        lines.append(f"  {label}={p} exists={str(p.is_dir()).lower()} "
                     f"writable={str(_writable(p)).lower()}")
    lines.append(f"  event_stream={Path(dirs['event_stream_path'])}")
    return "\n".join(lines)


def _rows(p: Path) -> int:
    try:
        if not p.exists():
            return -1
        with p.open("r", encoding="utf-8") as fh:
            return sum(1 for ln in fh if ln.strip())
    except Exception:  # noqa: BLE001
        return -1


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.exists() else -1
    except Exception:  # noqa: BLE001
        return -1


def proof_lines(dirs: dict) -> list:
    """Absolute-path proof lines for the key artifacts (printed each tick AFTER the
    files are written), with exists + size/rows so the log can never lie."""
    insp = Path(dirs["metrics_dir"]) / "inspection_summary.json"
    rep = Path(dirs["reports_dir"]) / "paper_training_inspection.md"
    ev = Path(dirs["event_stream_path"])
    dr = Path(dirs["training_data_dir"]) / "decision_records.jsonl"
    pl = Path(dirs["training_data_dir"]) / "pending_labels.jsonl"
    return [
        f"Report: {rep} exists={str(rep.exists()).lower()} size={_size(rep)}",
        f"Inspection summary: {insp} exists={str(insp.exists()).lower()} size={_size(insp)}",
        f"Event stream: {ev} exists={str(ev.exists()).lower()} rows={_rows(ev)}",
        f"Decision records: {dr} exists={str(dr.exists()).lower()} rows={_rows(dr)}",
        f"Pending labels: {pl} exists={str(pl.exists()).lower()} rows={_rows(pl)}",
    ]


def verify_durable_writes(dirs: dict, *, decision_count: int = 0,
                          pending_count: int = 0) -> dict:
    """Verify the durable files actually exist (+ have rows when counters say so).

    Returns ``{ok, blocking_reasons, missing, empty, checked}``. When the in-memory
    counters are positive but the files are missing/empty, ``ok=False`` and the
    caller must NOT claim run-readiness (durable_event_files_not_written)."""
    training = Path(dirs["training_data_dir"])
    metrics = Path(dirs["metrics_dir"])
    checks = {
        "metrics/inspection_summary.json": metrics / "inspection_summary.json",
        "metrics/training_reconciliation.json": metrics / "training_reconciliation.json",
        "metrics/run_ready.json": metrics / "run_ready.json",
        "data/training/events.jsonl": Path(dirs["event_stream_path"]),
        "data/training/decision_records.jsonl": training / "decision_records.jsonl",
        "data/training/pending_labels.jsonl": training / "pending_labels.jsonl",
        "data/training/learning_state.json": training / "learning_state.json",
    }
    missing = [rel for rel, p in checks.items() if not p.exists()]
    blocking: list = []
    if missing:
        blocking.append("durable_artifact_files_not_written")
    # rows must be > 0 when counters are positive (a positive counter cannot
    # coexist with an empty/missing event file)
    empty: list = []
    if int(decision_count) > 0:
        for rel in ("data/training/events.jsonl", "data/training/decision_records.jsonl"):
            if _rows(checks[rel]) <= 0:
                empty.append(rel)
    if int(pending_count) > 0 and _rows(checks["data/training/pending_labels.jsonl"]) <= 0:
        empty.append("data/training/pending_labels.jsonl")
    if empty:
        blocking.append("durable_event_files_not_written")
    return {"ok": not blocking, "blocking_reasons": blocking, "missing": missing,
            "empty": empty, "checked": {k: str(v) for k, v in checks.items()}}
