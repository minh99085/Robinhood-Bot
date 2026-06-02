#!/usr/bin/env python3
"""Sync venue-neutral prediction-market metadata (Phase 6, read-only).

Writes venue_markets, venue_series, and resolution_rules for the requested venue.
NEVER calls order endpoints.

Examples:
  python scripts/sync_prediction_markets.py --venue kalshi --status open --max-markets 25
  python scripts/sync_prediction_markets.py --venue polymarket
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
    ap = argparse.ArgumentParser(description="Sync venue-neutral prediction-market metadata (no orders)")
    ap.add_argument("--venue", default="kalshi", choices=["polymarket", "kalshi"])
    ap.add_argument("--status", default="open")
    ap.add_argument("--max-markets", type=int, default=100)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    from engine.venues import MarketFilter, build_default_registry

    store = Store(Path(args.db or _default_db()))
    reg = build_default_registry(store=store)
    adapter = reg.get(args.venue)
    if adapter is None or not hasattr(adapter, "sync_metadata"):
        print(json.dumps({"venue": args.venue, "error": "venue unavailable"}))
        return 1
    res = adapter.sync_metadata(MarketFilter(venue=args.venue, status=args.status,
                                             limit=args.max_markets))
    print(json.dumps(res.model_dump(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
