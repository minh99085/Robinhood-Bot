"""Kalshi micro-live broker (Phase 9). Demo by default. Submits exactly one
FOK order, polls order/fills for reconciliation, and supports emergency cancel
only. No batch, no resting GTC/GTD, no amend/replace, no autonomous rebalance,
no private user WebSocket. Trading signer is used only after all locks pass; no
key material is ever logged."""

from __future__ import annotations

from typing import Optional

from .errors import MicroLiveDisabled
from .live_broker_base import LiveBrokerBase

_PROD_BASE = "https://api.elections.kalshi.com"
_DEMO_BASE = "https://demo-api.kalshi.co"
_API = "/trade-api/v2"


class KalshiLiveBroker(LiveBrokerBase):
    venue = "kalshi"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = _PROD_BASE if self.environment == "prod" else _DEMO_BASE

    def _call(self, method: str, path: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
        if self._transport is None:
            raise MicroLiveDisabled(f"kalshi.{method}", "no transport configured (live disabled)")
        url = self.base_url + path
        self.guard.record(method, url)
        headers = {}
        if self._signer is not None:
            headers = self._signer.headers(method, _API + path if not path.startswith(_API) else path)
            self.signer_used = True
        status, body = self._transport(method, url, headers=headers, json_body=json_body)
        return int(status), (body or {})

    def get_account_snapshot(self) -> dict:
        self._require_locks("get_account_snapshot")
        _, body = self._call("GET", _API + "/portfolio/balance")
        return body

    def submit_fok_canary_order(self, payload: dict, client_order_id: str) -> dict:
        self._require_locks("submit_fok_canary_order")
        if self._signer is None:
            raise MicroLiveDisabled("submit_fok_canary_order", "trading signer not loaded")
        if str(payload.get("time_in_force")) != "fill_or_kill":
            raise MicroLiveDisabled("submit_fok_canary_order", "time_in_force must be fill_or_kill")
        body = dict(payload)
        body["client_order_id"] = client_order_id
        body.setdefault("cancel_order_on_pause", True)
        body.setdefault("self_trade_prevention_type", "taker_at_cross")
        status, resp = self._call("POST", _API + "/portfolio/orders", json_body=body)
        return {"status_code": status, "body": resp}

    def get_order(self, order_id: str) -> dict:
        self._require_locks("get_order")
        _, body = self._call("GET", _API + f"/portfolio/orders/{order_id}")
        return body

    def get_open_orders(self, market_ticker: Optional[str] = None) -> dict:
        self._require_locks("get_open_orders")
        path = _API + "/portfolio/orders?status=resting"
        if market_ticker:
            path += f"&ticker={market_ticker}"
        _, body = self._call("GET", path)
        return body

    def get_fills(self, order_id: Optional[str] = None) -> dict:
        self._require_locks("get_fills")
        path = _API + "/portfolio/fills"
        if order_id:
            path += f"?order_id={order_id}"
        _, body = self._call("GET", path)
        return body

    def emergency_cancel_order(self, order_id: str) -> dict:
        self._require_locks("emergency_cancel_order")
        status, body = self._call("DELETE", _API + f"/portfolio/orders/{order_id}")
        return {"status_code": status, "body": body}

    def emergency_cancel_all_for_market(self, market_ticker: str) -> dict:
        self._require_locks("emergency_cancel_all_for_market")
        open_orders = self.get_open_orders(market_ticker).get("orders", [])
        cancelled = []
        for o in open_orders:
            oid = o.get("order_id")
            if oid:
                cancelled.append(self.emergency_cancel_order(oid))
        return {"cancelled_count": len(cancelled)}
