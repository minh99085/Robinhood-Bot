#!/usr/bin/env python3
"""Production-review veto exit-code wrapper (Phase 11).

Exit codes:
- NOT_READY / FIX_AND_REPEAT_* -> nonzero
- READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW -> zero
- APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN -> zero (only the DESIGN plan)
  unless --fail-on-not-approved-to-draft-phase12 requires this exact outcome.
Any forbidden production-execution outcome is always treated as failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _production_review_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Production-review veto exit-code gate")
    ap.add_argument("--review-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--fail-on-not-approved-to-draft-phase12", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.production_review import FORBIDDEN_PRODUCTION_RECOMMENDATIONS
    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    runs = store.get_production_review_runs(200)
    if args.review_id:
        runs = [r for r in runs if r.get("review_id") == args.review_id]
    run = runs[0] if runs else None
    if not run:
        print("no production-review run found")
        return 2
    rec = run.get("recommendation")

    if rec in FORBIDDEN_PRODUCTION_RECOMMENDATIONS or \
            int(run.get("eligible_for_production_execution") or 0) or \
            int(run.get("eligible_for_size_increase") or 0) or \
            int(run.get("eligible_for_autonomous_live") or 0):
        code = 3
    elif rec == "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN":
        code = 0
    elif rec == "READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW":
        code = 1 if args.fail_on_not_approved_to_draft_phase12 else 0
    else:  # NOT_READY / FIX_AND_REPEAT_*
        code = 1

    out = {"recommendation": rec, "status": run.get("status"), "exit_code": code,
           "production_execution": "NOT_IMPLEMENTED", "size_increase": "NOT_APPROVED",
           "autonomous_live": "NOT_APPROVED"}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"recommendation={rec} status={run.get('status')} -> exit {code}")
        print("production_execution=NOT_IMPLEMENTED size_increase=NOT_APPROVED "
              "autonomous_live=NOT_APPROVED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
