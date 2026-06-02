#!/usr/bin/env python3
"""Read-only Kalshi smoke test (Phase 6).

Verifies Kalshi config, optionally syncs a few open markets, and reports a health
summary. NEVER calls order endpoints. Never prints secrets. Degrades gracefully
when credentials are missing.

Examples:
  python scripts/kalshi_readonly_smoke.py --env demo --max-markets 5 --seconds 30
  python scripts/kalshi_readonly_smoke.py --ticker FED-23DEC-T3.00 --seconds 30
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
    ap = argparse.ArgumentParser(description="Read-only Kalshi market-data smoke (no orders)")
    ap.add_argument("--env", default=os.getenv("KALSHI_ENV", "demo"), choices=["demo", "prod"])
    ap.add_argument("--max-markets", type=int, default=5)
    ap.add_argument("--ticker", action="append", default=None, help="market_ticker (repeatable)")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    os.environ["KALSHI_ENV"] = args.env
    from engine.storage import Store
    from engine.venues.kalshi.smoke import run_smoke

    store = Store(Path(args.db or _default_db()))
    summary = run_smoke(store=store, max_markets=args.max_markets, tickers=args.ticker,
                        seconds=args.seconds, do_sync=not args.no_sync)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
