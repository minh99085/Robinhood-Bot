#!/usr/bin/env python3
"""Guarded-live precheck (Phase 8). Fails closed unless ALL dry-run conditions
are met. Real execution remains disabled regardless of outcome."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
    ap = argparse.ArgumentParser(description="Guarded-live precheck (dry-run only)")
    ap.add_argument("--readiness-report-id", default=None)
    ap.add_argument("--readiness-report-fixture", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig, run_precheck
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    cfg = GuardedLiveConfig.from_env()
    report_id = args.readiness_report_id

    if args.readiness_report_fixture:
        rep = json.loads(Path(args.readiness_report_fixture).read_text())
        rep.setdefault("report_id", "fixture-readiness")
        rep["generated_ts_ms"] = int(time.time() * 1000)  # fresh for age check
        store.add_readiness_report({
            "report_id": rep["report_id"], "shadow_session_id": rep.get("shadow_session_id", "fx"),
            "generated_ts_ms": rep["generated_ts_ms"],
            "overall_status": rep.get("overall_status", "READY_FOR_MANUAL_REVIEW"),
            "summary_json": rep.get("metrics_summary", {}), "report_path": None})
        report_id = rep["report_id"]

    conf = ConformanceHarness(store=store, config=cfg).run()
    pre = run_precheck(store, cfg, readiness_report_id=report_id,
                       conformance_ok=(conf.status == "PASS"))
    out = {"precheck_id": pre.precheck_id, "status": pre.status,
           "hard_fail_count": pre.hard_fail_count, "conformance": conf.status,
           "failed_checks": [c.check_name for c in pre.checks if c.status == "FAIL"],
           "no_live_execution": True, "real_execution_disabled": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"precheck: {pre.status}  conformance: {conf.status}")
        for c in pre.checks:
            print(f"  [{c.status}] {c.check_name} {('- ' + c.reason) if c.reason else ''}")
        print("No live orders were submitted. Real execution remains DISABLED.")
    return 0 if pre.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
