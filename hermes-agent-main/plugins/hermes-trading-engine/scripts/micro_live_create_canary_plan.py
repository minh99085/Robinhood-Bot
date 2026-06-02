#!/usr/bin/env python3
"""Create a micro-live canary plan from an approved Phase 8 dry-run intent.

Read-only safety: this does NOT submit anything. The plan must still pass
preflight + arming + typed CLI confirmation before any submit is even attempted.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from _micro_live_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create a micro-live canary plan (no submit)")
    ap.add_argument("--dry-run-intent-id", default=None)
    ap.add_argument("--readiness-report-id", default=None)
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--environment", default="demo")
    ap.add_argument("--approval-batch-id", default=None)
    ap.add_argument("--arming-token-id", default=None)
    ap.add_argument("--fixture", default=None,
                    help="JSON file of a dry-run intent to seed (mocked exchange only)")
    ap.add_argument("--readiness-report-fixture", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.micro_live import MicroLiveConfig
    from engine.micro_live.canary_plan import create_canary_plan
    from engine.storage import Store

    store = Store(Path(args.db or default_db()))
    cfg = MicroLiveConfig.from_env()
    now = int(time.time() * 1000)
    dri_id = args.dry_run_intent_id
    rr_id = args.readiness_report_id

    if args.fixture:
        intent = json.loads(Path(args.fixture).read_text())
        intent.setdefault("dry_run_intent_id", "fixture-dri")
        intent.setdefault("ts_ms", now)
        intent.setdefault("unsigned", 1)
        intent.setdefault("unsent", 1)
        intent.setdefault("signer_used", 0)
        intent.setdefault("network_called", 0)
        intent.setdefault("safety_envelope_decision_id", "fixture-safe")
        store.add_dry_run_order_intent(intent)
        dri_id = intent["dry_run_intent_id"]
        # fixture/demo is self-contained: seed a passing Phase 8 conformance run
        store.add_conformance_run({
            "conformance_run_id": "fixture-conformance", "started_ts_ms": now,
            "finished_ts_ms": now, "status": "PASS", "config_hash": "fixture",
            "test_count": 1, "pass_count": 1, "fail_count": 0, "warning_count": 0,
            "report_path": None})
    if args.readiness_report_fixture:
        rep = json.loads(Path(args.readiness_report_fixture).read_text())
        rr_id = rep.get("report_id", "fixture-readiness")
        store.add_readiness_report({
            "report_id": rr_id, "shadow_session_id": rep.get("shadow_session_id", "fx"),
            "generated_ts_ms": now,
            "overall_status": rep.get("overall_status", "READY_FOR_MANUAL_REVIEW"),
            "summary_json": rep.get("metrics_summary", {}), "report_path": None})

    plan, errs = create_canary_plan(
        store, cfg, dry_run_intent_id=dri_id or "", readiness_report_id=rr_id,
        venue=args.venue, environment=args.environment,
        approval_batch_id=args.approval_batch_id, arming_token_id=args.arming_token_id,
        now_ms=now)
    out = {"canary_plan_id": plan.canary_plan_id, "status": plan.status, "errors": errs,
           "venue": plan.venue, "environment": plan.environment,
           "expires_ts_ms": plan.expires_ts_ms, "notional": str(plan.notional),
           "no_submit": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"canary_plan_id: {plan.canary_plan_id}  status: {plan.status}")
        if errs:
            print("  errors: " + "; ".join(errs))
        print("No order was submitted. Run preflight then the CLI submit workflow.")
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
