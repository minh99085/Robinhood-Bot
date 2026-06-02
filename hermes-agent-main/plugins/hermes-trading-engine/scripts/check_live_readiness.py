#!/usr/bin/env python3
"""Check live readiness for a shadow session (Phase 7).

Exits nonzero unless the overall status is READY_FOR_MANUAL_REVIEW. This NEVER
enables live trading — READY_FOR_MANUAL_REVIEW only means a human may begin
designing a future guarded-live phase.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _default_db() -> str:
    try:
        from engine.config import settings
        return str(settings.db_path)
    except Exception:  # noqa: BLE001
        return os.getenv("HTE_DB_PATH", "trading.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check shadow live readiness (never enables live)")
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--fail-on-not-ready", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.shadow import LiveReadinessGate, ShadowConfig, compute_session_metrics
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    sid = args.session_id
    if sid is None or args.latest:
        sessions = store.get_shadow_sessions(1)
        sid = sessions[0]["shadow_session_id"] if sessions else None

    overall = "NOT_ENOUGH_DATA"
    failed_gates: list = []
    # Prefer a stored readiness report if present (authoritative).
    reports = store.get_readiness_reports(sid, 1) if sid else store.get_readiness_reports(None, 1)
    if reports:
        overall = reports[0].get("overall_status") or overall
    elif sid:
        cfg = ShadowConfig.from_env()
        metrics = compute_session_metrics(store, sid, cfg)
        report = LiveReadinessGate(cfg).evaluate(metrics, {"reconciliation_clean": True}, sid)
        overall = report.overall_status
        failed_gates = [g.gate_name for g in report.gate_results if g.status == "FAIL"]

    print(f"session: {sid}")
    print(f"overall: {overall}")
    if failed_gates:
        print("failed gates: " + ", ".join(failed_gates))
    print("NOTE: READY_FOR_MANUAL_REVIEW does NOT enable live trading.")
    return 0 if overall == "READY_FOR_MANUAL_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
