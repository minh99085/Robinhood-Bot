#!/usr/bin/env python3
"""Re-run all micro-live gates for a canary plan (Phase 9). Does NOT submit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _micro_live_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Micro-live preflight (no submit)")
    ap.add_argument("--canary-plan-id", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.micro_live import MicroLiveConfig
    from engine.micro_live.preflight import preflight_canary_plan
    from engine.micro_live.schemas import MicroLiveCanaryPlan
    from engine.storage import Store

    store = Store(Path(args.db or default_db()))
    cfg = MicroLiveConfig.from_env()
    row = store.get_micro_live_canary_plan(args.canary_plan_id)
    if not row:
        print("canary plan not found")
        return 1
    plan = MicroLiveCanaryPlan(**{k: row.get(k) for k in MicroLiveCanaryPlan.model_fields
                                  if k in row})
    result, safety, risk = preflight_canary_plan(store, cfg, plan)
    out = {"preflight_id": result.preflight_id, "status": result.status,
           "hard_fail_count": result.hard_fail_count, "risk": result.risk_status,
           "safety": result.safety_status, "readiness": result.readiness_status,
           "approval": result.approval_status, "arming": result.arming_status,
           "locks": [{"lock": r.lock_name, "passed": r.passed} for r in result.lock_results],
           "no_submit": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"preflight: {result.status}  hard_fail={result.hard_fail_count}")
        print(f"  risk={result.risk_status} safety={result.safety_status} "
              f"readiness={result.readiness_status} approval={result.approval_status} "
              f"arming={result.arming_status}")
        print("No order was submitted.")
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
