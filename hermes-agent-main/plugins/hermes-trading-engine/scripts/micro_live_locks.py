#!/usr/bin/env python3
"""Show micro-live lock status (Phase 9). Live submit is blocked by default."""

from __future__ import annotations

import argparse
import json

from _micro_live_common import default_db  # noqa: F401  (ensures sys.path bootstrap)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Show micro-live locks (live disabled by default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from engine.micro_live import MicroLiveConfig, all_pass, check_locks
    cfg = MicroLiveConfig.from_env()
    res = check_locks(cfg)
    ok = all_pass(res)
    out = {"all_locks_open": ok, "live_submit_blocked": not ok,
           "environment": cfg.environment, "production_allowed": cfg.allow_production,
           "max_order_notional_usd": str(cfg.max_order_notional_usd),
           "allowed_venues": cfg.allowed_venues, "allowed_order_types": cfg.allowed_order_types,
           "allowed_tif": cfg.allowed_tif,
           "locks": [{"lock_name": r.lock_name, "passed": r.passed, "reason": r.reason}
                     for r in res]}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"micro-live: all_locks_open={ok}  live_submit_blocked={not ok}")
        for r in res:
            print(f"  [{'OPEN' if r.passed else 'CLOSED'}] {r.lock_name}"
                  f"{(' - ' + r.reason) if (r.reason and not r.passed) else ''}")
        print("Real order submission is CLI-only, one-canary-only, and blocked unless ALL "
              "locks/gates are open. Default = disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
