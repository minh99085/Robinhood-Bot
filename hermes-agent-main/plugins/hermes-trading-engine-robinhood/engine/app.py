"""Health, options, and chart vision API for the Robinhood Agentic plugin (port 8810)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.options_readiness import evaluate_readiness
from engine.robinhood.options_state import load_chain_snapshot

logger = logging.getLogger("hermes.robinhood.app")

app = FastAPI(title="Hermes Robinhood Agentic", version="1.1")


async def _live_mcp_client(audit: Any) -> Any:
    """Build a short-lived Robinhood MCP client from the stored OAuth token.

    The API process holds no persistent MCP session (that lives in the agent
    container), but the OAuth tokens sit in the shared /data volume, so any
    process can open a session non-interactively. Returns a connected adapter
    or None — callers MUST disconnect() a returned adapter. Any failure (no
    token yet, transport error) returns None so chart analysis degrades to
    image-only rather than failing the request.
    """
    try:
        from engine.robinhood.robinhood_mcp_adapter import RobinhoodMCPAdapter
        adapter = RobinhoodMCPAdapter(RobinhoodConfig.from_env(), audit=audit)
        if not adapter.storage.has_tokens():
            return None
        await adapter.connect(interactive_oauth=False)
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning("chart analyze: live MCP unavailable (%s) — image-only", exc)
        return None


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


@app.get("/api/robinhood/mcp/catalog")
def mcp_catalog() -> JSONResponse:
    cat = _read_json("mcp_tool_catalog.json")
    if not cat:
        return JSONResponse(
            {"available": False, "reason": "MCP not connected yet — no tool catalog"},
            status_code=503,
        )
    return JSONResponse({"available": True, **cat})


@app.get("/api/robinhood/mc-bridge")
def mc_bridge() -> JSONResponse:
    """MC → bot paper bridge state (ledger tail + aggregate counts).

    Read by the Monte-Carlo-Sim dashboard on port 80 so it can display bridge
    activity without needing filesystem access to this container's volume.
    """
    ledger = _data_dir() / "mc_bridge_ledger.jsonl"
    counts = {"total": 0, "planned": 0, "skipped": 0, "gate_blocked": 0}
    recent: list[dict] = []
    if ledger.exists():
        try:
            for raw in ledger.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(row, dict):
                    continue
                counts["total"] += 1
                outcome = str(row.get("outcome") or "")
                if outcome.startswith("paper_planned"):
                    counts["planned"] += 1
                elif outcome.startswith("gate_blocked"):
                    counts["gate_blocked"] += 1
                else:
                    counts["skipped"] += 1
                recent.append(row)
        except OSError:
            pass
    state = _read_json("mc_bridge_state.json") or {}
    return JSONResponse({
        "available": True,
        "counts": counts,
        "recent": recent[-40:],
        "processed_ids": len(state.get("processed") or {}),
    })


@app.get("/api/robinhood/options/chain")
def options_chain(symbol: str = Query(..., min_length=1, max_length=12)) -> JSONResponse:
    snap = load_chain_snapshot(_data_dir(), symbol.upper())
    if not snap:
        return JSONResponse(
            {"available": False, "symbol": symbol.upper(), "reason": "no cached chain yet"},
            status_code=404,
        )
    return JSONResponse({"available": True, **snap})


@app.get("/api/robinhood/options/readiness")
def options_readiness() -> JSONResponse:
    cached = _read_json("options_readiness.json")
    if cached:
        return JSONResponse({"available": True, **cached})
    cfg = RobinhoodConfig.from_env()
    report = evaluate_readiness(
        cfg,
        status=_read_json("robinhood_status.json"),
        options_status=_read_json("options_status.json"),
        min_paper_scans=cfg.options_min_paper_scans,
    )
    return JSONResponse({"available": True, **report.to_dict()})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    health = health_endpoint()
    opts = _read_json("options_status.json") or {}
    ready = _read_json("options_readiness.json") or {}
    funnel = opts.get("funnel") or {}
    positions = opts.get("positions") or {}
    rows = opts.get("results") or []
    row_html = "".join(
        f"<tr><td>{r.get('symbol','')}</td><td>{r.get('bias','')}</td>"
        f"<td>{r.get('stage','')}</td><td>{r.get('action','')}</td></tr>"
        for r in rows[:25]
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Robinhood Options Bot</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; background: #0f1419; color: #e7ecf3; }}
.card {{ background: #1a2332; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; }}
.ok {{ color: #3dd68c; }} .warn {{ color: #f5c542; }} .bad {{ color: #f87171; }}
table {{ border-collapse: collapse; width: 100%; }} td, th {{ border-bottom: 1px solid #2d3a4d; padding: 0.4rem; text-align: left; }}
</style></head><body>
<h1>Robinhood Options Bot</h1>
<div class="card">
  <div>MCP: <span class="{'ok' if health.get('mcp_connected') else 'bad'}">{'connected' if health.get('mcp_connected') else 'down'}</span></div>
  <div>Live trading: <span class="{'bad' if health.get('live_trading_enabled') else 'ok'}">{'ON' if health.get('live_trading_enabled') else 'OFF (paper)'}</span></div>
  <div>Open positions: {positions.get('open_count', 0)} / {positions.get('max_open_positions', '?')}</div>
  <div>Readiness: <span class="{'ok' if ready.get('ready') else 'warn'}">{'ready' if ready.get('ready') else 'not ready'}</span></div>
</div>
<div class="card"><h3>Last scan funnel</h3><pre>{json.dumps(funnel, indent=2)}</pre></div>
<div class="card"><h3>Symbol results</h3>
<table><tr><th>Symbol</th><th>Bias</th><th>Stage</th><th>Action</th></tr>{row_html or '<tr><td colspan=4>no scan yet</td></tr>'}</table>
</div>
<p><a href="/api/robinhood/options/status" style="color:#7cb8ff">JSON status</a></p>
</body></html>"""


def health_endpoint() -> dict:
    """Shared health payload for /api/health and /dashboard."""
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
        "chart_vision_enabled": os.getenv("CHART_VISION_ENABLED", "1")
        not in ("0", "false", "False"),
    }


@app.get("/api/health")
def health() -> dict:
    return health_endpoint()


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


# ---------------------------------------------------------------------------
# Chart vision endpoints
# ---------------------------------------------------------------------------


class ChartAnalyzeBody(BaseModel):
    image_base64: Optional[str] = None
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    mime_type: Optional[str] = None
    ticker_hint: Optional[str] = None
    run_validation: bool = True
    run_monte_carlo: Optional[bool] = None
    mc_paths: Optional[int] = Field(None, ge=100, le=2_000_000)
    execution_mode: Optional[str] = None


@app.get("/api/chart/config")
def chart_config() -> dict:
    from engine.chart_vision.config import ChartVisionConfig

    cfg = ChartVisionConfig.from_env()
    return {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model,
        "execution_mode": cfg.execution_mode,
        "run_monte_carlo": cfg.run_monte_carlo,
        "mc_paths": cfg.mc_paths,
        "min_overall_confidence": cfg.min_overall_confidence,
        "max_price_rel_error": cfg.max_price_rel_error,
        "require_mcp": cfg.require_mcp,
        "has_api_key": bool(cfg.api_key),
        "monte_carlo_sim_path": cfg.monte_carlo_sim_path,
    }


@app.post("/api/chart/extract")
async def chart_extract(body: ChartAnalyzeBody) -> JSONResponse:
    """Vision extraction only (no MCP, no Monte Carlo)."""
    from engine.chart_vision.config import ChartVisionConfig
    from engine.chart_vision.extractor import analyze_tradingview_chart
    from engine.robinhood.audit_log import AuditLog

    if not any([body.image_base64, body.image_url, body.image_path]):
        return JSONResponse(
            {"ok": False, "error": "Provide image_base64, image_url, or image_path"},
            status_code=400,
        )
    cfg = ChartVisionConfig.from_env()
    audit = AuditLog(_data_dir())
    try:
        result = analyze_tradingview_chart(
            image_base64=body.image_base64,
            image_url=body.image_url,
            image_path=body.image_path,
            mime_type=body.mime_type,
            ticker_hint=body.ticker_hint,
            config=cfg,
        )
        audit.record(
            "chart_vision_extract_api",
            tool="analyze_tradingview_chart",
            details={"ticker": result.ticker, "bias": result.bias.value},
        )
        return JSONResponse({"ok": True, "extraction": result.model_dump(mode="json")})
    except Exception as exc:  # noqa: BLE001
        audit.record(
            "chart_vision_extract_api_error",
            tool="analyze_tradingview_chart",
            reason=str(exc),
        )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/chart/analyze")
async def chart_analyze(body: ChartAnalyzeBody) -> JSONResponse:
    """
    Full pipeline: extract → optional MCP validate → optional Monte Carlo decision.

    Does **not** place orders. Recommendations only unless execution_mode is
    gated_execution (still requires SafeRobinhoodClient for place_*).
    """
    from engine.chart_vision.config import ChartVisionConfig
    from engine.chart_vision.pipeline import run_full_pipeline
    from engine.robinhood.audit_log import AuditLog

    if not any([body.image_base64, body.image_url, body.image_path]):
        return JSONResponse(
            {"ok": False, "error": "Provide image_base64, image_url, or image_path"},
            status_code=400,
        )

    cfg = ChartVisionConfig.from_env()
    audit = AuditLog(_data_dir())

    # Fact-check against live Robinhood quotes when validation is on and a
    # token exists. Built per request and torn down after; falls back to
    # image-only (mcp_client=None) whenever the live session is unavailable.
    mcp_client: Any = None
    if body.run_validation is not False:
        mcp_client = await _live_mcp_client(audit)

    try:
        resp = await run_full_pipeline(
            image_base64=body.image_base64,
            image_url=body.image_url,
            image_path=body.image_path,
            mime_type=body.mime_type,
            ticker_hint=body.ticker_hint,
            run_validation=body.run_validation,
            run_monte_carlo=body.run_monte_carlo,
            mc_paths=body.mc_paths,
            execution_mode=body.execution_mode,
            config=cfg,
            mcp_client=mcp_client,
            audit=audit,
        )
    finally:
        if mcp_client is not None:
            try:
                await mcp_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    status = 200 if resp.ok else 500
    return JSONResponse(resp.model_dump(mode="json"), status_code=status)
