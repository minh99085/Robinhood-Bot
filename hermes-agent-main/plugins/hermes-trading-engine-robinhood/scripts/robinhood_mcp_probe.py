#!/usr/bin/env python3
"""Probe Robinhood MCP: list tools and optionally sample read-only option chain data."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.client import SafeRobinhoodClient
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.options_market import fetch_equity_quotes, load_symbol_snapshot
from engine.robinhood.robinhood_mcp_adapter import RobinhoodMCPAdapter
from engine.robinhood.safety_gates import RobinhoodSafetyGates


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Robinhood MCP probe")
    parser.add_argument("--symbol", default="SPY", help="Symbol for optional chain sample")
    parser.add_argument("--bias", choices=("call", "put"), default="call")
    parser.add_argument("--oauth", action="store_true", help="Run interactive OAuth if needed")
    args = parser.parse_args()

    cfg = RobinhoodConfig.from_env()
    audit = AuditLog(cfg.data_dir)
    adapter = RobinhoodMCPAdapter(cfg, audit)
    gates = RobinhoodSafetyGates(cfg, audit)
    client = SafeRobinhoodClient(adapter, gates, cfg)

    await adapter.connect(interactive_oauth=args.oauth)
    tools = await client.list_tools()
    print(f"MCP connected — {len(tools)} tools")
    print(json.dumps(tools, indent=2))

    quotes = await fetch_equity_quotes(client, [args.symbol.upper()])
    print(f"\nEquity quote {args.symbol.upper()}: {quotes}")

    snap = await load_symbol_snapshot(client, args.symbol.upper(), cfg, args.bias)
    if snap:
        print(
            f"\nSnapshot: spot={snap.spot.last_price} contracts={len(snap.contracts)} "
            f"quotes={len(snap.quotes)}"
        )
    else:
        print("\nNo option snapshot (tools missing or empty chain)")

    await adapter.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
