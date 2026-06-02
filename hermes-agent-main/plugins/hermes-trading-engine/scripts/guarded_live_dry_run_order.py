#!/usr/bin/env python3
"""Create a DRY-RUN order intent (Phase 8). Maps an internal order to a would-be
venue payload for VALIDATION only — UNSIGNED, UNSENT, no network, no signing."""

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
    ap = argparse.ArgumentParser(description="Create a DRY-RUN order intent (no network/signing)")
    ap.add_argument("--venue", default="kalshi", choices=["polymarket", "kalshi"])
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--asset-id", default=None)
    ap.add_argument("--outcome", default="YES")
    ap.add_argument("--side", default="BUY")
    ap.add_argument("--price", type=float, default=0.45)
    ap.add_argument("--quantity", type=float, default=1)
    ap.add_argument("--fixture", default=None, help="optional armed-state fixture (informational)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import DryRunLiveBroker, GuardedLiveConfig, SafetyEnvelope
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    cfg = GuardedLiveConfig.from_env()
    order = {"venue": args.venue, "market_ticker": args.ticker, "market_id": args.ticker,
             "asset_id": args.asset_id, "outcome": args.outcome, "side": args.side,
             "price": args.price, "quantity": args.quantity, "order_type": "LIMIT"}
    safe = SafetyEnvelope(cfg, state="ARMED_DRY_RUN_ONLY").validate(order)
    store.add_safety_envelope_decision(safe.record())
    intent = DryRunLiveBroker(store, cfg).validate_order(
        order, risk_decision_id="rd-stub", safety_envelope_decision_id=safe.decision_id)
    print(json.dumps({
        "dry_run_intent_id": intent.dry_run_intent_id, "status": intent.status,
        "reason": intent.reason, "venue_payload": intent.venue_payload,
        "unsigned": intent.unsigned, "unsent": intent.unsent,
        "signer_used": intent.signer_used, "network_called": intent.network_called,
        "no_live_execution": True}, indent=2, default=str))
    print("No live orders were submitted. Real execution remains DISABLED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
