#!/usr/bin/env python3
"""Import realized market outcomes (for realized calibration) from a CSV.

CSV columns: venue, market_id, asset_id (optional), outcome (optional),
resolved_ts_ms, realized_outcome (0/1), payout_price (optional).

Stored into the ``market_outcomes`` table. No network. No secrets.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.storage import Store  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import replay realized outcomes from CSV")
    ap.add_argument("csv_path")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    db_path = args.db or os.path.join(os.getenv("HTE_DATA_DIR", "."), "trading_engine.sqlite3")
    store = Store(Path(db_path))
    n = 0
    with open(args.csv_path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                store.upsert_market_outcome({
                    "venue": row.get("venue") or "",
                    "market_id": row.get("market_id") or "",
                    "asset_id": row.get("asset_id") or None,
                    "outcome": row.get("outcome") or None,
                    "resolved_ts_ms": int(row["resolved_ts_ms"]) if row.get("resolved_ts_ms") else None,
                    "realized_outcome": int(row["realized_outcome"]) if row.get("realized_outcome") not in (None, "") else None,
                    "payout_price": row.get("payout_price") or None,
                    "source": "csv_import", "payload_json": None})
                n += 1
            except Exception as exc:  # noqa: BLE001
                print(f"skip row {row}: {exc}", file=sys.stderr)
    print(f"imported {n} outcomes into {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
