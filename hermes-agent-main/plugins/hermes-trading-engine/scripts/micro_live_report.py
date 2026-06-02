#!/usr/bin/env python3
"""Show / locate the latest micro-live report (Phase 9)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _micro_live_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Micro-live report view")
    ap.add_argument("--order-attempt-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    reports = store.get_micro_live_reports(100)
    if args.order_attempt_id:
        reports = [r for r in reports if r.get("live_order_attempt_id") == args.order_attempt_id]
    rep = reports[0] if reports else None
    if not rep:
        print("no micro-live report found")
        return 1
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"report {rep['report_id']} status={rep.get('status')}")
        print(f"  path: {rep.get('report_path')}")
        p = rep.get("report_path")
        if p and Path(p).exists():
            print("---")
            print(Path(p).read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
