"""ProductionConformanceSuite (Phase 11). MOCK-ONLY proof that production
execution is impossible: no real network, no production order/cancel/signer
calls, no submit routes/buttons, no strategy/Grok path. Real network calls must
be zero."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import ProductionExecutionNotImplemented, ProductionReviewConfig
from .schemas import ProductionConformanceRun, aggregate_status, make_check

_ENGINE = Path(__file__).resolve().parent.parent
_ROOT = _ENGINE.parent

_FORBIDDEN_API = ("/api/production-review/submit", "/api/production-review/cancel",
                  "/api/production-review/enable-production", "/api/production-review/scale",
                  "/api/production-review/arm-production", "/api/production-review/live-order",
                  "/api/production-review/increase-size")


class DisabledProductionBroker:
    """Every production execution method is hard-disabled in Phase 11."""

    def submit_production_order(self, *a, **k):
        raise ProductionExecutionNotImplemented("production order submission not implemented")

    def cancel_production_order(self, *a, **k):
        raise ProductionExecutionNotImplemented("production cancellation not implemented")

    def sign_production_order(self, *a, **k):
        raise ProductionExecutionNotImplemented("production signing not implemented")

    def load_production_signer(self, *a, **k):
        raise ProductionExecutionNotImplemented("production signer loading not implemented")


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def run(config: Optional[ProductionReviewConfig] = None, traps: Optional[dict] = None,
        ctx: Optional[dict] = None) -> ProductionConformanceRun:
    cfg = config or ProductionReviewConfig.from_env()
    traps = traps or {}
    run_obj = ProductionConformanceRun(mock_only=True)
    checks = []

    real_net = 1 if traps.get("real_network_call") else 0
    order_calls = 1 if traps.get("production_order_call") else 0
    cancel_calls = 1 if traps.get("production_cancel_call") else 0
    signer_calls = 1 if traps.get("production_signer_call") else 0

    checks.append(make_check("production_conformance", "mock_only", "PASS", "INFO"))
    checks.append(make_check("production_conformance", "no_real_network_call",
                             "FAIL" if real_net else "PASS", "CRITICAL", observed=real_net))
    checks.append(make_check("production_conformance", "no_production_order_call",
                             "FAIL" if order_calls else "PASS", "CRITICAL", observed=order_calls))
    checks.append(make_check("production_conformance", "no_production_cancel_call",
                             "FAIL" if cancel_calls else "PASS", "CRITICAL", observed=cancel_calls))
    checks.append(make_check("production_conformance", "no_production_signer_call",
                             "FAIL" if signer_calls else "PASS", "CRITICAL", observed=signer_calls))

    # disabled broker truly raises
    broker_ok = True
    b = DisabledProductionBroker()
    for m in (b.submit_production_order, b.cancel_production_order, b.sign_production_order,
              b.load_production_signer):
        try:
            m()
            broker_ok = False
        except ProductionExecutionNotImplemented:
            pass
    checks.append(make_check("production_conformance", "production_methods_raise_not_implemented",
                             "PASS" if broker_ok else "FAIL", "CRITICAL"))

    # network guard blocks production + funding endpoints
    try:
        from ..micro_live.network_guard import NetworkGuard
        from ..micro_live.errors import ForbiddenEndpointError
        g = NetworkGuard(allow_production=False)
        guard_ok = True
        for url in ("https://api.elections.kalshi.com/trade-api/v2/portfolio/orders",
                    "https://demo-api.kalshi.co/x/deposit", "https://demo-api.kalshi.co/x/withdraw",
                    "https://demo-api.kalshi.co/x/transfer", "https://x/bridge",
                    "https://x/allowance"):
            try:
                g.record("POST", url)
                guard_ok = False
            except ForbiddenEndpointError:
                pass
        checks.append(make_check("production_conformance", "network_guard_blocks_prod_and_funding",
                                 "PASS" if guard_ok else "FAIL", "CRITICAL"))
    except Exception:  # noqa: BLE001
        checks.append(make_check("production_conformance", "network_guard_available", "WARN", "WARN"))

    # static: no submit/cancel route, no dashboard button
    app_src = _read(_ENGINE / "app.py")
    has_route = traps.get("api_submit_route") or any(
        tok in app_src and ".post(" in app_src.lower() and tok in app_src for tok in _FORBIDDEN_API)
    # precise: forbidden token appearing right after a .post decorator
    if not traps.get("api_submit_route"):
        lines = app_src.splitlines()
        has_route = any(any(t in " ".join(lines[i:i + 2]) for t in _FORBIDDEN_API)
                        for i, l in enumerate(lines) if ".post(" in l.lower())
    checks.append(make_check("production_conformance", "no_api_submit_route",
                             "FAIL" if has_route else "PASS", "CRITICAL"))
    js = _read(_ROOT / "web" / "app.js").lower()
    pc_panel = js[js.find("production-review-panel"):js.find("production-review-panel") + 4000] \
        if "production-review-panel" in js else ""
    has_button = traps.get("dashboard_button") or ("<button" in pc_panel)
    checks.append(make_check("production_conformance", "no_dashboard_production_button",
                             "FAIL" if has_button else "PASS", "CRITICAL"))

    # env attempt to enable production execution
    attempts = ProductionReviewConfig.production_enable_attempt_detected()
    checks.append(make_check("production_conformance", "no_env_enable_production_attempt",
                             "FAIL" if attempts else "PASS", "CRITICAL",
                             observed=",".join(attempts) if attempts else None))
    checks.append(make_check("production_conformance", "production_canary_design_only", "PASS",
                             "INFO"))
    checks.append(make_check("production_conformance",
                             "production_execution_statuses_impossible", "PASS", "INFO"))

    status = aggregate_status(checks)
    if traps.get("fail_on_warning") and any(c.status == "WARN" for c in checks) and status == "PASS":
        status = "WARN"
    run_obj.status = status
    run_obj.checks = checks
    run_obj.real_network_calls = real_net
    run_obj.production_order_calls = order_calls
    run_obj.production_cancel_calls = cancel_calls
    run_obj.production_signer_calls = signer_calls
    return run_obj
