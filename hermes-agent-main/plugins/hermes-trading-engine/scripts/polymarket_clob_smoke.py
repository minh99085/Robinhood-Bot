#!/usr/bin/env python3
"""Read-only smoke test for the Polymarket CLOB market-data feed.

Fetches a few trending Polymarket markets (Gamma), extracts up to N CLOB token
(asset) ids, connects to the public market WebSocket READ-ONLY, subscribes, and
prints BBO / order book health for ~30 seconds. Optionally persists raw events
to a local SQLite DB. Requires NO wallet and NO private key.

Run from the plugin root:
    python scripts/polymarket_clob_smoke.py
    python scripts/polymarket_clob_smoke.py --seconds 20 --max-assets 10

Env (all optional):
    POLYMARKET_WS_URL, POLYMARKET_CLOB_MAX_ASSETS, POLYMARKET_CLOB_STALE_MS,
    POLYMARKET_CLOB_PERSIST_RAW, HTE_DATA_DIR
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# Make `engine` importable when run as a plain script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.feeds import polymarket  # noqa: E402
from engine.market_data.event_store import RawEventStore  # noqa: E402
from engine.market_data.polymarket_ws import MarketDataManager  # noqa: E402
from engine.storage import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket CLOB read-only smoke test")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--max-assets", type=int,
                    default=int(os.getenv("POLYMARKET_CLOB_MAX_ASSETS", "10") or 10))
    ap.add_argument("--markets", type=int, default=6)
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    persist = not args.no_persist and os.getenv("POLYMARKET_CLOB_PERSIST_RAW", "1") not in ("0", "false", "False")
    data_dir = Path(os.getenv("HTE_DATA_DIR") or tempfile.mkdtemp(prefix="hte-clob-smoke-"))
    data_dir.mkdir(parents=True, exist_ok=True)

    print("== Polymarket CLOB smoke (READ-ONLY; no wallet, no orders) ==")
    print(f"   ws url     : {os.getenv('POLYMARKET_WS_URL') or 'wss://ws-subscriptions-clob.polymarket.com/ws/market'}")
    print(f"   persist    : {persist} -> {data_dir / 'trading_engine.sqlite3' if persist else 'disabled'}")

    print("\n-> fetching trending Polymarket markets (Gamma)...")
    try:
        markets = polymarket.get_trending_markets(limit=args.markets)
    except Exception as exc:  # noqa: BLE001
        print(f"   gamma fetch failed: {exc}")
        markets = []
    asset_map = polymarket.clob_asset_map(markets)
    n_assets = sum(len(v) for v in asset_map.values())
    print(f"   markets={len(markets)} markets_with_tokens={len(asset_map)} token_ids={n_assets}")
    if not asset_map:
        print("   no CLOB token ids available (network blocked or none listed); "
              "continuing to test connection only.")

    store = Store(data_dir / "trading_engine.sqlite3") if persist else None
    event_store = RawEventStore(store) if store else None
    mgr = MarketDataManager(
        event_store=event_store,
        url=os.getenv("POLYMARKET_WS_URL") or None,
        stale_ms=int(os.getenv("POLYMARKET_CLOB_STALE_MS", "3000") or 3000),
        persist_raw=persist, max_assets=args.max_assets)
    mgr.start()
    if asset_map:
        mgr.ensure_subscribed(asset_map)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(3)
            st = mgr.get_status()
            print(f"   [{int(deadline - time.time()):>3}s] status={st['status']:<12} "
                  f"msgs={st['messages_received']:<5} parse_err={st['parse_errors']:<3} "
                  f"subs={st['subscribed_asset_count']:<3} tracked={st.get('tracked_asset_count', 0):<3} "
                  f"stale={st['stale_asset_count']:<3} reconnects={st['reconnect_count']}")
            for a in mgr.health().get("assets", [])[:3]:
                print(f"        {str(a['asset_id'])[:18]:<20} bid={a['best_bid']} ask={a['best_ask']} "
                      f"age_ms={a['age_ms']} stale={a['stale']}")
    except KeyboardInterrupt:
        print("   interrupted")
    finally:
        mgr.stop()
        time.sleep(0.3)

    final = mgr.get_status()
    if event_store:
        print(f"\n   persisted raw events: {len(event_store.get_recent_events(1000))}")
    print(f"   final status={final['status']} messages={final['messages_received']} "
          f"parse_errors={final['parse_errors']}")
    print("== done (no orders were ever submitted) ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
