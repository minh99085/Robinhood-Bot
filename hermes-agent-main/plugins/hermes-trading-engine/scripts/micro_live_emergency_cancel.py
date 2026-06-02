#!/usr/bin/env python3
"""Emergency-cancel a micro-live order (Phase 9). Explicit + audited. Fails
closed unless a target and the exact typed confirmation are provided. This NEVER
places a new order."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _micro_live_common import default_db

CONFIRM = "EMERGENCY CANCEL MICRO LIVE ORDER"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Emergency cancel a micro-live order")
    ap.add_argument("--order-attempt-id", default=None)
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--environment", default="demo")
    ap.add_argument("--market-ticker", default=None)
    ap.add_argument("--cancel-all", action="store_true")
    ap.add_argument("--confirm", default="", help=f'must be: "{CONFIRM}"')
    ap.add_argument("--requested-by", default="cli")
    ap.add_argument("--non-interactive-test-fixture", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.micro_live import MicroLiveConfig
    from engine.micro_live.execution_service import (FixtureSigner, MicroLiveExecutionService,
                                                     fixture_transport)
    from engine.storage import Store

    store = Store(Path(args.db or default_db()))
    cfg = MicroLiveConfig.from_env()
    svc = MicroLiveExecutionService(store, cfg)

    order_id = None
    if args.order_attempt_id:
        a = store.get_micro_live_attempt(args.order_attempt_id)
        order_id = (a or {}).get("exchange_order_id") or (a or {}).get("client_order_id")

    transport = signer = None
    if args.non_interactive_test_fixture:
        transport, signer = fixture_transport(), FixtureSigner()

    res = svc.emergency_cancel(
        venue=args.venue, environment=args.environment, confirm=args.confirm,
        requested_by=args.requested_by, order_id=order_id,
        market_ticker=args.market_ticker, cancel_all=args.cancel_all,
        transport=transport, signer=signer, cli_context=True)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"emergency_cancel: sent={res.get('sent')} reason={res.get('reason', '')} "
              f"success={res.get('success')}")
    return 0 if res.get("sent") else 1


if __name__ == "__main__":
    raise SystemExit(main())
