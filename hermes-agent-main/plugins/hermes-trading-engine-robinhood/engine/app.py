"""Read-only health API for the Robinhood Agentic plugin (port 8810 by default)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Hermes Robinhood Agentic", version="1.0")


def _data_dir() -> Path:
    return Path(os.environ.get("RH_DATA_DIR", "/data"))


def _read_json(name: str) -> dict | None:
    path = _data_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/health")
def health() -> dict:
    st = _read_json("robinhood_status.json") or {}
    p = _data_dir() / "robinhood_status.json"
    age = round(time.time() - p.stat().st_mtime, 1) if p.exists() else None
    fresh = age is not None and age < 180
    return {
        "status": "ok",
        "plugin": "hermes-trading-engine-robinhood",
        "live_trading_enabled": st.get("live_trading_enabled", False),
        "mcp_connected": st.get("connected", False),
        "status_fresh": fresh,
        "status_age_s": age,
        "tool_count": st.get("tool_count", 0),
    }


@app.get("/api/robinhood/status")
def robinhood_status() -> JSONResponse:
    st = _read_json("robinhood_status.json")
    if not st:
        return JSONResponse(
            {"available": False, "reason": "agent loop has not written status yet"},
            status_code=503,
        )
    return JSONResponse({"available": True, **st})


@app.get("/api/robinhood/tools")
def robinhood_tools() -> JSONResponse:
    st = _read_json("robinhood_status.json") or {}
    tools = st.get("tools") or []
    return JSONResponse({"tools": tools, "count": len(tools)})


@app.get("/api/robinhood/options/status")
def options_status() -> JSONResponse:
    st = _read_json("options_status.json")
    if not st:
        return JSONResponse(
            {"available": False, "reason": "options loop has not run yet"},
            status_code=503,
        )
    return JSONResponse({"available": True, **st})


@app.get("/api/robinhood/options/ledger")
def options_ledger() -> JSONResponse:
    ledger = _read_json("options_ledger.json") or {"events": []}
    events = ledger.get("events") or []
    return JSONResponse({"count": len(events), "events": events[-100:]})


@app.get("/api/robinhood/mcp/catalog")
def mcp_catalog() -> JSONResponse:
    cat = _read_json("mcp_tool_catalog.json")
    if not cat:
        return JSONResponse(
            {"available": False, "reason": "MCP not connected yet — no tool catalog"},
            status_code=503,
        )
    return JSONResponse({"available": True, **cat})