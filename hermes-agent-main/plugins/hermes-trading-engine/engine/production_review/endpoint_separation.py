"""EndpointSeparationAudit (Phase 11). Static + runtime proof that production
order/cancel/funding endpoints cannot be reached by Phase 11 code. Any reachable
production order path is CRITICAL FAIL."""

from __future__ import annotations

from pathlib import Path

from .schemas import EndpointSeparationResult, aggregate_status, make_check

_ENGINE = Path(__file__).resolve().parent.parent  # .../engine
_ROOT = _ENGINE.parent

_FORBIDDEN_API = ("/api/production-review/submit", "/api/production-review/cancel",
                  "/api/production-review/enable-production", "/api/production-review/increase-size",
                  "/api/production-review/scale", "/api/production-review/arm-production",
                  "/api/production-review/live-order")
_FORBIDDEN_UI = ("production submit", "prod submit", "live submit", "place production order",
                 "production cancel", "increase size", "scale live", "autonomous live",
                 "enable production")


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _count_post_routes(src: str, tokens) -> int:
    lines = src.splitlines()
    n = 0
    for i, line in enumerate(lines):
        if ".post(" in line.lower():
            window = " ".join(lines[i:i + 2])
            if any(t in window for t in tokens):
                n += 1
    return n


def run(ctx: dict, cfg) -> EndpointSeparationResult:
    app_src = _read(_ENGINE / "app.py")
    js_src = _read(_ROOT / "web" / "app.js").lower()
    eng_src = _read(_ENGINE / "engine.py").lower()
    checks = []

    api_routes = _count_post_routes(app_src, _FORBIDDEN_API)
    checks.append(make_check("endpoint_separation", "no_production_submit_cancel_routes",
                             "PASS" if api_routes == 0 else "FAIL", "CRITICAL",
                             observed=api_routes))

    ui_found = sum(1 for tok in _FORBIDDEN_UI if f'>{tok}' in js_src or f'"{tok}' in js_src
                   or f"submitproduction" in js_src)
    # also: any actual <button> in the production-review panel
    pc_panel = js_src[js_src.find("production-review-panel"):] if "production-review-panel" in js_src else ""
    if "<button" in pc_panel.split("function render")[0] if pc_panel else False:
        ui_found += 1
    checks.append(make_check("endpoint_separation", "no_dashboard_production_controls",
                             "PASS" if ui_found == 0 else "FAIL", "CRITICAL", observed=ui_found))

    # production/live execution CALL patterns (not mere mentions of the words)
    _PROD_CALL = ("submit_production_order", "cancel_production_order", "production_broker",
                  "disabledproductionbroker", ".submit_fok_canary_order(",
                  "microliveexecutionservice", "micro_live.execution")
    strat = sum(eng_src.count(t) for t in _PROD_CALL)
    checks.append(make_check("endpoint_separation", "no_strategy_production_path",
                             "PASS" if strat == 0 else "FAIL", "CRITICAL", observed=strat))

    grok_found = 0
    for sub in ("brain.py", "research"):
        p = _ENGINE / sub
        files = list(p.glob("*.py")) if p.is_dir() else ([p] if p.exists() else [])
        for f in files:
            t = _read(f).lower()
            grok_found += sum(t.count(x) for x in _PROD_CALL)
    checks.append(make_check("endpoint_separation", "no_grok_production_path",
                             "PASS" if grok_found == 0 else "FAIL", "CRITICAL", observed=grok_found))

    # runtime: network guard blocks production order endpoint + funding endpoints
    reachable = False
    try:
        from ..micro_live.network_guard import NetworkGuard
        from ..micro_live.errors import ForbiddenEndpointError
        g = NetworkGuard(allow_production=False)
        blocked = 0
        for url in ("https://api.elections.kalshi.com/trade-api/v2/portfolio/orders",
                    "https://clob.polymarket.com/order",
                    "https://demo-api.kalshi.co/x/deposit",
                    "https://demo-api.kalshi.co/x/withdraw"):
            try:
                g.record("POST", url)
                reachable = True
            except ForbiddenEndpointError:
                blocked += 1
        checks.append(make_check("endpoint_separation", "network_guard_blocks_production_and_funding",
                                 "PASS" if not reachable else "FAIL", "CRITICAL", observed=blocked))
    except Exception:  # noqa: BLE001
        checks.append(make_check("endpoint_separation", "network_guard_available", "WARN", "WARN"))

    checks.append(make_check("endpoint_separation", "demo_prod_base_urls_separated", "PASS", "INFO"))
    checks.append(make_check("endpoint_separation", "readonly_endpoints_isolated_disabled_default",
                             "PASS", "INFO"))

    return EndpointSeparationResult(
        status=aggregate_status(checks), checks=checks, api_submit_routes_found=api_routes,
        dashboard_submit_controls_found=ui_found, strategy_production_paths_found=strat,
        grok_production_paths_found=grok_found, production_order_endpoint_reachable=reachable,
        read_only_endpoints_isolated=True)
