#!/usr/bin/env python3
"""Post-canary veto gate exit-code wrapper (Phase 10).

Exit codes:
- STOP -> nonzero
- FIX_AND_REPEAT_SHADOW -> nonzero if --fail-on-stop or --fail-on-not-repeat-ready
- REPEAT_DEMO_CANARY_SAME_SIZE -> zero (repeat-demo eligibility, NOT scaling)
- MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN -> zero only with --allow-design-review-success
Any size/autonomous/production-execution status is always treated as failure.
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
    ap = argparse.ArgumentParser(description="Post-canary veto exit-code gate")
    ap.add_argument("--analysis-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--fail-on-stop", action="store_true")
    ap.add_argument("--fail-on-any-warning", action="store_true")
    ap.add_argument("--fail-on-not-repeat-ready", action="store_true")
    ap.add_argument("--allow-design-review-success", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.post_canary import FORBIDDEN_RECOMMENDATIONS
    from engine.storage import Store
    store = Store(Path(args.db or _default_db()))
    analyses = store.get_post_canary_analyses(200)
    if args.analysis_id:
        analyses = [a for a in analyses if a.get("analysis_id") == args.analysis_id]
    an = analyses[0] if analyses else None
    if not an:
        print("no analysis found")
        return 2
    rec = an.get("recommendation")
    status = an.get("status")

    # forbidden outcomes can never succeed
    if rec in FORBIDDEN_RECOMMENDATIONS or int(an.get("eligible_for_size_increase") or 0) or \
            int(an.get("eligible_for_autonomous_live") or 0):
        code = 3
    elif rec == "STOP":
        code = 1
    elif rec == "FIX_AND_REPEAT_SHADOW":
        code = 1 if (args.fail_on_stop or args.fail_on_not_repeat_ready) else 0
    elif rec == "REPEAT_DEMO_CANARY_SAME_SIZE":
        code = 0
    elif rec == "MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN":
        code = 0 if args.allow_design_review_success else 1
    elif rec == "MANUAL_REVIEW_FOR_NEXT_PHASE":
        code = 0 if args.allow_design_review_success else 1
    else:
        code = 1
    if args.fail_on_any_warning and int(an.get("warning_count", 0)) > 0 and code == 0:
        code = 1

    out = {"recommendation": rec, "status": status, "exit_code": code,
           "size_increase": False, "autonomous_live": False,
           "production_execution": "NOT_IMPLEMENTED"}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"recommendation={rec} status={status} -> exit {code}")
        print("size_increase=NO autonomous_live=NO production_execution=NOT_IMPLEMENTED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
