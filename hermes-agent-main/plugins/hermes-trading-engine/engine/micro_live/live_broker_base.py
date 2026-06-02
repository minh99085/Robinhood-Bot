"""LiveBrokerBase (Phase 9). Defines the narrow real-execution surface. Base
methods raise MicroLiveDisabled; venue brokers override them. Every method
checks the locks at entry, routes network through the NetworkGuard, and logs
only redacted metadata. A broker is only instantiated by the execution service.

Transport contract: a callable ``transport(method, path, *, headers, json_body)``
returning ``(status_code:int, body:dict)``. When ``transport`` is None, no real
network exists and submission raises MicroLiveDisabled — this keeps real network
out of the default build and out of unit tests (tests inject a fake transport)."""

from __future__ import annotations

from typing import Callable, Optional

from .config import MicroLiveConfig
from .errors import MicroLiveDisabled
from .network_guard import NetworkGuard


class LiveBrokerBase:
    venue = "base"

    def __init__(self, config: MicroLiveConfig, *, locks_ok: bool,
                 network_guard: Optional[NetworkGuard] = None,
                 transport: Optional[Callable] = None, signer=None,
                 environment: str = "demo"):
        self.cfg = config
        self.locks_ok = bool(locks_ok)
        self.guard = network_guard or NetworkGuard(allow_production=config.allow_production)
        self._transport = transport
        self._signer = signer
        self.environment = environment
        self.signer_used = False

    def _require_locks(self, method: str) -> None:
        if not self.locks_ok:
            raise MicroLiveDisabled(method, "locks not open")

    # --- methods all raise unless a venue broker overrides them --------- #
    def preflight(self) -> dict:
        raise MicroLiveDisabled("preflight", "base broker")

    def get_account_snapshot(self) -> dict:
        raise MicroLiveDisabled("get_account_snapshot", "base broker")

    def submit_fok_canary_order(self, payload, client_order_id: str) -> dict:
        raise MicroLiveDisabled("submit_fok_canary_order", "base broker")

    def get_order(self, order_id: str) -> dict:
        raise MicroLiveDisabled("get_order", "base broker")

    def get_open_orders(self, market_ticker: Optional[str] = None) -> dict:
        raise MicroLiveDisabled("get_open_orders", "base broker")

    def get_fills(self, order_id: Optional[str] = None) -> dict:
        raise MicroLiveDisabled("get_fills", "base broker")

    def emergency_cancel_order(self, order_id: str) -> dict:
        raise MicroLiveDisabled("emergency_cancel_order", "base broker")

    def emergency_cancel_all_for_market(self, market_ticker: str) -> dict:
        raise MicroLiveDisabled("emergency_cancel_all_for_market", "base broker")

    def reconcile_order(self, order_id: str) -> dict:
        raise MicroLiveDisabled("reconcile_order", "base broker")

    def health(self) -> dict:
        return {"venue": self.venue, "environment": self.environment,
                "locks_ok": self.locks_ok, "transport": self._transport is not None}
