#!/usr/bin/env python3
"""Run post-canary analysis for a micro-live canary attempt (Phase 10).

Read-only: NEVER submits or cancels orders. Optional read-only exchange refresh
is disabled by default. Produces a hard veto recommendation and artifacts.
"""

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
    ap = argparse.ArgumentParser(description="Post-canary analysis (no submit/cancel)")
    ap.add_argument("--live-order-attempt-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--refresh-readonly-exchange-state", action="store_true")
    ap.add_argument("--fixture", default=None, help="JSON analysis-context fixture (mocked)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.post_canary import (PostCanaryAnalysisRequest, PostCanaryAnalyzer,
                                    PostCanaryConfig)
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    cfg = PostCanaryConfig.from_env()
    fixture = None
    attempt_id = args.live_order_attempt_id

    if args.fixture:
        fixture = json.loads(Path(args.fixture).read_text())
        attempt_id = (fixture.get("attempt") or {}).get("live_order_attempt_id", "fixture-attempt")
    elif args.latest:
        rows = store.get_micro_live_attempts(1)
        if not rows:
            print("no micro-live attempts found")
            return 1
        attempt_id = rows[0]["live_order_attempt_id"]

    if not attempt_id:
        print("--live-order-attempt-id, --latest, or --fixture required")
        return 2

    req = PostCanaryAnalysisRequest(
        live_order_attempt_id=attempt_id,
        refresh_readonly_exchange_state=bool(args.refresh_readonly_exchange_state
                                             and cfg.allow_readonly_refresh),
        generated_by="cli")
    res = PostCanaryAnalyzer(store, cfg).analyze(req, fixture=fixture)
    out = {"analysis_id": res.analysis_id, "status": res.status,
           "recommendation": res.recommendation, "hard_fail_count": res.hard_fail_count,
           "warning_count": res.warning_count, "unknown_blocking_count": res.unknown_blocking_count,
           "blocking_reasons": res.blocking_reasons, "eligible_for_size_increase": False,
           "eligible_for_autonomous_live": False, "no_execution": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"recommendation: {res.recommendation}  status: {res.status}")
        for r in res.blocking_reasons:
            print(f"  blocking: {r}")
        print("No order was submitted or cancelled. No scaling/production is approved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
