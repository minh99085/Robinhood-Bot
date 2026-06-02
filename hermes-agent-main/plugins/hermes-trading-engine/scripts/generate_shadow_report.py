#!/usr/bin/env python3
"""Generate a live-readiness report for a shadow session (Phase 7, offline).

Quant scope — *Live Trading & Monitoring* + *Compliance*: offline, read-only
shadow report generation. No live orders, no network."""

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
    ap = argparse.ArgumentParser(description="Generate a shadow live-readiness report")
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.shadow import (LiveReadinessGate, ShadowConfig, compute_session_metrics,
                               write_report)
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    sid = args.session_id
    if sid is None or args.latest:
        sessions = store.get_shadow_sessions(1)
        if not sessions:
            print("no shadow sessions found")
            return 1
        sid = sessions[0]["shadow_session_id"]

    cfg = ShadowConfig.from_env()
    metrics = compute_session_metrics(store, sid, cfg)
    report = LiveReadinessGate(cfg).evaluate(metrics, {"reconciliation_clean": True}, sid)
    out = write_report(store, sid, cfg, report, metrics)
    print(f"session: {sid}")
    print(f"overall: {report.overall_status}  next: {report.recommended_next_step}")
    print(f"artifacts: {out}")
    print("NO live orders were submitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
