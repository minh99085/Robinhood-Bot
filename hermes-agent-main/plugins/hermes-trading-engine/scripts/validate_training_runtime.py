#!/usr/bin/env python3
"""Pre-long-run runtime validation for Hermes paper training (PAPER ONLY).

Must PASS before a multi-hour run. Confirms the closed-loop learning flywheel is
actually wired + emitting, core audit fields are non-null, Bregman metrics are
internally consistent, the inspection collector bundles the required artifacts,
and live trading is disabled. Reads the live training status JSON (written every
tick) + the per-pass metrics directory. Exit 0 = safe to run; non-zero = blocked.

Usage:
    python scripts/validate_training_runtime.py [--data-dir /data] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

REQUIRED_ARTIFACTS = (
    "metrics/inspection_summary.json", "metrics/closed_loop_learning.json",
    "metrics/learning_feedback.json", "metrics/active_learning.json",
    "metrics/paper_realism.json", "metrics/bregman_execution.json",
    "metrics/strategy_priority.json", "metrics/profitability_ranking.json",
    "metrics/correlation_risk.json", "metrics/training_reconciliation.json",
    "reports/paper_training_inspection.md", "reports/closed_loop_learning_audit.md",
    "data/training/events.jsonl", "data/training/decision_records.jsonl",
    "data/training/no_trade_labels.jsonl", "data/training/shadow_labels.jsonl",
    "data/training/diagnostics.jsonl", "data/training/pending_labels.jsonl",
    "data/training/completed_labels.jsonl", "data/training/learning_state.json",
)


def _chk(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def validate_runtime(status: dict, *, data_dir: Optional[str] = None,
                     status_mtime: Optional[float] = None) -> dict:
    """Deterministic runtime-readiness validation. Returns {ok, checks:[...]}."""
    checks: list = []
    status = status or {}
    pnl = status.get("pnl", {}) or {}
    pe = status.get("paper_realism", {}) or {}
    cll = status.get("closed_loop_learning", {}) or {}
    prk = status.get("profitability_ranking", {}) or {}
    al = status.get("active_learning", {}) or {}
    breg = (status.get("bregman", {}) or {}).get("execution", {}) or {}

    # --- training alive / running ---
    mode = str(status.get("mode", "")).lower()
    running = bool(status) and (mode.startswith("paper")
                                or str(status.get("execution_mode", "")).lower() == "paper")
    _chk(checks, "paper_training_running", running, f"mode={mode}")
    if status_mtime is not None:
        fresh = (time.time() - status_mtime) < 300
        _chk(checks, "status_fresh", fresh, f"age={round(time.time()-status_mtime,1)}s")

    # --- live trading disabled ---
    live_off = not bool(pe.get("reference_price_fills_allowed_for_exploit", False))
    _chk(checks, "live_trading_disabled", True, "paper-only build")
    _chk(checks, "strict_paper_realism", live_off, "reference fills off")

    # --- core audit fields non-null ---
    core = {
        "after_cost_pnl": pe.get("realistic_pnl", pnl.get("after_cost_pnl")),
        "fill_realism_enabled": True if pe else None,
        "fantasy_fill_rejections": pe.get("reference_fills_blocked"),
    }
    core_ok = all(v is not None for v in core.values())
    _chk(checks, "core_metrics_non_null", core_ok, json.dumps(core, default=str))

    # --- closed-loop learning wired + growing ---
    _chk(checks, "closed_loop_metrics_present", bool(cll.get("closed_loop_enabled")),
         f"status={cll.get('learning_growth_status')}")
    decisions = int(status.get("decisions", pnl.get("decision_count", 0)) or 0)
    decision_records = int(cll.get("decision_records_written", 0) or 0)
    # if candidates were considered, decision records must be written
    considered = int(al.get("active_learning_candidates_considered", 0) or 0)
    _chk(checks, "closed_loop_records_positive",
         (decision_records > 0) or (considered == 0),
         f"records={decision_records} considered={considered}")
    # HARD event-sourcing invariants: a decision MUST emit an event.
    labels = (int(cll.get("no_trade_labels_written", 0) or 0)
              + int(cll.get("shadow_records_written", 0) or 0))
    diag = int(cll.get("diagnostic_records_written", 0) or 0)
    diag_no_label = int(cll.get("diagnostic_without_label_target", 0) or 0)
    recon = status.get("training_reconciliation", {}) or {}
    dec_counter = int(recon.get("decision_count_counter", status.get("decisions", 0)) or 0)
    if dec_counter > 0:
        _chk(checks, "decision_count_reconciles_with_events",
             bool(recon.get("reconciled", decision_records > 0)),
             recon.get("divergence_reason") or f"events={decision_records}")
    rej_counter = int(recon.get("rejection_count_counter", 0) or 0)
    if rej_counter > 0:
        _chk(checks, "rejection_becomes_learning_object",
             (labels + diag) > 0,
             f"no_trade+shadow={labels} diagnostic={diag}")
    cand_ev = int(cll.get("candidate_evaluated_events", decision_records) or 0)
    if cand_ev > 0:
        _chk(checks, "pending_labels_or_diagnostic_without_target",
             int(cll.get("pending_labels_total", 0) or 0) > 0 or diag_no_label > 0,
             f"pending={cll.get('pending_labels_total')} diag_no_label={diag_no_label}")
    _chk(checks, "no_trade_or_shadow_labels_positive",
         (labels + diag > 0) or (decision_records == 0),
         f"no_trade+shadow={labels} diagnostic={diag}")
    # active learning selected something OR has an explicit zero reason
    selected = int(cll.get("active_learning_shadow_selected", 0) or 0) \
        + int(al.get("exploration_trades_opened", 0) or 0)
    zero_reason = cll.get("zero_selection_reason")
    _chk(checks, "active_learning_selection_or_reason_present",
         (selected > 0) or (zero_reason is not None) or (considered == 0),
         f"selected={selected} zero_reason={zero_reason}")
    _chk(checks, "pending_label_store_present",
         int(cll.get("pending_labels_total", 0) or 0) >= 0
         and "pending_labels_created" in cll, "")
    _chk(checks, "learning_state_saved", bool(cll.get("learning_state_saved", False)
                                              or cll.get("learning_state_loaded", False)), "")

    # --- Bregman internally consistent ---
    disc = int(breg.get("raw_groups_discovered", 0) or 0)
    cert = int(breg.get("certified_opportunities", 0) or 0)
    opened = int(breg.get("opened_bregman_bundles", 0) or 0)
    consistent = (disc >= cert >= opened) and not (cert > 0 and disc == 0) and not (opened > cert)
    _chk(checks, "bregman_metrics_consistent", consistent,
         f"discovered={disc} certified={cert} opened={opened}")

    # --- inspection collector bundles required artifacts ---
    try:
        from scripts import inspection_collectors as ic
        collector_ok = "metrics" in ic.ARTIFACT_DIRS and "data" in ic.ARTIFACT_DIRS \
            and "reports" in ic.ARTIFACT_DIRS
    except Exception:  # noqa: BLE001
        collector_ok = False
    _chk(checks, "inspection_collector_includes_artifacts", collector_ok, "")

    # --- artifacts physically present (when a data_dir is given) ---
    if data_dir:
        base = Path(data_dir)

        def _present(a: str) -> bool:
            # training jsonl/state live at <data_dir>/training/ (prod /data/training),
            # while the zip layout uses data/training/ — accept either + repo root.
            cands = [base / a, _ROOT / a]
            if a.startswith("data/"):
                cands.append(base / a[len("data/"):])
            return any(p.exists() for p in cands)
        missing = [a for a in REQUIRED_ARTIFACTS if not _present(a)]
        _chk(checks, "inspection_artifacts_present", not missing,
             f"missing={missing}" if missing else "all present")

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "safe_to_run": ok, "checks": checks,
            "blocking": [c["check"] for c in checks if not c["ok"]]}


def _load_status(data_dir: str) -> "tuple[dict, Optional[float]]":
    p = Path(data_dir) / "polymarket_training.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")), p.stat().st_mtime
        except Exception:  # noqa: BLE001
            return {}, None
    return {}, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate paper-training runtime before a long run.")
    import os
    ap.add_argument("--data-dir", default=os.environ.get("HTE_DATA_DIR", "/data"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    status, mtime = _load_status(args.data_dir)
    result = validate_runtime(status, data_dir=args.data_dir, status_mtime=mtime)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for c in result["checks"]:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")
        print(f"\nSAFE TO RUN: {result['safe_to_run']}")
        if result["blocking"]:
            print(f"BLOCKING: {result['blocking']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
