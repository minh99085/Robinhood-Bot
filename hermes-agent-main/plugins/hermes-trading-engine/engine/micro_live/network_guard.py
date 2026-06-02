"""NetworkGuard (Phase 9). Counts outbound calls and HARD-BLOCKS forbidden
endpoints (deposits/withdrawals/transfers/bridge/allowance/wallet/batch/amend/
replace) and production hosts unless production is explicitly unlocked. Every
broker request goes through ``record()`` before it leaves the process."""

from __future__ import annotations

from .errors import ForbiddenEndpointError

FORBIDDEN_SUBSTR = (
    "deposit", "withdraw", "transfer", "/bridge", "allowance", "/wallet",
    "/batch", "batch_order", "/amend", "/replace", "amend_order", "replace_order",
)
PRODUCTION_HOSTS = ("api.elections.kalshi.com", "trading-api.kalshi.com",
                    "clob.polymarket.com", "api.polymarket.com")


class NetworkGuard:
    def __init__(self, allow_production: bool = False):
        self.allow_production = allow_production
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.forbidden_attempts: list[str] = []

    def record(self, method: str, url: str) -> None:
        self.calls.append((method, url))
        low = url.lower()
        for f in FORBIDDEN_SUBSTR:
            if f in low:
                self.forbidden_attempts.append(url)
                raise ForbiddenEndpointError(f"forbidden endpoint pattern {f!r}")
        if (not self.allow_production) and any(h in low for h in PRODUCTION_HOSTS):
            self.forbidden_attempts.append(url)
            raise ForbiddenEndpointError("production endpoint blocked (MICRO_LIVE_ALLOW_PRODUCTION=0)")
        svc = ("create_order" if (method.upper() == "POST" and "/orders" in low)
               else ("cancel" if method.upper() == "DELETE" else "read"))
        self.counts[svc] = self.counts.get(svc, 0) + 1

    def count(self, svc: str) -> int:
        return self.counts.get(svc, 0)

    def total(self) -> int:
        return len(self.calls)
