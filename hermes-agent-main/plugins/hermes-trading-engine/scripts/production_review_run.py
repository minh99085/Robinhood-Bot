#!/usr/bin/env python3
"""Run the production-canary DESIGN REVIEW (Phase 11).

Read-only: NEVER submits/cancels/signs production orders, NEVER moves funds,
NEVER enables production. Maximum positive result only approves drafting a
Phase 12 production-canary PLAN.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _production_review_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Production-canary design review (no execution)")
    ap.add_argument("--include-mock-conformance", action="store_true")
    ap.add_argument("--fixture", default=None, help="JSON evidence/context fixture (mocked)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.production_review import (ProductionReviewConfig, ProductionReviewRequest,
                                          run_review)
    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    cfg = ProductionReviewConfig.from_env()
    fixture = json.loads(Path(args.fixture).read_text()) if args.fixture else None
    req = ProductionReviewRequest(
        generated_by="cli",
        include_mock_production_conformance=(args.include_mock_conformance or fixture is not None
                                             or True))
    res = run_review(store, cfg, request=req, fixture=fixture)
    out = {"review_id": res.review_id, "status": res.status, "recommendation": res.recommendation,
           "eligible_to_draft_phase12_plan": res.eligible_to_draft_phase12_plan,
           "eligible_for_production_execution": False, "eligible_for_size_increase": False,
           "eligible_for_autonomous_live": False, "blocking_reasons": res.blocking_reasons,
           "no_execution": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"recommendation: {res.recommendation}  status: {res.status}")
        for r in res.blocking_reasons:
            print(f"  blocking: {r}")
        print("Production execution remains UNIMPLEMENTED in Phase 11. No orders/cancels/funds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
