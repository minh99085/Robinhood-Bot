#!/usr/bin/env python3
"""Show a post-canary report (Phase 10)."""

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
    ap = argparse.ArgumentParser(description="Post-canary report view")
    ap.add_argument("--analysis-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or _default_db()))
    reports = store.get_post_canary_reports(200)
    if args.analysis_id:
        reports = [r for r in reports if r.get("analysis_id") == args.analysis_id]
    rep = reports[0] if reports else None
    if not rep:
        print("no post-canary report found")
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
