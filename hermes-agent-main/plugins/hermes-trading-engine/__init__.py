"""Hermes plugin: agent tools for the paper-trading engine.

These tools talk to a running Hermes Trading Engine over HTTP (default
http://localhost:8800, set HTE_URL to change). The engine itself is the
separate FastAPI/Docker service in this folder. PAPER-TRADING ONLY — the
engine never submits real orders, and these tools cannot make it do so.

Per the Hermes plugin policy this file does NOT modify any core file; it only
registers tools via the plugin context.
"""

from __future__ import annotations

import json
import os

_DEFAULT_URL = os.getenv("HTE_URL", "http://localhost:8800")
_TIMEOUT = 6.0


def _base_url() -> str:
    return os.getenv("HTE_URL", _DEFAULT_URL).rstrip("/")


def _http_get(path: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{_base_url()}{path}", timeout=_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def _http_post(path: str) -> dict:
    import urllib.request

    req = urllib.request.Request(f"{_base_url()}{path}", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# tool handlers (all return JSON strings, per the registry contract)
# --------------------------------------------------------------------------
def _trading_status(args: dict) -> str:
    try:
        snap = _http_get("/api/state")
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "success": False,
            "error": f"Could not reach the trading engine at {_base_url()}: {exc}. "
                     "Is the Docker container running? (docker compose up)",
        })
    p = snap.get("portfolio", {})
    pulse = snap.get("pulse", {})
    out = {
        "success": True,
        "mode": snap.get("mode", "PAPER"),
        "autotrade": snap.get("autotrade"),
        "round": snap.get("round"),
        "equity": p.get("equity"),
        "total_pnl": p.get("total_pnl"),
        "realized": p.get("realized"),
        "unrealized": p.get("unrealized"),
        "win_rate": p.get("win_rate"),
        "trades": p.get("trades"),
        "sharpe": p.get("sharpe"),
        "pulse": {
            "symbol": pulse.get("symbol"),
            "price_to_beat": pulse.get("start_price"),
            "current_price": pulse.get("current_price"),
            "seconds_left": pulse.get("seconds_left"),
            "up_price": pulse.get("up_price"),
            "down_price": pulse.get("down_price"),
            "bet": pulse.get("bet"),
        },
        "regime": snap.get("regime", {}).get("current_state"),
        "p_up": snap.get("regime", {}).get("p_up"),
        "brain": snap.get("brain", {}),
        "open_trades": snap.get("open_trades", []),
    }
    return json.dumps(out)


def _trading_set_autotrade(args: dict) -> str:
    flag = str(args.get("enabled", "")).lower() in ("1", "true", "on", "yes", "enable")
    try:
        res = _http_post(f"/api/autotrade/{'on' if flag else 'off'}")
        return json.dumps({"success": True, **res})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(exc)})


def _trading_reset(args: dict) -> str:
    try:
        _http_post("/api/reset")
        return json.dumps({"success": True, "message": "Paper portfolio reset."})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(exc)})


def register(ctx) -> None:
    ctx.register_tool(
        name="trading_status",
        toolset="trading",
        description="Read paper-trading P&L, positions, Grok brain, and the BTC pulse market.",
        schema={
            "name": "trading_status",
            "description": (
                "Get the current state of the Hermes Trading Engine (PAPER trading): "
                "equity, P&L, win rate, open positions across crypto/stocks/Polymarket, "
                "the Grok brain's latest call, and the live BTC 5-min pulse round. Read-only."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kw: _trading_status(args or {}),
    )

    ctx.register_tool(
        name="trading_set_autotrade",
        toolset="trading",
        description="Turn the autonomous paper-trading bot on or off.",
        schema={
            "name": "trading_set_autotrade",
            "description": (
                "Enable or disable the autonomous PAPER-trading bot. When off, no new "
                "simulated bets/positions are opened. Does not affect real funds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "true to enable the paper bot, false to pause it.",
                    }
                },
                "required": ["enabled"],
            },
        },
        handler=lambda args, **kw: _trading_set_autotrade(args or {}),
    )

    ctx.register_tool(
        name="trading_reset",
        toolset="trading",
        description="Reset the paper portfolio and clear simulated trade history.",
        schema={
            "name": "trading_reset",
            "description": "Wipe all simulated trades and reset paper equity to the starting balance.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kw: _trading_reset(args or {}),
    )
