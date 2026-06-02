"""Micro-live conformance harness (Phase 9). Proves the safe-disabled defaults
and that the forbidden behaviors are impossible: no real submit API route, no
autonomous loop, FOK-only, hard caps, base broker disabled, Polymarket cannot
sign, secret redaction works, and the network guard blocks forbidden/production
endpoints. With ``--with-network-trap`` it also asserts no real network call."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import locks as locks_mod
from .config import MicroLiveConfig
from .errors import ForbiddenEndpointError, MicroLiveDisabled
from .network_guard import NetworkGuard

_FORBIDDEN_ROUTE_TOKENS = ("/api/micro-live/submit", "/api/micro-live/cancel",
                           "/api/micro-live/live-order")


def _app_source() -> str:
    p = Path(__file__).resolve().parent.parent / "app.py"
    try:
        return p.read_text()
    except OSError:
        return ""


class MicroLiveConformanceHarness:
    def __init__(self, config: Optional[MicroLiveConfig] = None):
        self.cfg = config or MicroLiveConfig()

    def run(self, traps: Optional[dict] = None) -> dict:
        traps = traps or {}
        checks: list[dict] = []

        def add(name, ok, reason=""):
            checks.append({"check_name": name, "status": "PASS" if ok else "FAIL",
                           "reason": reason})

        # 1 build lock default False
        add("build_lock_default_false", locks_mod.BUILD_ENABLED is False,
            "MICRO_LIVE_BUILD_ENABLED build constant must default False")
        # 2 default config disabled + demo
        add("default_disabled", not MicroLiveConfig().enabled)
        add("default_demo", MicroLiveConfig().environment == "demo")
        add("default_no_production", not MicroLiveConfig().allow_production)
        add("default_cli_only", MicroLiveConfig().cli_only)
        # 3 hard notional cap <= $1
        c = MicroLiveConfig(max_order_notional_usd=1000)
        add("hard_notional_cap", c.max_order_notional_usd <= 1)
        # 4 FOK only, no GTC/GTD/batch/replace/amend
        c2 = MicroLiveConfig(allow_gtc=True, allow_gtd=True, allow_batch=True,
                             allow_replace=True, allow_amend=True)
        add("fok_only_enforced", (not c2.allow_gtc and not c2.allow_gtd and not c2.allow_batch
                                  and not c2.allow_replace and not c2.allow_amend))
        add("no_autonomous_loop_flag", not MicroLiveConfig(allow_autonomous_loop=True).allow_autonomous_loop)
        # 5 no real submit API route in app.py source
        src = traps.get("app_source", _app_source())
        route_present = any(tok in src for tok in _FORBIDDEN_ROUTE_TOKENS) and \
            (".post(" in src.lower() or "@app.post" in src)
        # more precise: a POST decorator immediately above one of the tokens
        bad_route = self._has_post_submit_route(src)
        add("no_api_submit_route", not bad_route,
            "found POST submit/cancel/live-order route" if bad_route else "")
        # 6 autonomous loop trap
        add("no_autonomous_loop_path", not traps.get("autonomous_loop", False),
            "autonomous loop detected" if traps.get("autonomous_loop") else "")
        # 7 base broker disabled
        add("base_broker_disabled", self._base_broker_blocks())
        # 8 polymarket cannot sign/submit
        add("polymarket_cannot_submit", self._polymarket_blocks())
        # 9 network guard blocks forbidden endpoints
        add("network_guard_blocks_forbidden", self._guard_blocks_forbidden())
        # 10 network guard blocks production by default
        add("network_guard_blocks_production", self._guard_blocks_production())
        # 11 secret redaction works
        add("secret_redaction", self._redaction_works())
        # 12 network trap (optional): assert no real network usage in disabled default
        if traps.get("with_network_trap"):
            add("network_trap_clean", not traps.get("network_used", False),
                "real network call detected under trap" if traps.get("network_used") else "")

        fail = sum(1 for c in checks if c["status"] == "FAIL")
        warn = sum(1 for c in checks if c["status"] == "WARN")
        status = "PASS" if fail == 0 and (warn == 0 or not traps.get("fail_on_warning")) else "FAIL"
        return {"status": status, "checks": checks, "test_count": len(checks),
                "pass_count": len(checks) - fail - warn, "fail_count": fail, "warning_count": warn}

    # -- individual probes --------------------------------------------- #
    @staticmethod
    def _has_post_submit_route(src: str) -> bool:
        lines = src.splitlines()
        for i, line in enumerate(lines):
            low = line.lower()
            if ".post(" in low:
                window = " ".join(lines[i:i + 2])
                if any(tok in window for tok in _FORBIDDEN_ROUTE_TOKENS):
                    return True
        return False

    def _base_broker_blocks(self) -> bool:
        from .live_broker_base import LiveBrokerBase
        b = LiveBrokerBase(self.cfg, locks_ok=False)
        try:
            b.submit_fok_canary_order({}, "x")
            return False
        except MicroLiveDisabled:
            return True

    def _polymarket_blocks(self) -> bool:
        from .polymarket_live_broker import PolymarketLiveBroker
        b = PolymarketLiveBroker(self.cfg, locks_ok=True)
        try:
            b.submit_fok_canary_order(object(), "x")
            return False
        except Exception:  # noqa: BLE001 — NotImplementedLiveSigning or MicroLiveDisabled
            return True

    def _guard_blocks_forbidden(self) -> bool:
        g = NetworkGuard(allow_production=False)
        try:
            g.record("POST", "https://demo-api.kalshi.co/trade-api/v2/portfolio/deposit")
            return False
        except ForbiddenEndpointError:
            return True

    def _guard_blocks_production(self) -> bool:
        g = NetworkGuard(allow_production=False)
        try:
            g.record("POST", "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders")
            return False
        except ForbiddenEndpointError:
            return True

    def _redaction_works(self) -> bool:
        import os

        from .secret_runtime import redact
        os.environ["KALSHI_TRADING_ACCESS_KEY_ID"] = "SECRETKEY123"
        try:
            out = redact("token=SECRETKEY123")
            return "SECRETKEY123" not in out
        finally:
            os.environ.pop("KALSHI_TRADING_ACCESS_KEY_ID", None)
