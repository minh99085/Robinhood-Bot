"""FastAPI app: dashboard, REST snapshot, live WebSocket, mode/safety + arb endpoints.

Run: uvicorn engine.app:app --host 0.0.0.0 --port 8800
PAPER only.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .arb.detector import ArbitrageDetector
from .arb.execution import ArbExecutionEngine
from .arb.feeds import FeedAggregator
from .arb.gateway import ExchangeGateway
from .arb.ledger import ArbLedger
from .arb.symbol_map import SymbolMapper
from .arb.universe import UniverseManager
from .config import settings
from .engine import TradingEngine
from .research import GrokResearchClient
from .research.schemas import ONLINE_MODES
from .storage import Store
from .venues import MarketFilter, MarketRef, build_default_registry, enabled_venues
from .shadow import (
    LiveReadinessGate,
    ShadowConfig,
    ShadowOrchestrator,
    compute_session_metrics,
    write_report,
)
from .guarded_live import (
    ApprovalWorkflow,
    ArmingTokenManager,
    ConformanceHarness,
    DryRunLiveBroker,
    GuardedLiveConfig,
    GuardedLiveStateMachine,
    SafetyEnvelope,
    run_precheck,
)
from .guarded_live import write_report as gl_write_report
from .guarded_live.schemas import ApprovalBatch

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Hermes Trading Engine (paper)")

_store = Store(settings.db_path)
_engine = TradingEngine(settings, _store)
_research_mode = (os.getenv("RESEARCH_MODE") or "offline_cache").strip().lower()
_research = GrokResearchClient.from_env(store=_store)
_venues = build_default_registry(store=_store, market_data=globals().get("_market_data"))

# Phase 2: read-only Polymarket CLOB market-data feed (default OFF). When
# enabled, it subscribes to trending-market token ids and feeds the RiskEngine
# freshness state. It can never place an order — it is strictly read-only.
_clob_enabled = os.getenv("POLYMARKET_CLOB_ENABLED", "0") not in ("0", "false", "False", "")
_market_data = None
if _clob_enabled:
    try:
        from .market_data.event_store import RawEventStore
        from .market_data.polymarket_ws import MarketDataManager

        _market_data = MarketDataManager(
            event_store=RawEventStore(_store),
            url=os.getenv("POLYMARKET_WS_URL") or None,
            stale_ms=int(os.getenv("POLYMARKET_CLOB_STALE_MS", "3000") or 3000),
            persist_raw=os.getenv("POLYMARKET_CLOB_PERSIST_RAW", "1") not in ("0", "false", "False"),
            max_assets=int(os.getenv("POLYMARKET_CLOB_MAX_ASSETS", "20") or 20),
        )
        _engine.market_data = _market_data
    except Exception as exc:  # noqa: BLE001 — feed init must never block the dashboard
        _engine.record_error(f"clob init: {exc}")
        _market_data = None

_arb_mapper = SymbolMapper(settings.data_dir)
_arb_feeds = FeedAggregator(_arb_mapper)
_arb_universe = UniverseManager(_arb_feeds, _arb_mapper)
_arb_detector = ArbitrageDetector(_arb_feeds, _arb_mapper, _arb_universe)
_arb_gateway = ExchangeGateway(_arb_feeds, _arb_mapper, paper=True)
_arb_ledger = ArbLedger(settings.data_dir)


def _arb_market_context() -> dict:
    reg = _engine.regime or {}
    closes = [c["c"] for c in _engine.klines_cache[-60:]] if _engine.klines_cache else []
    vol = 0.0
    if len(closes) > 2:
        arr = np.array(closes, dtype=float)
        vol = float((np.diff(arr) / arr[:-1]).std())
    return {"currentRegime": reg.get("current_state"), "markovState": reg.get("current_state"),
            "recentVolatility_1m": round(vol, 5), "arbMemory": _engine.brain.memory.recent(5)}


_arb = ArbExecutionEngine(
    detector=_arb_detector, gateway=_arb_gateway, ledger=_arb_ledger,
    feeds=_arb_feeds, universe=_arb_universe, brain=_engine.brain,
    get_mode=lambda: _engine.mode, circuit=_engine.circuit,
    get_market_context=_arb_market_context,
    # Route every arb (paper) execution through the deterministic RiskEngine.
    risk_gate=_engine.assess_arb_proposal)

_latest_snapshot: dict = {}
_snapshot_lock = threading.Lock()
_stop = threading.Event()


def _engine_loop() -> None:
    try:
        _engine.tick()
    except Exception as exc:  # noqa: BLE001
        _engine.record_error(str(exc))
    while not _stop.is_set():
        t0 = time.time()
        try:
            _engine.tick()
            snap = _engine.snapshot()
            with _snapshot_lock:
                global _latest_snapshot
                _latest_snapshot = snap
        except Exception as exc:  # noqa: BLE001
            _engine.record_error(str(exc))
            with _snapshot_lock:
                _latest_snapshot = {"error": str(exc)}
        _stop.wait(max(0.5, settings.tick_seconds - (time.time() - t0)))


_thread: threading.Thread | None = None


@app.on_event("startup")
def _startup() -> None:
    global _thread
    _thread = threading.Thread(target=_engine_loop, name="hte-engine", daemon=True)
    _thread.start()
    # Arbitrage is permanently disabled (Polymarket-only PAPER training). start()
    # is a no-op kept for backwards-compatible shutdown wiring.
    _arb.start()
    if _market_data is not None:
        _market_data.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop.set()
    fns = [_engine.brain.stop, _arb.stop]
    if _market_data is not None:
        fns.append(_market_data.stop)
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def _snapshot() -> dict:
    with _snapshot_lock:
        snap = dict(_latest_snapshot) if _latest_snapshot else None
    if snap is None:
        snap = _engine.snapshot()
    if "error" not in snap:
        snap["aggressiveness"] = settings.aggressiveness
        try:
            snap["arb"] = _arb.snapshot()
        except Exception:  # noqa: BLE001
            pass
        # Replay / backtest runs (offline; read-only) so the dashboard
        # "Replay / backtest" panel populates from the persisted replay_*
        # tables. Never triggers a run — that's POST /api/replay/run or
        # scripts/run_replay.py. Enrich each shown run with the headline
        # metrics the panel renders (metrics live in a separate table).
        try:
            runs = _store.get_replay_runs(20)
            for r in runs[:6]:
                try:
                    m = _store.get_replay_metrics(r.get("replay_run_id", "")) or {}
                    cal = m.get("calibration") if isinstance(m.get("calibration"), dict) else {}
                    r.update({
                        "ending_equity": m.get("ending_equity"),
                        "total_pnl": m.get("total_pnl"),
                        "max_drawdown": m.get("max_drawdown"),
                        "fill_ratio": m.get("fill_ratio"),
                        "brier": cal.get("brier_score"),
                    })
                except Exception:  # noqa: BLE001
                    pass
            snap["replay"] = {"recent_runs": runs}
        except Exception:  # noqa: BLE001
            pass
    return snap


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.get("/api/health")
def api_health() -> dict:
    return {"ok": True, "mode": _engine.mode, "engine": settings.engine_name,
            "brain_enabled": _engine.brain.enabled, "aggressiveness": settings.aggressiveness,
            "arb_enabled": False, "oms_degraded": _engine.oms.degraded}


@app.post("/api/autotrade/{flag}")
def api_autotrade(flag: str) -> dict:
    enabled = flag.lower() in ("1", "on", "true", "enable", "enabled")
    _engine.set_autotrade(enabled)
    return {"autotrade": enabled}


@app.post("/api/reset")
def api_reset() -> dict:
    _engine.reset()
    return {"ok": True}


@app.get("/api/readiness")
def api_readiness() -> dict:
    return _engine.readiness()


@app.get("/api/mode")
def api_mode() -> dict:
    return {"mode": _engine.mode, "readiness": _engine.readiness(), "circuit": _engine.circuit.status()}


@app.post("/api/mode/paper")
def api_mode_paper() -> dict:
    return _engine.set_mode("paper", reason="manual downgrade")


@app.post("/api/mode/live")
async def api_mode_live(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    confirmed = (str(body.get("confirm", "")) == "CONFIRM") and bool(body.get("ack", False))
    result = _engine.set_mode("live", confirmed=confirmed)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.get("/api/circuit")
def api_circuit() -> dict:
    return _engine.circuit.status()


@app.get("/api/risk")
def api_risk() -> dict:
    return _engine.risk_status()


@app.get("/api/risk/decisions")
def api_risk_decisions() -> dict:
    return _engine.risk_decisions()


@app.get("/api/accounting")
def api_accounting() -> dict:
    return _engine.accounting_summary()


@app.get("/api/markets/universe")
def api_markets_universe() -> dict:
    """Adaptive Polymarket universe status (read-only; never triggers a network
    scan). Reads the cached snapshot written by
    ``scripts/scan_polymarket_universe.py`` (or the opt-in engine loop). Returns a
    config-only stub if no scan has run yet."""
    from engine.markets import universe_manager as _um
    path = _engine.s.data_dir / "polymarket_universe.json"
    status = _um.load_status(path)
    if status is None:
        cfg = _um.UniverseConfig.from_env()
        return {
            "available": False,
            "reason": "no scan yet — run scripts/scan_polymarket_universe.py",
            "config": cfg.as_dict(),
            "max_open_trades": cfg.effective_max_open_trades(paper=True),
            "live_subscribe_enabled": os.getenv("POLYMARKET_CLOB_ENABLED", "0")
                                      not in ("0", "false", "False", ""),
        }
    try:
        status["open_polymarket_trades"] = len(_engine.store.open_trades("polymarket"))
    except Exception:  # noqa: BLE001
        pass
    return status


def _training_status() -> dict | None:
    """Read the persisted Polymarket PAPER training status (read-only)."""
    path = _engine.s.data_dir / "polymarket_training.json"
    if not path.exists():
        return None
    try:
        import json as _json
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/polymarket/training/status")
def api_training_status() -> dict:
    st = _training_status()
    if st is None:
        return {"available": False,
                "reason": "no training run yet — run scripts/start_polymarket_paper_training.py",
                "mode": "paper", "polymarket_only": True}
    return st


@app.get("/api/polymarket/training/btc_pulse")
def api_training_btc_pulse() -> dict:
    """BTC 5-min Pulse PAPER-ONLY isolated experiment status (read-only).

    Surfaces the ``btc_pulse`` block written by the training engine so the
    dashboard can show whether the pulse experiment is enabled / frozen and its
    paper metrics. PAPER ONLY — never reflects any live order."""
    st = _training_status() or {}
    bp = st.get("btc_pulse") or {}
    if not bp:
        return {"available": False, "btc_pulse_enabled": False, "btc_pulse_frozen": True,
                "reason": "BTC Pulse disabled — set BTC_PULSE_ENABLED=1 and restart "
                          "hermes-training to unfreeze the paper experiment."}
    return {"available": True, **bp}


@app.get("/api/polymarket/training/scan")
def api_training_scan() -> dict:
    st = _training_status() or {}
    return {"available": bool(st), "scan_metrics": st.get("scan_metrics", {})}


@app.get("/api/polymarket/training/candidates")
def api_training_candidates() -> dict:
    st = _training_status() or {}
    learning = st.get("learning", {})
    return {"available": bool(st),
            "trade_candidate_limit": st.get("config", {}).get("trade_candidate_limit"),
            "subscribed_assets": st.get("scan_metrics", {}).get("subscribed_assets"),
            "category_reliability": learning.get("category_reliability", {})}


@app.get("/api/polymarket/training/edge")
def api_training_edge() -> dict:
    st = _training_status() or {}
    learning = st.get("learning", {})
    return {"available": bool(st), "edge_buckets": learning.get("edge_buckets", {}),
            "no_trade_reasons": learning.get("no_trade_reasons", {}),
            "min_net_edge": st.get("config", {}).get("min_net_edge")}


@app.get("/api/polymarket/training/learning")
def api_training_learning() -> dict:
    st = _training_status() or {}
    return {"available": bool(st), "learning": st.get("learning", {}),
            "feedback": st.get("feedback", {})}


@app.get("/api/polymarket/training/report")
def api_training_report() -> dict:
    """Latest report bundle summary (read-only). Returns the most recent
    summary.json under polymarket_training_reports/ if present."""
    import glob as _glob
    import json as _json
    import os as _os
    roots = [str(_engine.s.data_dir / "polymarket_training_reports"),
             "polymarket_training_reports"]
    summaries = []
    for root in roots:
        summaries += _glob.glob(_os.path.join(root, "*", "summary.json"))
    if not summaries:
        return {"available": False, "reason": "no report yet — run scripts/polymarket_training_report.py"}
    latest = max(summaries, key=_os.path.getmtime)
    try:
        data = _json.loads(open(latest, encoding="utf-8").read())
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "report unreadable"}
    return {"available": True, "report_path": latest,
            "recommendation": data.get("recommendation"),
            "pnl": data.get("pnl", {}), "safety": data.get("safety", {})}


@app.get("/api/polymarket/training/baselines")
def api_training_baselines() -> dict:
    st = _training_status() or {}
    return {"available": bool(st), "baselines": st.get("baselines", [])}


def _training_start_refusals() -> list:
    """Safety preflight for starting paper training. Returns a list of refusal
    reasons (empty = safe). PAPER ONLY — refuses on ANY live-execution flag."""
    from engine.training.config import _envb as _tb
    refused = []
    for f in ("MICRO_LIVE_ENABLED", "KALSHI_MICRO_LIVE_ENABLED",
              "POLYMARKET_MICRO_LIVE_ENABLED", "MICRO_LIVE_ALLOW_PRODUCTION",
              "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION", "GUARDED_LIVE_ENABLED"):
        if _tb(f, False):
            refused.append(f)
    if _tb("ARB_EXECUTION_ENABLED", False):
        refused.append("ARB_EXECUTION_ENABLED")
    try:
        from engine.arb.execution import ARBITRAGE_PERMANENTLY_DISABLED
        if not ARBITRAGE_PERMANENTLY_DISABLED:
            refused.append("arbitrage_not_permanently_disabled")
    except Exception:  # noqa: BLE001
        pass
    if not _tb("POLYMARKET_CLOB_ENABLED", True):
        refused.append("polymarket_clob_disabled")
    return refused


@app.post("/api/polymarket/training/start-paper")
def api_training_start_paper() -> JSONResponse:
    """Start PAPER training. Refuses (409) on any live/production/arbitrage flag
    or if CLOB is disabled. This NEVER submits a real order; it only signals the
    paper-training loop (run via the training script / service) to start."""
    refused = _training_start_refusals()
    if refused:
        return JSONResponse(
            {"started": False, "reason": "unsafe_config", "refused": refused,
             "execution": "paper", "note": "PAPER ONLY — refused due to live config"},
            status_code=409)
    try:
        (_engine.s.data_dir / "polymarket_training.start").write_text(
            "paper_train", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(
        {"started": True, "mode": "paper_train", "execution": "paper",
         "note": "PAPER ONLY — no real orders, no live submit"})


@app.post("/api/polymarket/training/stop-paper")
def api_training_stop_paper() -> dict:
    """Stop the PAPER training loop safely (data preserved)."""
    try:
        (_engine.s.data_dir / "polymarket_training.stop").write_text("stop", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return {"stopped": True, "execution": "paper"}


@app.post("/api/grok/{flag}")
def api_grok_toggle(flag: str) -> dict:
    """Dashboard on/off switch for the Grok RESEARCH layer. Research-only:
    toggling this only controls whether Grok runs/makes estimates — Grok can
    never place, cancel, size, or approve an order."""
    on = str(flag).strip().lower() in ("on", "1", "true", "enable", "enabled", "yes")
    try:
        return _engine.brain.set_active(on)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/favicon.ico")
def favicon():
    # Browsers auto-request a favicon; we have none. Return "no content" instead
    # of a scary 404 in the console. Purely cosmetic.
    from fastapi import Response
    return Response(status_code=204)


@app.get("/api/market-data/status")
def api_market_data_status() -> dict:
    return _engine.market_data_status()


@app.get("/api/market-data/recent-events")
def api_market_data_recent_events() -> dict:
    return {"events": _store.get_recent_raw_market_events(100)}


@app.get("/api/market-data/bbo")
def api_market_data_bbo(asset_id: str | None = None) -> JSONResponse:
    if _market_data is None:
        return JSONResponse({"enabled": False, "bbo": None})
    if asset_id:
        b = _market_data.get_bbo(asset_id)
        return JSONResponse({"asset_id": asset_id, "bbo": b.model_dump() if b else None})
    return JSONResponse({"assets": _market_data.health().get("assets", [])})


@app.get("/api/market-data/orderbook/{asset_id}")
def api_market_data_orderbook(asset_id: str) -> JSONResponse:
    if _market_data is None:
        return JSONResponse({"error": "clob disabled"}, status_code=404)
    ob = _market_data.get_orderbook(asset_id)
    if ob is None:
        return JSONResponse({"error": "unknown asset"}, status_code=404)
    return JSONResponse(ob.to_snapshot().model_dump())


# --- Phase 3: OMS / PaperBroker (paper only; no real execution) -------- #
@app.get("/api/orders")
def api_orders() -> dict:
    return {"orders": _engine.oms.get_orders(200)}


@app.get("/api/orders/open")
def api_orders_open() -> dict:
    return {"orders": _engine.oms.get_open_orders()}


@app.get("/api/orders/recent")
def api_orders_recent() -> dict:
    return {"orders": _engine.oms.get_recent_orders(50)}


@app.post("/api/orders/cancel-all")
def api_orders_cancel_all() -> dict:
    return _engine.oms.cancel_all()


@app.post("/api/orders/{client_order_id}/cancel")
def api_orders_cancel(client_order_id: str) -> dict:
    return _engine.oms.cancel_order(client_order_id)


@app.get("/api/orders/{client_order_id}")
def api_order(client_order_id: str) -> JSONResponse:
    o = _engine.oms.get_order(client_order_id)
    if o is None:
        return JSONResponse({"error": "unknown order"}, status_code=404)
    o["events"] = _store.get_order_events(client_order_id, 100)
    o["fills"] = _store.get_fills_for_order(client_order_id)
    return JSONResponse(o)


@app.get("/api/fills")
def api_fills() -> dict:
    return {"fills": _engine.oms.get_fills(200)}


@app.get("/api/positions")
def api_positions() -> dict:
    return {"positions": _engine.oms.get_positions()}


@app.get("/api/reconciliation/status")
def api_reconciliation_status() -> dict:
    st = _engine.oms.status()
    return {"degraded": st.get("degraded"), "degraded_reason": st.get("degraded_reason"),
            "last_reconciliation": st.get("last_reconciliation")}


@app.get("/api/reconciliation/events")
def api_reconciliation_events() -> dict:
    return {"events": _store.get_reconciliation_events(100)}


# --- Phase 4: replay / backtest (offline; no network; no live orders) -- #
@app.get("/api/replay/runs")
def api_replay_runs() -> dict:
    return {"runs": _store.get_replay_runs(50)}


@app.get("/api/replay/runs/{replay_run_id}/metrics")
def api_replay_metrics(replay_run_id: str) -> dict:
    return {"metrics": _store.get_replay_metrics(replay_run_id)}


@app.get("/api/replay/runs/{replay_run_id}/equity")
def api_replay_equity(replay_run_id: str) -> dict:
    return {"equity": _store.get_replay_equity(replay_run_id)}


@app.get("/api/replay/runs/{replay_run_id}/orders")
def api_replay_orders(replay_run_id: str) -> dict:
    return {"orders": _store.get_replay_orders(replay_run_id)}


@app.get("/api/replay/runs/{replay_run_id}/fills")
def api_replay_fills(replay_run_id: str) -> dict:
    return {"fills": _store.get_replay_fills(replay_run_id)}


@app.get("/api/replay/runs/{replay_run_id}/calibration")
def api_replay_calibration(replay_run_id: str) -> dict:
    return {"calibration": _store.get_replay_calibration(replay_run_id)}


@app.get("/api/replay/runs/{replay_run_id}/report")
def api_replay_report(replay_run_id: str) -> JSONResponse:
    run = _store.get_replay_run(replay_run_id)
    if run is None:
        return JSONResponse({"error": "unknown replay run"}, status_code=404)
    return JSONResponse({"run": run, "metrics": _store.get_replay_metrics(replay_run_id),
                         "calibration": _store.get_replay_calibration(replay_run_id)})


@app.get("/api/replay/runs/{replay_run_id}")
def api_replay_run(replay_run_id: str) -> JSONResponse:
    run = _store.get_replay_run(replay_run_id)
    if run is None:
        return JSONResponse({"error": "unknown replay run"}, status_code=404)
    return JSONResponse(run)


@app.post("/api/replay/run")
async def api_replay_run_start(request: Request) -> JSONResponse:
    """Run a SMALL replay synchronously from explicit config (no network)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        from .replay import ReplayConfig, ReplayEventLoader, ReplayRunner, write_report
        cfg = ReplayConfig(**body)
        loader = ReplayEventLoader(store=_store)
        if cfg.from_jsonl:
            events = loader.from_jsonl(cfg.from_jsonl, venue=cfg.venue, market_ids=cfg.market_ids,
                                       asset_ids=cfg.asset_ids, start_ts_ms=cfg.start_ts_ms,
                                       end_ts_ms=cfg.end_ts_ms, max_events=cfg.max_events,
                                       dedup=cfg.dedup_raw_events)
        else:
            events = loader.from_sqlite(venue=cfg.venue, market_ids=cfg.market_ids,
                                        asset_ids=cfg.asset_ids, start_ts_ms=cfg.start_ts_ms,
                                        end_ts_ms=cfg.end_ts_ms, max_events=cfg.max_events,
                                        dedup=cfg.dedup_raw_events)
        if not events:
            return JSONResponse({"status": "failed", "error": "no_events"}, status_code=400)
        if len(events) > 200000:
            return JSONResponse({"status": "failed", "error": "too_many_events_for_sync_run"},
                                status_code=400)
        runner = ReplayRunner(cfg, _store, events)
        report = runner.run()
        try:
            write_report(runner, cfg.output_dir)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"replay_run_id": report["replay_run_id"], "status": report["status"]})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "failed", "error": str(exc)[:200]}, status_code=400)


# --- Phase 5: research / probability engine (Grok research-only) --------- #
@app.get("/api/research/status")
def api_research_status() -> dict:
    return {"mode": _research_mode, "model": _research.model,
            "online": _research_mode in ONLINE_MODES,
            "budget": _research.budget.status(),
            "web_search": _research.enable_web_search,
            "x_search": _research.enable_x_search,
            "recent_runs": _store.get_research_runs(5)}


@app.get("/api/research/runs")
def api_research_runs() -> dict:
    return {"runs": _store.get_research_runs(50)}


@app.get("/api/research/runs/{research_run_id}")
def api_research_run(research_run_id: str) -> JSONResponse:
    run = _store.get_research_run(research_run_id)
    if run is None:
        return JSONResponse({"error": "unknown research run"}, status_code=404)
    return JSONResponse(run)


@app.get("/api/research/estimates")
def api_research_estimates(venue: str | None = None, market_id: str | None = None) -> dict:
    return {"estimates": _store.get_probability_estimates(venue=venue, market_id=market_id, limit=100)}


@app.get("/api/research/estimates/{estimate_id}")
def api_research_estimate(estimate_id: str) -> JSONResponse:
    est = _store.get_probability_estimate(estimate_id)
    if est is None:
        return JSONResponse({"error": "unknown estimate"}, status_code=404)
    return JSONResponse(est)


@app.get("/api/research/evidence")
def api_research_evidence(research_run_id: str | None = None,
                          estimate_id: str | None = None) -> dict:
    return {"evidence": _store.get_research_evidence(
        research_run_id=research_run_id, estimate_id=estimate_id, limit=200)}


@app.get("/api/research/market-rules/{market_id}")
def api_research_market_rules(market_id: str, venue: str = "polymarket") -> dict:
    return {"rules": _store.get_market_rule_summary(venue, market_id)}


@app.get("/api/research/budget")
def api_research_budget() -> dict:
    return _research.budget.status()


@app.post("/api/research/estimate")
async def api_research_estimate_create(request: Request) -> JSONResponse:
    """Research-ONLY. Never places, sizes, or cancels orders. Disabled unless
    RESEARCH_MODE is an online mode."""
    if _research_mode not in ONLINE_MODES:
        return JSONResponse(
            {"status": "disabled",
             "error": f"research endpoint requires online mode (current: {_research_mode})"},
            status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not body.get("market_id"):
        return JSONResponse({"status": "failed", "error": "market_id required"}, status_code=400)
    ctx = {
        "venue": body.get("venue") or "polymarket", "market_id": str(body["market_id"]),
        "asset_id": body.get("asset_id"), "outcome": body.get("outcome") or "YES",
        "question": body.get("question"), "resolution_source": body.get("resolution_source"),
        "close_ts_ms": body.get("close_ts_ms"), "p_market_mid": body.get("p_market_mid"),
        "best_bid": body.get("best_bid"), "best_ask": body.get("best_ask"),
    }
    result = _research.research(ctx, mode=_research_mode)
    # ResearchFailure vs ProbabilityEstimateBundle — never an order.
    return JSONResponse(result.model_dump(mode="json"))


# --- Phase 6: venue-neutral read-only endpoints (Polymarket + Kalshi) ---- #
@app.get("/api/venues")
def api_venues() -> dict:
    return {"venues": _venues.venues(), "enabled": enabled_venues()}


@app.get("/api/venues/status")
def api_venues_status() -> dict:
    # config presence only — never secret values
    return {"venues": [s.model_dump() for s in _venues.statuses()]}


@app.get("/api/venues/{venue}/status")
def api_venue_status(venue: str) -> JSONResponse:
    a = _venues.get(venue)
    if a is None:
        return JSONResponse({"error": "unknown venue"}, status_code=404)
    return JSONResponse(a.get_status().model_dump())


@app.get("/api/venues/{venue}/markets")
def api_venue_markets(venue: str, status: str | None = None, limit: int = 50) -> JSONResponse:
    a = _venues.get(venue)
    if a is None or not hasattr(a, "list_markets"):
        return JSONResponse({"error": "unknown venue"}, status_code=404)
    try:
        markets = a.list_markets(MarketFilter(venue=venue, status=status, limit=limit))
        return JSONResponse({"markets": [m.model_dump(mode="json") for m in markets]})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)[:200], "markets": []})


@app.get("/api/venues/{venue}/markets/{market_ref}")
def api_venue_market(venue: str, market_ref: str) -> JSONResponse:
    a = _venues.get(venue)
    if a is None:
        return JSONResponse({"error": "unknown venue"}, status_code=404)
    m = a.get_market(MarketRef.parse(venue, market_ref))
    return JSONResponse(m.model_dump(mode="json") if m else {"error": "not found"})


@app.get("/api/venues/{venue}/orderbook/{market_ref}")
def api_venue_orderbook(venue: str, market_ref: str, outcome: str = "YES") -> JSONResponse:
    ob = _venues.get_orderbook(MarketRef.parse(venue, market_ref), outcome)
    return JSONResponse(ob.model_dump(mode="json") if ob else {"error": "no orderbook"})


@app.get("/api/venues/{venue}/bbo/{market_ref}")
def api_venue_bbo(venue: str, market_ref: str, outcome: str = "YES") -> JSONResponse:
    bbo = _venues.get_bbo(MarketRef.parse(venue, market_ref), outcome)
    return JSONResponse(bbo.model_dump(mode="json") if bbo else {"error": "no bbo"})


@app.get("/api/venues/{venue}/lifecycle")
def api_venue_lifecycle(venue: str, limit: int = 100) -> dict:
    return {"events": _store.get_venue_lifecycle_events(venue=venue, limit=limit)}


@app.get("/api/venues/{venue}/resolution-rules/{market_ref}")
def api_venue_resolution_rules(venue: str, market_ref: str) -> JSONResponse:
    rr = _venues.get_resolution_rules(MarketRef.parse(venue, market_ref))
    if rr is not None:
        return JSONResponse(rr.model_dump(mode="json"))
    ref = MarketRef.parse(venue, market_ref)
    row = _store.get_resolution_rules(venue=venue, market_ticker=ref.market_ticker,
                                      market_id=ref.market_id)
    return JSONResponse(row or {"error": "no resolution rules"})


@app.post("/api/venues/{venue}/sync-metadata")
def api_venue_sync_metadata(venue: str, status: str = "open", limit: int = 50) -> JSONResponse:
    a = _venues.get(venue)
    if a is None or not hasattr(a, "sync_metadata"):
        return JSONResponse({"error": "unknown venue"}, status_code=404)
    try:
        res = a.sync_metadata(MarketFilter(venue=venue, status=status, limit=limit))
        return JSONResponse(res.model_dump())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "error", "error": str(exc)[:200]}, status_code=400)


@app.post("/api/venues/kalshi/smoke-readonly")
def api_kalshi_smoke(max_markets: int = 3) -> JSONResponse:
    """Read-only smoke. NEVER places orders. Returns disabled status if no creds."""
    try:
        from .venues.kalshi.smoke import run_smoke
        return JSONResponse(run_smoke(store=_store, max_markets=max_markets, do_sync=True))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "error", "error": str(exc)[:200]}, status_code=400)


# --- Phase 7: shadow-mode orchestration (NO live orders) ----------------- #
_shadow_state: dict = {"orch": None}


def _latest_shadow_session_id() -> str | None:
    sessions = _store.get_shadow_sessions(1)
    return sessions[0]["shadow_session_id"] if sessions else None


@app.get("/api/shadow/status")
def api_shadow_status() -> dict:
    cfg = ShadowConfig.from_env()
    orch = _shadow_state.get("orch")
    sess = orch.session if orch and orch.session else None
    # config presence only — there are no secrets on ShadowConfig
    return {
        "enabled": cfg.enabled, "mode": cfg.mode, "venues": cfg.venues,
        "active_session_id": sess.shadow_session_id if sess else None,
        "session_status": sess.status if sess else "STOPPED",
        "new_orders_allowed": orch.new_orders_allowed() if orch else False,
        "kill_switch_active": cfg.kill_switch_active(),
        "counters": orch.counters if orch else {},
    }


@app.post("/api/shadow/start")
async def api_shadow_start(request: Request) -> JSONResponse:
    cfg = ShadowConfig.from_env()
    ok, reason = cfg.verify_safe_to_start()
    if not ok:
        return JSONResponse({"status": "disabled", "reason": reason}, status_code=200)
    orch = ShadowOrchestrator(store=_store, config=cfg, registry=_venues,
                              research=_research, risk=getattr(_engine, "risk", None))
    started, sess = orch.start()
    if not started:
        return JSONResponse({"status": "error", "reason": str(sess)}, status_code=200)
    _shadow_state["orch"] = orch
    return JSONResponse({"status": "started", "mode": "shadow_live",
                         "shadow_session_id": sess.shadow_session_id,
                         "note": "no live orders are submitted in shadow mode"})


@app.post("/api/shadow/stop")
def api_shadow_stop() -> dict:
    orch = _shadow_state.get("orch")
    if orch is None:
        return {"status": "no_active_session"}
    sess = orch.stop()
    return {"status": "stopped", "shadow_session_id": sess.shadow_session_id if sess else None}


@app.post("/api/shadow/pause")
def api_shadow_pause() -> dict:
    orch = _shadow_state.get("orch")
    if orch is None:
        return {"status": "no_active_session"}
    orch.pause()
    return {"status": "paused"}


@app.post("/api/shadow/resume")
def api_shadow_resume() -> dict:
    orch = _shadow_state.get("orch")
    if orch is None:
        return {"status": "no_active_session"}
    orch.resume()
    return {"status": "running"}


@app.get("/api/shadow/sessions")
def api_shadow_sessions() -> dict:
    return {"sessions": _store.get_shadow_sessions(50)}


@app.get("/api/shadow/sessions/{shadow_session_id}")
def api_shadow_session(shadow_session_id: str) -> JSONResponse:
    s = _store.get_shadow_session(shadow_session_id)
    if s is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return JSONResponse(s)


def _shadow_rows_ep(shadow_session_id: str, table: str, key: str) -> dict:
    return {key: _store.get_shadow_rows(table, shadow_session_id)}


@app.get("/api/shadow/sessions/{shadow_session_id}/candidates")
def api_shadow_candidates(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_candidates", "candidates")


@app.get("/api/shadow/sessions/{shadow_session_id}/decisions")
def api_shadow_decisions(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_decisions", "decisions")


@app.get("/api/shadow/sessions/{shadow_session_id}/orders")
def api_shadow_orders(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_orders", "orders")


@app.get("/api/shadow/sessions/{shadow_session_id}/fills")
def api_shadow_fills(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_fills", "fills")


@app.get("/api/shadow/sessions/{shadow_session_id}/positions")
def api_shadow_positions(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_positions", "positions")


@app.get("/api/shadow/sessions/{shadow_session_id}/equity")
def api_shadow_equity(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_equity", "equity")


@app.get("/api/shadow/sessions/{shadow_session_id}/observations")
def api_shadow_observations(shadow_session_id: str) -> dict:
    return _shadow_rows_ep(shadow_session_id, "shadow_observations", "observations")


@app.get("/api/shadow/sessions/{shadow_session_id}/alerts")
def api_shadow_alerts(shadow_session_id: str) -> dict:
    return {"alerts": _store.get_shadow_alerts(shadow_session_id, 200)}


@app.get("/api/shadow/sessions/{shadow_session_id}/metrics")
def api_shadow_metrics(shadow_session_id: str) -> dict:
    return compute_session_metrics(_store, shadow_session_id, ShadowConfig.from_env())


@app.get("/api/shadow/readiness")
def api_shadow_readiness() -> JSONResponse:
    sid = _latest_shadow_session_id()
    if sid is None:
        return JSONResponse({"overall_status": "NOT_ENOUGH_DATA", "reason": "no sessions"})
    return api_shadow_session_readiness(sid)


@app.get("/api/shadow/sessions/{shadow_session_id}/readiness")
def api_shadow_session_readiness(shadow_session_id: str) -> JSONResponse:
    cfg = ShadowConfig.from_env()
    metrics = compute_session_metrics(_store, shadow_session_id, cfg)
    counters = {k: metrics.get(k, 0) for k in
                ("risk_bypass_count", "unhandled_exception_count", "live_order_endpoint_calls")}
    counters["reconciliation_clean"] = True
    report = LiveReadinessGate(cfg).evaluate(metrics, counters, shadow_session_id)
    return JSONResponse(report.model_dump(mode="json"))


@app.post("/api/shadow/sessions/{shadow_session_id}/readiness/report")
def api_shadow_readiness_report(shadow_session_id: str) -> JSONResponse:
    cfg = ShadowConfig.from_env()
    metrics = compute_session_metrics(_store, shadow_session_id, cfg)
    counters = {"reconciliation_clean": True}
    report = LiveReadinessGate(cfg).evaluate(metrics, counters, shadow_session_id)
    try:
        write_report(_store, shadow_session_id, cfg, report, metrics)
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"report_id": report.report_id,
                         "overall_status": report.overall_status})


@app.get("/api/shadow/readiness/reports")
def api_shadow_reports() -> dict:
    return {"reports": _store.get_readiness_reports(limit=50)}


@app.get("/api/shadow/readiness/reports/{report_id}")
def api_shadow_report(report_id: str) -> JSONResponse:
    r = _store.get_readiness_report(report_id)
    if r is None:
        return JSONResponse({"error": "unknown report"}, status_code=404)
    return JSONResponse(r)


# --- Phase 8: guarded-live design skeleton (DRY-RUN ONLY; door stays shut) - #
_gl = {"sm": GuardedLiveStateMachine(store=_store, config=GuardedLiveConfig.from_env())}


def _gl_cfg() -> GuardedLiveConfig:
    return GuardedLiveConfig.from_env()


@app.get("/api/guarded-live/status")
def api_gl_status() -> dict:
    cfg = _gl_cfg()
    sm = _gl["sm"]
    # config presence only — no secrets are present on GuardedLiveConfig
    return {"enabled": cfg.enabled, "mode": cfg.mode, "dry_run_only": True,
            "no_live_execution": True, "state": sm.state,
            "kill_switch_active": cfg.kill_switch_active(),
            "config_hash": cfg.config_hash()}


@app.post("/api/guarded-live/precheck")
async def api_gl_precheck(request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    cfg = _gl_cfg()
    conf = ConformanceHarness(store=_store, config=cfg).run()
    pre = run_precheck(_store, cfg, readiness_report_id=body.get("readiness_report_id"),
                       conformance_ok=(conf.status == "PASS"))
    try:
        _gl["sm"].transition("PRECHECK_PASSED" if pre.status == "PASS" else "PRECHECK_FAILED",
                             reason="precheck", actor="api")
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"precheck_id": pre.precheck_id, "status": pre.status,
                         "hard_fail_count": pre.hard_fail_count, "no_live_execution": True})


@app.get("/api/guarded-live/prechecks")
def api_gl_prechecks() -> dict:
    return {"prechecks": _store.get_guarded_live_prechecks(50)}


@app.get("/api/guarded-live/prechecks/{precheck_id}")
def api_gl_precheck_detail(precheck_id: str) -> JSONResponse:
    p = _store.get_guarded_live_precheck(precheck_id)
    return JSONResponse(p or {"error": "not found"}, status_code=200 if p else 404)


@app.post("/api/guarded-live/approval-batches")
async def api_gl_create_batch(request: Request) -> JSONResponse:
    body = await _json(request)
    cfg = _gl_cfg()
    batch = ApprovalWorkflow(_store, cfg).create_batch(
        readiness_report_id=body.get("readiness_report_id", ""), config_hash=cfg.config_hash())
    return JSONResponse({"approval_batch_id": batch.approval_batch_id, "status": batch.status})


@app.get("/api/guarded-live/approval-batches")
def api_gl_batches() -> dict:
    return {"approval_batches": _store.get_approval_batches(50)}


@app.get("/api/guarded-live/approval-batches/{approval_batch_id}")
def api_gl_batch(approval_batch_id: str) -> JSONResponse:
    b = _store.get_approval_batch(approval_batch_id)
    return JSONResponse(b or {"error": "not found"}, status_code=200 if b else 404)


@app.post("/api/guarded-live/approval-batches/{approval_batch_id}/approve")
async def api_gl_approve(approval_batch_id: str, request: Request) -> JSONResponse:
    body = await _json(request)
    cfg = _gl_cfg()
    row = _store.get_approval_batch(approval_batch_id)
    if row is None:
        return JSONResponse({"error": "unknown batch"}, status_code=404)
    batch = ApprovalBatch(**{k: row[k] for k in (
        "approval_batch_id", "readiness_report_id", "config_hash", "required_approvals",
        "valid_approvals", "status", "created_ts_ms", "expires_ts_ms") if k in row})
    ok, res = ApprovalWorkflow(_store, cfg).approve(
        batch, approver_id=body.get("approver_id", ""), role=body.get("role", ""),
        confirmation_text=body.get("confirm", ""), readiness_report_id=batch.readiness_report_id,
        config_hash=cfg.config_hash(), approval_reason=body.get("reason", ""))
    return JSONResponse({"accepted": ok, "detail": (res if isinstance(res, str) else "approved"),
                         "batch_status": batch.status, "no_live_execution": True})


@app.post("/api/guarded-live/approval-batches/{approval_batch_id}/revoke")
def api_gl_revoke_batch(approval_batch_id: str) -> dict:
    _store.upsert_approval_batch({"approval_batch_id": approval_batch_id, "status": "REVOKED",
                                  "readiness_report_id": "", "config_hash": "",
                                  "required_approvals": 0, "valid_approvals": 0,
                                  "created_ts_ms": 0, "expires_ts_ms": 0})
    return {"status": "REVOKED"}


@app.post("/api/guarded-live/arm-dry-run")
async def api_gl_arm_dry_run(request: Request) -> JSONResponse:
    """Arms DRY-RUN ONLY. Can never set a live state."""
    body = await _json(request)
    cfg = _gl_cfg()
    row = _store.get_approval_batch(body.get("approval_batch_id", ""))
    if row is None or row.get("status") != "APPROVED_DRY_RUN_ONLY":
        return JSONResponse({"armed": False, "reason": "approval batch not APPROVED_DRY_RUN_ONLY",
                             "mode": "dry_run_only", "no_live_execution": True}, status_code=200)
    batch = ApprovalBatch(**{k: row[k] for k in (
        "approval_batch_id", "readiness_report_id", "config_hash", "required_approvals",
        "valid_approvals", "status", "created_ts_ms", "expires_ts_ms") if k in row})
    plain, rec = ArmingTokenManager(_store, cfg).issue(batch)
    for to in ("APPROVED_DRY_RUN_ONLY", "ARMED_DRY_RUN_ONLY"):
        try:
            _gl["sm"].transition(to, reason="arm_dry_run", actor="api")
        except Exception:  # noqa: BLE001
            pass
    return JSONResponse({"armed": True, "mode": "dry_run_only", "state": _gl["sm"].state,
                         "arming_token": plain, "arming_token_id": rec.arming_token_id,
                         "no_live_execution": True,
                         "note": "DRY-RUN ONLY — this token can never enable live execution"})


@app.post("/api/guarded-live/dry-run-intent")
async def api_gl_dry_run_intent(request: Request) -> JSONResponse:
    if _gl["sm"].state not in ("ARMED_DRY_RUN_ONLY", "DRY_RUN_ACTIVE"):
        return JSONResponse({"error": "must be ARMED_DRY_RUN_ONLY to create a dry-run intent",
                             "state": _gl["sm"].state, "no_live_execution": True}, status_code=200)
    body = await _json(request)
    cfg = _gl_cfg()
    safe = SafetyEnvelope(cfg, state=_gl["sm"].state).validate(body)
    _store.add_safety_envelope_decision(safe.record())
    drb = DryRunLiveBroker(_store, cfg)
    intent = drb.validate_order(body, risk_decision_id=body.get("risk_decision_id", "rd-stub"),
                                safety_envelope_decision_id=safe.decision_id)
    return JSONResponse({"dry_run_intent_id": intent.dry_run_intent_id, "status": intent.status,
                         "unsigned": intent.unsigned, "unsent": intent.unsent,
                         "signer_used": intent.signer_used, "network_called": intent.network_called,
                         "no_live_execution": True})


@app.get("/api/guarded-live/dry-run-intents")
def api_gl_dry_run_intents() -> dict:
    return {"dry_run_intents": _store.get_guarded_rows("dry_run_order_intents", 200)}


@app.get("/api/guarded-live/dry-run-intents/{dry_run_intent_id}")
def api_gl_dry_run_intent_detail(dry_run_intent_id: str) -> JSONResponse:
    i = _store.get_dry_run_order_intent(dry_run_intent_id)
    return JSONResponse(i or {"error": "not found"}, status_code=200 if i else 404)


@app.post("/api/guarded-live/conformance/run")
def api_gl_conformance_run() -> dict:
    run = ConformanceHarness(store=_store, config=_gl_cfg()).run()
    return {"conformance_run_id": run.conformance_run_id, "status": run.status,
            "pass_count": run.pass_count, "fail_count": run.fail_count}


@app.get("/api/guarded-live/conformance/runs")
def api_gl_conformance_runs() -> dict:
    return {"runs": _store.get_conformance_runs(50)}


@app.get("/api/guarded-live/conformance/runs/{conformance_run_id}")
def api_gl_conformance_detail(conformance_run_id: str) -> JSONResponse:
    r = _store.get_conformance_run(conformance_run_id)
    return JSONResponse(r or {"error": "not found"}, status_code=200 if r else 404)


@app.post("/api/guarded-live/report")
def api_gl_report() -> dict:
    cfg = _gl_cfg()
    out = gl_write_report(_store, cfg, state=_gl["sm"].state)
    return {"report_path": str(out), "no_live_execution": True}


@app.get("/api/guarded-live/audit-events")
def api_gl_audit() -> dict:
    return {"audit_events": _store.get_guarded_live_audit_events(200)}


@app.get("/api/guarded-live/secret-policy/violations")
def api_gl_secret_violations() -> dict:
    return {"violations": _store.get_guarded_rows("secret_policy_violations", 200)}


# --- Phase 9: micro-live (READ-ONLY/status only; NO real submit endpoint) -- #
# There is intentionally NO POST /api/micro-live/submit, /cancel or /live-order.
# Real submission is CLI-only, one-canary-only, and blocked unless all locks pass.


@app.get("/api/micro-live/status")
def api_ml_status() -> dict:
    from .micro_live import MicroLiveConfig, all_pass, check_locks
    cfg = MicroLiveConfig.from_env()
    res = check_locks(cfg)
    return {"enabled": cfg.enabled, "environment": cfg.environment,
            "production_allowed": cfg.allow_production, "cli_only": cfg.cli_only,
            "all_locks_open": all_pass(res), "live_submit_blocked": not all_pass(res),
            "max_order_notional_usd": str(cfg.max_order_notional_usd),
            "allowed_venues": cfg.allowed_venues, "allowed_order_types": cfg.allowed_order_types,
            "allowed_tif": cfg.allowed_tif, "no_live_submit_endpoint": True,
            "no_dashboard_submit_button": True, "config": cfg.public_dict()}


@app.get("/api/micro-live/locks")
def api_ml_locks() -> dict:
    from .micro_live import MicroLiveConfig, all_pass, check_locks
    res = check_locks(MicroLiveConfig.from_env())
    return {"all_locks_open": all_pass(res),
            "locks": [{"lock_name": r.lock_name, "passed": r.passed, "reason": r.reason,
                       "observed_value_redacted": r.observed_value_redacted} for r in res]}


@app.get("/api/micro-live/canary-plans")
def api_ml_plans() -> dict:
    return {"canary_plans": _store.get_micro_live_canary_plans(50)}


@app.get("/api/micro-live/canary-plans/{canary_plan_id}")
def api_ml_plan(canary_plan_id: str) -> dict:
    return {"canary_plan": _store.get_micro_live_canary_plan(canary_plan_id)}


@app.get("/api/micro-live/preflights")
def api_ml_preflights() -> dict:
    return {"preflights": _store.get_micro_live_preflights(50)}


@app.get("/api/micro-live/order-attempts")
def api_ml_attempts() -> dict:
    return {"order_attempts": _store.get_micro_live_attempts(50)}


@app.get("/api/micro-live/order-attempts/{live_order_attempt_id}")
def api_ml_attempt(live_order_attempt_id: str) -> dict:
    return {"order_attempt": _store.get_micro_live_attempt(live_order_attempt_id)}


@app.get("/api/micro-live/reconciliations")
def api_ml_recons() -> dict:
    return {"reconciliations": _store.get_micro_live_reconciliations(50)}


@app.get("/api/micro-live/reports")
def api_ml_reports() -> dict:
    return {"reports": _store.get_micro_live_reports(50)}


@app.get("/api/micro-live/audit-events")
def api_ml_audit() -> dict:
    return {"audit_events": _store.get_micro_live_audit_events(200)}


@app.post("/api/micro-live/canary-plans")
async def api_ml_create_plan(request: Request) -> dict:
    from .micro_live import MicroLiveConfig
    from .micro_live.canary_plan import create_canary_plan
    body = await _json(request)
    plan, errs = create_canary_plan(
        _store, MicroLiveConfig.from_env(),
        dry_run_intent_id=body.get("dry_run_intent_id", ""),
        readiness_report_id=body.get("readiness_report_id"),
        venue=body.get("venue", "kalshi"), environment=body.get("environment", "demo"),
        approval_batch_id=body.get("approval_batch_id"),
        arming_token_id=body.get("arming_token_id"))
    return {"canary_plan_id": plan.canary_plan_id, "status": plan.status, "errors": errs,
            "no_submit": True}


@app.post("/api/micro-live/preflight")
async def api_ml_preflight(request: Request) -> dict:
    from .micro_live import MicroLiveConfig
    from .micro_live.preflight import preflight_canary_plan
    from .micro_live.schemas import MicroLiveCanaryPlan
    body = await _json(request)
    row = _store.get_micro_live_canary_plan(body.get("canary_plan_id", ""))
    if not row:
        return {"error": "canary_plan_not_found"}
    plan = MicroLiveCanaryPlan(**{k: row.get(k) for k in MicroLiveCanaryPlan.model_fields
                                  if k in row})
    result, _, _ = preflight_canary_plan(_store, MicroLiveConfig.from_env(), plan)
    return {"preflight_id": result.preflight_id, "status": result.status,
            "hard_fail_count": result.hard_fail_count, "no_submit": True}


@app.post("/api/micro-live/report")
async def api_ml_report(request: Request) -> dict:
    body = await _json(request)
    reports = _store.get_micro_live_reports(100)
    if body.get("live_order_attempt_id"):
        reports = [r for r in reports
                   if r.get("live_order_attempt_id") == body["live_order_attempt_id"]]
    return {"report": reports[0] if reports else None}


# --- Phase 10: post-canary analysis & scaling-VETO (read-only / non-exec) --- #
# NO submit, NO cancel, NO scale, NO production-unlock, NO size-increase routes.


@app.get("/api/post-canary/analyses")
def api_pc_analyses() -> dict:
    return {"analyses": _store.get_post_canary_analyses(50)}


@app.get("/api/post-canary/analyses/{analysis_id}")
def api_pc_analysis(analysis_id: str) -> dict:
    return {"analysis": _store.get_post_canary_analysis(analysis_id)}


@app.get("/api/post-canary/analyses/{analysis_id}/checks")
def api_pc_checks(analysis_id: str) -> dict:
    return {"checks": _store.get_post_canary_audit_checks(analysis_id)}


@app.get("/api/post-canary/analyses/{analysis_id}/markout")
def api_pc_markout(analysis_id: str) -> dict:
    return {"markout": _store.get_post_canary_markout(analysis_id)}


@app.get("/api/post-canary/analyses/{analysis_id}/report")
def api_pc_report(analysis_id: str) -> dict:
    reports = [r for r in _store.get_post_canary_reports(200)
               if r.get("analysis_id") == analysis_id]
    return {"report": reports[0] if reports else None}


@app.get("/api/post-canary/eligibility")
def api_pc_eligibility(venue: str = "kalshi", environment: str = "demo") -> dict:
    from .post_canary import PostCanaryConfig, compute_eligibility
    elig = compute_eligibility(_store, PostCanaryConfig.from_env(), venue, environment)
    return {"eligibility": elig.model_dump(), "size_increase": False,
            "autonomous_live": False, "production_execution": "NOT_IMPLEMENTED"}


@app.get("/api/post-canary/latest")
def api_pc_latest() -> dict:
    rows = _store.get_post_canary_analyses(1)
    return {"latest": rows[0] if rows else None}


@app.post("/api/post-canary/analyze")
async def api_pc_analyze(request: Request) -> dict:
    # NEVER submits/cancels orders. Read-only refresh is opt-in + disabled by default.
    from .post_canary import PostCanaryAnalysisRequest, PostCanaryAnalyzer, PostCanaryConfig
    body = await _json(request)
    req = PostCanaryAnalysisRequest(
        live_order_attempt_id=body.get("live_order_attempt_id", ""),
        refresh_readonly_exchange_state=False, generated_by="api")
    if not req.live_order_attempt_id:
        return {"error": "live_order_attempt_id_required", "no_execution": True}
    res = PostCanaryAnalyzer(_store, PostCanaryConfig.from_env()).analyze(req)
    return {"analysis_id": res.analysis_id, "status": res.status,
            "recommendation": res.recommendation, "blocking_reasons": res.blocking_reasons,
            "eligible_for_size_increase": False, "eligible_for_autonomous_live": False,
            "no_execution": True}


@app.post("/api/post-canary/report")
async def api_pc_report_post(request: Request) -> dict:
    body = await _json(request)
    reports = _store.get_post_canary_reports(200)
    if body.get("analysis_id"):
        reports = [r for r in reports if r.get("analysis_id") == body["analysis_id"]]
    return {"report": reports[0] if reports else None}


# --- Phase 11: production-canary DESIGN REVIEW (read-only / non-execution) --- #
# NO submit, NO cancel, NO enable-production, NO arm-production, NO scale,
# NO increase-size, NO live-order routes. Design review only.


@app.get("/api/production-review/status")
def api_pr_status() -> dict:
    from .production_review import ProductionReviewConfig
    cfg = ProductionReviewConfig.from_env()
    rows = _store.get_production_review_runs(1)
    return {"enabled": cfg.enabled, "production_execution": "NOT_IMPLEMENTED",
            "size_increase": "NOT_APPROVED", "autonomous_live": "NOT_APPROVED",
            "dashboard_submit": "NOT_AVAILABLE", "api_submit": "NOT_AVAILABLE",
            "latest": rows[0] if rows else None}


@app.get("/api/production-review/runs")
def api_pr_runs() -> dict:
    return {"runs": _store.get_production_review_runs(50)}


@app.get("/api/production-review/runs/{review_id}")
def api_pr_run(review_id: str) -> dict:
    return {"run": _store.get_production_review_run(review_id)}


@app.get("/api/production-review/runs/{review_id}/checks")
def api_pr_checks(review_id: str) -> dict:
    return {"checks": _store.get_production_review_checks(review_id)}


@app.get("/api/production-review/runs/{review_id}/report")
def api_pr_report(review_id: str) -> dict:
    reports = [r for r in _store.get_production_review_reports(200)
               if r.get("review_id") == review_id]
    return {"report": reports[0] if reports else None}


@app.get("/api/production-review/evidence")
def api_pr_evidence() -> dict:
    from .production_review import ProductionReviewConfig
    from .production_review.evidence_loader import load
    ctx = load(_store, ProductionReviewConfig.from_env())
    ev = ctx.get("evidence_summary")
    return {"evidence": ev.model_dump() if ev else None}


@app.get("/api/production-review/conformance")
def api_pr_conformance() -> dict:
    return {"conformance_runs": _store.get_production_conformance_runs(50)}


@app.post("/api/production-review/conformance/run")
async def api_pr_conformance_run(request: Request) -> dict:
    from .production_review import ProductionReviewConfig
    from .production_review import production_conformance as pc
    run = pc.run(ProductionReviewConfig.from_env())
    try:
        _store.add_production_conformance_run(run.record(None))
    except Exception:  # noqa: BLE001
        pass
    return {"conformance_run_id": run.conformance_run_id, "status": run.status,
            "mock_only": True, "real_network_calls": run.real_network_calls,
            "production_order_calls": run.production_order_calls, "no_execution": True}


@app.post("/api/production-review/run")
async def api_pr_run_review(request: Request) -> dict:
    # NEVER submits/cancels/signs. Read-only design review.
    from .production_review import ProductionReviewConfig, ProductionReviewRequest, run_review
    body = await _json(request)
    req = ProductionReviewRequest(generated_by="api",
                                  include_mock_production_conformance=True)
    res = run_review(_store, ProductionReviewConfig.from_env(), request=req)
    return {"review_id": res.review_id, "status": res.status,
            "recommendation": res.recommendation, "blocking_reasons": res.blocking_reasons,
            "eligible_to_draft_phase12_plan": res.eligible_to_draft_phase12_plan,
            "eligible_for_production_execution": False, "eligible_for_size_increase": False,
            "eligible_for_autonomous_live": False, "no_execution": True}


@app.get("/api/production-review/attestations")
def api_pr_attestations() -> dict:
    atts = _store.get_production_jurisdiction_attestations(200)
    # never include anything beyond redacted fields already stored
    return {"attestations": atts}


def _create_attestation(body: dict, kind: str) -> dict:
    from .production_review import ProductionReviewConfig
    from .production_review.jurisdiction import create_attestation
    cfg = ProductionReviewConfig.from_env()
    att, errs = create_attestation(
        kind=kind, reviewer_id=body.get("reviewer_id", ""), venue=body.get("venue", "kalshi"),
        confirmation_text=body.get("confirm", ""), expiry_hours=cfg.approval_expiry_hours,
        account_identifier=body.get("account_identifier", ""))
    if not errs:
        try:
            _store.add_production_jurisdiction_attestation(att.record())
        except Exception:  # noqa: BLE001
            pass
    return {"attestation_id": att.attestation_id, "status": att.status, "errors": errs,
            "kind": kind}


@app.post("/api/production-review/attestations/jurisdiction")
async def api_pr_attest_jur(request: Request) -> dict:
    return _create_attestation(await _json(request), "jurisdiction")


@app.post("/api/production-review/attestations/account-readiness")
async def api_pr_attest_acct(request: Request) -> dict:
    return _create_attestation(await _json(request), "account-readiness")


@app.post("/api/production-review/attestations/venue-terms")
async def api_pr_attest_venue(request: Request) -> dict:
    return _create_attestation(await _json(request), "venue-terms")


@app.get("/api/production-review/change-control")
def api_pr_cc() -> dict:
    return {"change_control": _store.get_production_change_control(50)}


@app.post("/api/production-review/change-control")
async def api_pr_cc_create(request: Request) -> dict:
    from .production_review import ProductionReviewConfig
    from .production_review.change_control import create_change_control
    body = await _json(request)
    cfg = ProductionReviewConfig.from_env()
    rec = create_change_control(
        requester_id=body.get("requester_id", ""), reviewers=body.get("reviewers", []),
        review_id=body.get("review_id", ""), risk_summary=body.get("risk_summary", ""),
        expiry_hours=cfg.approval_expiry_hours,
        approve_design_only=bool(body.get("approve_design_only")))
    try:
        _store.add_production_change_control(rec.record())
    except Exception:  # noqa: BLE001
        pass
    return {"change_id": rec.change_id, "approval_status": rec.approval_status,
            "no_execution": True}


@app.get("/api/production-review/checklist")
def api_pr_checklist() -> dict:
    return {"checklists": _store.get_production_human_checklists(50)}


@app.post("/api/production-review/checklist")
async def api_pr_checklist_create(request: Request) -> dict:
    from .production_review.human_checklist import build_checklist
    body = await _json(request)
    hc = build_checklist(reviewer_id=body.get("reviewer_id", ""),
                         review_id=body.get("review_id", ""),
                         item_results=body.get("items", {}),
                         confirmation_text=body.get("confirm", ""))
    try:
        _store.add_production_human_checklist(hc.record())
    except Exception:  # noqa: BLE001
        pass
    return {"checklist_id": hc.checklist_id, "status": hc.status, "no_execution": True}


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/arb")
def api_arb() -> dict:
    return _arb.snapshot()


@app.post("/api/arb/{flag}")
def api_arb_toggle(flag: str) -> dict:
    # Arbitrage is permanently disabled (Polymarket-only PAPER training). The
    # toggle is inert: it can never turn arbitrage on, regardless of `flag`.
    _arb.enabled = False
    return {"arb_enabled": False, "permanently_disabled": True,
            "reason": "arbitrage removed — Polymarket-only PAPER training"}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_text(json.dumps(_snapshot(), default=str))
            await asyncio.sleep(max(0.5, settings.tick_seconds))
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
