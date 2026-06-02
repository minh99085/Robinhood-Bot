#!/usr/bin/env python3
"""Submit ONE micro-live canary order (Phase 9).

This is the ONLY path that can place a real order, and only when EVERY lock and
gate is open. It requires a TTY and a typed confirmation by default. With
``--non-interactive-test-fixture`` it uses a MOCKED exchange transport/signer and
performs no real network call (for tests/demos only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _micro_live_common import default_db

CONFIRM = "SUBMIT ONE MICRO LIVE CANARY ORDER"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Submit one micro-live canary order (CLI-only)")
    ap.add_argument("--canary-plan-id", required=True)
    ap.add_argument("--arming-token", default="")
    ap.add_argument("--confirm", default=None, help=f'must be: "{CONFIRM}"')
    ap.add_argument("--non-interactive-test-fixture", action="store_true",
                    help="use mocked exchange (tests/demos only) - no real network")
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import os

    from engine.micro_live import MicroLiveConfig
    from engine.micro_live import locks as _locks_mod
    from engine.micro_live.config import REQUIRED_ACK_PHRASE
    from engine.micro_live.execution_service import (FixtureSigner, MicroLiveExecutionService,
                                                     fixture_transport)
    from engine.micro_live.schemas import MicroLiveCanaryPlan
    from engine.storage import Store

    # Fixture mode uses a MOCKED exchange (no real network is reachable); open the
    # build + runtime locks IN-PROCESS so the demo path can run end-to-end safely.
    if args.non_interactive_test_fixture:
        _locks_mod.BUILD_ENABLED = True
        os.environ.setdefault("MICRO_LIVE_ENABLED", "1")
        os.environ.setdefault("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", REQUIRED_ACK_PHRASE)
        os.environ.setdefault("KALSHI_MICRO_LIVE_ENABLED", "1")

    store = Store(Path(args.db or default_db()))
    cfg = MicroLiveConfig.from_env()
    row = store.get_micro_live_canary_plan(args.canary_plan_id)
    if not row:
        print("canary plan not found")
        return 1
    plan = MicroLiveCanaryPlan(**{k: row.get(k) for k in MicroLiveCanaryPlan.model_fields
                                  if k in row})

    print("=" * 64)
    print("MICRO-LIVE CANARY SUBMIT — this may submit a REAL order if all locks are open")
    print(f"  venue:        {plan.venue}")
    print(f"  environment:  {plan.environment} (production_allowed={cfg.allow_production})")
    print(f"  market:       {plan.market_ticker or plan.market_id}")
    print(f"  side/outcome: {plan.side} / {plan.outcome}")
    print(f"  price:        {plan.limit_price}")
    print(f"  quantity:     {plan.quantity}")
    print(f"  notional:     {plan.notional} USD (hard cap {cfg.max_order_notional_usd})")
    print(f"  max loss:     <= {plan.notional} USD")
    print(f"  order type:   {plan.order_type}  TIF: {plan.time_in_force}")
    print("=" * 64)

    confirm = args.confirm
    fixture = args.non_interactive_test_fixture
    transport = signer = None
    if fixture:
        transport, signer = fixture_transport(fill=True), FixtureSigner()
        if confirm is None:
            confirm = CONFIRM
    else:
        if not sys.stdin.isatty():
            print("Refusing: a TTY is required for live submit (or use "
                  "--non-interactive-test-fixture for mocked tests).")
            return 2
        if confirm is None:
            confirm = input(f'Type exactly to confirm: "{CONFIRM}"\n> ').strip()

    svc = MicroLiveExecutionService(store, cfg)
    res = svc.submit_canary_order(
        args.canary_plan_id, arming_token=args.arming_token, confirm=confirm,
        market_ctx={"edge_after_costs": cfg.min_edge_after_costs, "evidence_score":
                    cfg.min_evidence_score, "source_count": cfg.min_source_count},
        transport=transport, signer=signer, cli_context=True,
        non_interactive_test_fixture=fixture)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        if res.get("blocked"):
            print(f"BLOCKED: {res.get('reason')}  (no order submitted)")
        else:
            print(f"submitted={res.get('submitted')} status={res.get('status')} "
                  f"network_calls={res.get('network_call_count')} next_step={res.get('next_step')}")
            print(f"report: {res.get('report_path')}")
    return 0 if (res.get("submitted") or not res.get("blocked")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
