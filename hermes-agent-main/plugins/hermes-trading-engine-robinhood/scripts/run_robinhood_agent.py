#!/usr/bin/env python3
"""Robinhood Agentic plugin main loop — MCP connection manager + status persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

# Allow running from plugin root inside Docker (/app).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.client import SafeRobinhoodClient
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.options_loop import run_options_tick
from engine.robinhood.robinhood_mcp_adapter import RobinhoodMCPAdapter
from engine.robinhood.safety_gates import RobinhoodSafetyGates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hermes.robinhood.agent")


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


async def _main() -> None:
    cfg = RobinhoodConfig.from_env()
    audit = AuditLog(cfg.data_dir)
    adapter = RobinhoodMCPAdapter(cfg, audit)
    gates = RobinhoodSafetyGates(cfg, audit)
    client = SafeRobinhoodClient(adapter, gates, cfg)
    status_path = Path(cfg.data_dir) / "robinhood_status.json"

    stop = asyncio.Event()

    def _handle_sig(*_args: object) -> None:
        stop.set()
        adapter.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_sig)
        except ValueError:
            pass

    reconnect_task = asyncio.create_task(adapter.run_reconnect_loop())
    last_options_tick = 0.0

    try:
        while not stop.is_set():
            payload = client.status()
            payload["ts"] = time.time()
            payload["options_loop_enabled"] = cfg.options_loop_enabled

            if (
                cfg.options_loop_enabled
                and adapter.health.connected
                and (time.time() - last_options_tick) >= cfg.options_tick_seconds
            ):
                try:
                    payload["options"] = await run_options_tick(
                        client, cfg, agent_status=payload
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("options tick failed: %s", exc)
                    payload["options"] = {
                        "available": False,
                        "reason": "tick_exception",
                        "error": str(exc),
                    }
                last_options_tick = time.time()

            _write_status(status_path, payload)
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.health_interval_s)
            except asyncio.TimeoutError:
                pass
    finally:
        adapter.stop()
        reconnect_task.cancel()
        try:
            await reconnect_task
        except asyncio.CancelledError:
            pass
        await adapter.disconnect()
        logger.info("Robinhood agent stopped")


if __name__ == "__main__":
    asyncio.run(_main())