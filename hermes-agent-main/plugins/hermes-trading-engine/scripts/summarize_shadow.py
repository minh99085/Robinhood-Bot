#!/usr/bin/env python3
"""Summarize a shadow session's metrics (Phase 7, offline)."""

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
    ap = argparse.ArgumentParser(description="Summarize a shadow session")
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.shadow import ShadowConfig, compute_session_metrics
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    sid = args.session_id
    if sid is None or args.latest:
        sessions = store.get_shadow_sessions(1)
        if not sessions:
            print("no shadow sessions found")
            return 1
        sid = sessions[0]["shadow_session_id"]

    metrics = compute_session_metrics(store, sid, ShadowConfig.from_env())
    metrics["shadow_session_id"] = sid
    metrics["no_live_orders"] = True
    if args.json:
        print(json.dumps(metrics, indent=2, default=str))
    else:
        for k in ("decision_count", "approved_shadow_order_count", "shadow_order_count",
                  "shadow_fill_count", "fill_ratio", "risk_rejection_rate", "reject_rate",
                  "total_fees"):
            print(f"  {k}: {metrics.get(k)}")
        print("  NO live orders were submitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
