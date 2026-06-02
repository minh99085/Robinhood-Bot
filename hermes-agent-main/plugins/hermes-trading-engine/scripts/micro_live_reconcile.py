#!/usr/bin/env python3
"""Show reconciliation state for a micro-live order attempt (Phase 9)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _micro_live_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Micro-live reconciliation view")
    ap.add_argument("--order-attempt-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or default_db()))

    attempt = None
    if args.order_attempt_id:
        attempt = store.get_micro_live_attempt(args.order_attempt_id)
    elif args.latest:
        rows = store.get_micro_live_attempts(1)
        attempt = rows[0] if rows else None
    if not attempt:
        print("no order attempt found")
        return 1
    recons = [r for r in store.get_micro_live_reconciliations(100)
              if r.get("live_order_attempt_id") == attempt["live_order_attempt_id"]]
    out = {"live_order_attempt_id": attempt["live_order_attempt_id"], "status": attempt["status"],
           "filled_quantity": attempt.get("filled_quantity"),
           "reconciliations": recons}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"attempt {attempt['live_order_attempt_id']} status={attempt['status']} "
              f"filled={attempt.get('filled_quantity')}")
        for r in recons:
            print(f"  recon {r['status']} exch={r.get('exchange_order_status')} "
                  f"local={r.get('local_order_status')} disc={r.get('discrepancies_json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
