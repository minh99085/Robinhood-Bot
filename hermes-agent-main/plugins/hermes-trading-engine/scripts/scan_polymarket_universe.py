#!/usr/bin/env python3
"""Scan the Polymarket market catalog and build the adaptive universe.

Selection only — this NEVER places, cancels, or sizes an order. It fetches the
Gamma catalog (or reads a local JSON file with ``--from-json`` for offline use),
filters + scores + tiers the markets, and writes a status JSON the dashboard
reads. Live order-book subscription stays gated by ``POLYMARKET_CLOB_ENABLED``.

Examples
--------
    # offline (no network): build from a saved catalog file
    python scripts/scan_polymarket_universe.py --from-json catalog.jsonl --out /tmp/u.json

    # online: fetch up to MARKET_SCAN_LIMIT (default 1000) markets from Gamma
    python scripts/scan_polymarket_universe.py --limit 1000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from engine.markets import universe_manager as um  # noqa: E402


def _load_json_catalog(path: str) -> list:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    # support either a JSON array or JSON-lines
    if text[0] == "[":
        data = json.loads(text)
        return data if isinstance(data, list) else []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _default_out() -> str:
    data_dir = os.getenv("HTE_DATA_DIR") or str(Path.home() / ".hermes" / "trading-engine")
    return str(Path(data_dir) / "polymarket_universe.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan + tier the Polymarket universe (selection only).")
    ap.add_argument("--from-json", default=None,
                    help="Read catalog from a local JSON array / JSON-lines file (no network).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Override MARKET_SCAN_LIMIT for this run (clamped to 2000).")
    ap.add_argument("--out", default=None, help="Where to write the status JSON.")
    ap.add_argument("--paper", action="store_true", default=True,
                    help="Paper mode (default; affects max-open-trades clamp).")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="Print the status JSON to stdout.")
    args = ap.parse_args(argv)

    cfg = um.UniverseConfig.from_env()
    if args.limit is not None:
        cfg = um.UniverseConfig(**{**cfg.__dict__, "scan_limit": args.limit})

    if args.from_json:
        raw = _load_json_catalog(args.from_json)
        source = f"file:{args.from_json}"
    else:
        raw = um.fetch_catalog(cfg)
        source = "gamma-api"

    clob_enabled = os.getenv("POLYMARKET_CLOB_ENABLED", "0") not in ("0", "false", "False", "")
    mgr = um.UniverseManager(cfg=cfg, paper=args.paper, live_subscribe_enabled=clob_enabled)
    mgr.ingest(raw)
    targets = mgr.subscription_targets()
    mgr.apply_subscription(targets)
    status = mgr.status(open_polymarket_trades=0)
    status["source"] = source

    out = args.out or _default_out()
    um.save_status(out, status)

    print(f"scanned={status.get('total_markets_scanned')} "
          f"passed={status.get('markets_passing_filters')} "
          f"A={status.get('tier_a_count')} B={status.get('tier_b_count')} "
          f"C={status.get('tier_c_count')} live_subs={status.get('live_websocket_subscriptions')} "
          f"(live_subscribe_enabled={clob_enabled})")
    print(f"status written to {out}")
    if args.do_print:
        print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
