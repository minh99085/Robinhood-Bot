#!/usr/bin/env python3
"""Show a production-review dossier report (Phase 11)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _production_review_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Production-review report view")
    ap.add_argument("--review-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    reports = store.get_production_review_reports(200)
    if args.review_id:
        reports = [r for r in reports if r.get("review_id") == args.review_id]
    rep = reports[0] if reports else None
    if not rep:
        print("no production-review report found")
        return 1
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"report {rep['report_id']} recommendation={rep.get('recommendation')} "
              f"status={rep.get('status')}")
        p = rep.get("report_path")
        if p and Path(p).exists():
            print("---")
            print(Path(p).read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
