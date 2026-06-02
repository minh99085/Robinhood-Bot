#!/usr/bin/env python3
"""Guarded-live conformance harness (Phase 8). Proves no real network/order/
signing path exists. Fails if any trap fires."""

from __future__ import annotations

import argparse
import json
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
    ap = argparse.ArgumentParser(description="Guarded-live conformance (no live execution)")
    ap.add_argument("--fail-on-warning", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    run = ConformanceHarness(store=store, config=GuardedLiveConfig.from_env()).run()
    if args.json:
        print(json.dumps({"conformance_run_id": run.conformance_run_id, "status": run.status,
                          "pass": run.pass_count, "fail": run.fail_count,
                          "checks": [{"name": c.check_name, "status": c.status} for c in run.checks]},
                         indent=2))
    else:
        for c in run.checks:
            print(f"  [{c.status}] {c.check_name} {('- ' + c.reason) if c.reason else ''}")
        print(f"conformance: {run.status} ({run.pass_count}/{run.test_count})")
        print("No live orders were submitted. Real execution remains DISABLED.")
    bad = run.fail_count > 0 or (args.fail_on_warning and run.warning_count > 0)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
