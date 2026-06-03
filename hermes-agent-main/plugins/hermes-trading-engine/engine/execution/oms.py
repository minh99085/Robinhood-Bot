"""OrderManagementSystem — the single gate between a risk-approved proposal and
the PaperBroker. PAPER ONLY: it has no real-exchange client and cannot place,
cancel, or sign anything on a live venue.

Quant scope — *Execution Engine CLOB v2 simulation* + *Risk Management* +
*Compliance*: the OMS remains a MANDATORY gate — it only acts on an APPROVED
:class:`RiskDecision`. Upgraded portfolio/aggressive sizing (fractional Kelly,
Bregman bundle allocation) feeds SMALLER, more-diversified paper orders into this
same gate; it can never be bypassed and hard paper caps always clamp the size.

Lifecycle: CREATED -> (RISK_REJECTED | ACCEPTED) -> broker -> (OPEN |
PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED). Every transition is persisted
to ``orders`` + appended to ``order_events`` and structured-logged. Idempotent on
``client_order_id``. Reconciliation can flag the system degraded, which blocks
new orders.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from .paper_broker import PaperBroker
from .reconciliation import SEV_HIGH, ReconciliationService
from .types import (
    D,
    ExecutionResult,
    OrderAck,
    OrderRejectReason,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    TimeInForce,
    new_client_order_id,
    now_ms,
)

_log = logging.getLogger("hte.oms")
_ALLOWED_MODES = frozenset({"paper", "shadow", "live"})
# terminal statuses that cannot be cancelled
_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED,
                       OrderStatus.RISK_REJECTED, OrderStatus.EXPIRED})


class OrderManagementSystem:
    def __init__(self, store, broker: Optional[PaperBroker] = None, *,
                 mode_provider: Optional[Callable[[], str]] = None,
                 run_id: Optional[str] = None):
        self.store = store
        self.broker = broker or PaperBroker()
        self.mode_provider = mode_provider or (lambda: "paper")
        self.recon = ReconciliationService(store)
        self.run_id = run_id
        self.degraded = False
        self.degraded_reason: Optional[str] = None

    # ================================================================== #
    # logging (never logs secrets — only order metadata)
    # ================================================================== #
    def _emit(self, event: str, order: Optional[OrderRequest] = None, *,
              status: Optional[str] = None, reason: Optional[str] = None, **extra) -> None:
        rec = {"event": event, "run_id": self.run_id}
        if order is not None:
            rec.update({
                "client_order_id": order.client_order_id, "proposal_id": order.proposal_id,
                "venue": order.venue, "market_id": order.market_id, "asset_id": order.asset_id,
                "side": order.side, "limit_price": str(order.limit_price) if order.limit_price is not None else None,
                "quantity": str(order.quantity), "notional": str(order.notional) if order.notional is not None else None,
            })
        if status:
            rec["status"] = status
        if reason:
            rec["reason"] = reason
        rec.update(extra)
        try:
            _log.info(json.dumps(rec, default=str))
        except Exception:  # noqa: BLE001
            pass

    def _event(self, coid: str, event_type: str, payload: dict) -> None:
        try:
            self.store.add_order_event(now_ms(), coid, event_type, payload)
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== #
    # submit
    # ================================================================== #
    def submit(self, order: OrderRequest, risk_decision, *, book=None,
               reference_price=None) -> OrderResult:
        coid = order.client_order_id

        if self.degraded:
            self._emit("paper_broker_reject", order, status=OrderStatus.REJECTED,
                       reason=OrderRejectReason.BROKER_UNAVAILABLE)
            return self._reject_result(order, OrderRejectReason.BROKER_UNAVAILABLE, persist=False)

        # idempotency — duplicate client_order_id never double-books
        existing = self.store.get_order(coid)
        if existing is not None:
            self._emit("order_rejected", order, status=existing.get("status"),
                       reason=OrderRejectReason.DUPLICATE_CLIENT_ORDER_ID)
            fills = self.store.get_fills_for_order(coid)
            ack = OrderAck(coid, accepted=False, status=existing.get("status"),
                           reason=OrderRejectReason.DUPLICATE_CLIENT_ORDER_ID)
            return OrderResult(order, ack, [], existing.get("status"),
                               existing.get("reject_reason"))

        # persist intent — FAIL CLOSED if storage fails
        if not self.store.add_order(self._order_record(order, OrderStatus.CREATED, risk_decision)):
            self._emit("paper_broker_reject", order, status=OrderStatus.REJECTED,
                       reason=OrderRejectReason.BROKER_UNAVAILABLE)
            return self._reject_result(order, OrderRejectReason.BROKER_UNAVAILABLE, persist=False)
        self._event(coid, "order_created", order.record())
        self._emit("order_created", order, status=OrderStatus.CREATED)

        # risk gate (defense in depth — engine also checks before building the order)
        if risk_decision is None or not getattr(risk_decision, "approved", False):
            self._set_status(coid, OrderStatus.RISK_REJECTED, OrderRejectReason.RISK_REJECTED)
            self._event(coid, "order_risk_rejected",
                        {"reasons": list(getattr(risk_decision, "reasons", []) or [])})
            self._emit("order_risk_rejected", order, status=OrderStatus.RISK_REJECTED,
                       reason=OrderRejectReason.RISK_REJECTED)
            return self._result(order, OrderStatus.RISK_REJECTED, [], OrderRejectReason.RISK_REJECTED)

        # mode gate — paper/shadow/live (all simulated; no real adapter exists)
        if self.mode_provider() not in _ALLOWED_MODES:
            self._set_status(coid, OrderStatus.REJECTED, OrderRejectReason.MODE_NOT_ALLOWED)
            self._emit("order_rejected", order, status=OrderStatus.REJECTED,
                       reason=OrderRejectReason.MODE_NOT_ALLOWED)
            return self._result(order, OrderStatus.REJECTED, [], OrderRejectReason.MODE_NOT_ALLOWED)

        self._set_status(coid, OrderStatus.ACCEPTED)
        self._event(coid, "order_accepted", {})
        self._emit("order_accepted", order, status=OrderStatus.ACCEPTED)

        res = self.broker.execute(order, book=book, reference_price=reference_price,
                                  venue_kind=order.venue_kind)
        return self._apply_execution(order, res)

    # ------------------------------------------------------------------ #
    def _apply_execution(self, order: OrderRequest, res: ExecutionResult) -> OrderResult:
        coid = order.client_order_id
        for f in res.fills:
            f.client_order_id = coid
            self.store.add_fill(f.record())
            self._event(coid, "fill", f.record())
        self._set_status(coid, res.status, res.reject_reason)
        if res.fills:
            self._recompute_position(order)

        if res.status == OrderStatus.FILLED:
            self._emit("order_filled", order, status=res.status,
                       filled=str(res.filled_quantity), avg_price=str(res.avg_fill_price))
        elif res.status == OrderStatus.PARTIALLY_FILLED:
            self._emit("order_partially_filled", order, status=res.status,
                       filled=str(res.filled_quantity), remaining=str(res.remaining))
        elif res.status == OrderStatus.OPEN:
            self._emit("order_accepted", order, status=res.status, resting=True)
        elif res.status == OrderStatus.REJECTED:
            self._emit("paper_broker_reject", order, status=res.status, reason=res.reject_reason)
        elif res.status == OrderStatus.CANCELLED:
            self._emit("order_cancelled", order, status=res.status, reason="ioc_unfilled")

        accepted = res.status not in (OrderStatus.REJECTED,)
        ack = OrderAck(coid, accepted=accepted, status=res.status, reason=res.reject_reason)
        return OrderResult(order, ack, res.fills, res.status, res.reject_reason)

    # ================================================================== #
    # cancel / replace
    # ================================================================== #
    def cancel_order(self, client_order_id: str) -> dict:
        o = self.store.get_order(client_order_id)
        if o is None:
            return {"ok": False, "reason": "unknown_order"}
        st = o.get("status")
        if st == OrderStatus.CANCELLED:
            return {"ok": True, "status": st, "already_cancelled": True}
        if st in _TERMINAL:
            return {"ok": False, "status": st, "reason": "cannot_cancel_terminal"}
        self._set_status(client_order_id, OrderStatus.CANCELLED)
        self._event(client_order_id, "order_cancelled", {"prev_status": st})
        self._emit("order_cancelled", None, status=OrderStatus.CANCELLED,
                   client_order_id=client_order_id)
        return {"ok": True, "status": OrderStatus.CANCELLED}

    def cancel_all_for_market(self, market_id: str) -> dict:
        n = 0
        for o in self.store.get_open_orders():
            if o.get("market_id") == market_id:
                if self.cancel_order(o["client_order_id"]).get("ok"):
                    n += 1
        return {"ok": True, "cancelled": n}

    def cancel_all(self) -> dict:
        n = 0
        for o in self.store.get_open_orders():
            if self.cancel_order(o["client_order_id"]).get("ok"):
                n += 1
        return {"ok": True, "cancelled": n}

    def replace_order(self, client_order_id: str, new_limit_price=None,
                      new_quantity=None, risk_decision=None) -> dict:
        o = self.store.get_order(client_order_id)
        if o is None:
            return {"ok": False, "reason": "unknown_order"}
        if o.get("status") not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            return {"ok": False, "reason": "not_replaceable", "status": o.get("status")}
        # cancel the original
        self._set_status(client_order_id, OrderStatus.CANCELLED)
        self._event(client_order_id, "order_replace_requested",
                    {"new_limit_price": str(new_limit_price) if new_limit_price is not None else None,
                     "new_quantity": str(new_quantity) if new_quantity is not None else None})
        self._emit("order_replace_requested", None, status=OrderStatus.CANCELLED,
                   client_order_id=client_order_id)
        # create the linked replacement (resting)
        new = OrderRequest(
            client_order_id=new_client_order_id(),
            venue=o.get("venue", ""), market_id=o.get("market_id", ""),
            asset_id=o.get("asset_id"), outcome=o.get("outcome"), side=o.get("side"),
            order_type=o.get("order_type"),
            limit_price=D(new_limit_price) if new_limit_price is not None else D(o.get("limit_price")),
            quantity=D(new_quantity) if new_quantity is not None else D(o.get("quantity")),
            time_in_force=o.get("time_in_force") or TimeInForce.GTC,
            source=o.get("source", ""), proposal_id=o.get("proposal_id"),
            venue_kind=o.get("venue_kind") or "legacy",
            parent_client_order_id=client_order_id)
        approved = risk_decision is None or getattr(risk_decision, "approved", False)
        status = OrderStatus.OPEN if approved else OrderStatus.RISK_REJECTED
        reason = None if approved else OrderRejectReason.RISK_REJECTED
        self.store.add_order(self._order_record(new, status, risk_decision, reject_reason=reason))
        self._event(new.client_order_id, "order_created",
                    {**new.record(), "replaces": client_order_id})
        self._emit("order_created", new, status=status)
        return {"ok": True, "cancelled": client_order_id,
                "new_client_order_id": new.client_order_id, "status": status}

    # ================================================================== #
    # resting orders + reconciliation
    # ================================================================== #
    def process_resting(self, book_provider: Callable) -> int:
        filled = 0
        for o in self.store.get_open_orders():
            try:
                order = self._order_from_record(o)
                book = book_provider(order)
                res = self.broker.check_resting(order, book)
                if res.fills:
                    self._apply_execution(order, res)
                    filled += 1
            except Exception:  # noqa: BLE001 — resting processing is best-effort
                continue
        return filled

    def reconcile(self, price_provider: Optional[Callable] = None) -> dict:
        report = self.recon.run(price_provider)
        if report.get("severity") == SEV_HIGH:
            self.degraded = True
            self.degraded_reason = "reconciliation_high_severity"
            self._emit("reconciliation_warning", None, severity=SEV_HIGH,
                       warnings=len(report.get("warnings", [])))
        elif report.get("warnings"):
            self._emit("reconciliation_warning", None, severity=report.get("severity"),
                       warnings=len(report.get("warnings", [])))
        return report

    def clear_degraded(self) -> None:
        self.degraded = False
        self.degraded_reason = None

    # ================================================================== #
    # persistence helpers
    # ================================================================== #
    def _order_record(self, order: OrderRequest, status: str, risk_decision,
                      reject_reason: Optional[str] = None) -> dict:
        rec = order.record()
        rec["status"] = status
        rec["reject_reason"] = reject_reason
        rec["risk_decision_json"] = self._risk_json(risk_decision)
        rec["created_ts_ms"] = order.created_ts_ms
        rec["updated_ts_ms"] = now_ms()
        return rec

    @staticmethod
    def _risk_json(risk_decision) -> str:
        if risk_decision is None:
            return "{}"
        try:
            if hasattr(risk_decision, "as_record"):
                return json.dumps(risk_decision.as_record(), default=str)
            if hasattr(risk_decision, "model_dump"):
                return json.dumps(risk_decision.model_dump(), default=str)
        except Exception:  # noqa: BLE001
            pass
        return "{}"

    def _set_status(self, coid: str, status: str, reject_reason: Optional[str] = None) -> None:
        self.store.update_order(coid, status=status, reject_reason=reject_reason,
                                updated_ts_ms=now_ms())

    def _order_from_record(self, o: dict) -> OrderRequest:
        return OrderRequest(
            client_order_id=o.get("client_order_id"), venue=o.get("venue", ""),
            market_id=o.get("market_id", ""), asset_id=o.get("asset_id"),
            outcome=o.get("outcome"), side=o.get("side"), order_type=o.get("order_type"),
            limit_price=D(o.get("limit_price")) if o.get("limit_price") is not None else None,
            quantity=D(o.get("quantity")), time_in_force=o.get("time_in_force"),
            source=o.get("source", ""), proposal_id=o.get("proposal_id"),
            venue_kind=o.get("venue_kind") or "legacy")

    def _recompute_position(self, order: OrderRequest) -> None:
        key = (order.venue, order.market_id, order.asset_id)
        fills = [f for f in self.store.get_fills(limit=100000)
                 if (f.get("venue") or "", f.get("market_id") or "", f.get("asset_id")) == key]
        from .reconciliation import _fold_fills
        qty, avg, realized, fees = _fold_fills(fills)
        self.store.upsert_position({
            "venue": order.venue, "market_id": order.market_id, "asset_id": order.asset_id,
            "outcome": order.outcome, "quantity": str(qty), "avg_price": str(avg),
            "realized_pnl": str(realized), "unrealized_pnl": "0", "fees_paid": str(fees),
            "updated_ts_ms": now_ms()})

    # ------------------------------------------------------------------ #
    def _reject_result(self, order: OrderRequest, reason: str, *, persist: bool) -> OrderResult:
        if persist:
            self._set_status(order.client_order_id, OrderStatus.REJECTED, reason)
        ack = OrderAck(order.client_order_id, accepted=False, status=OrderStatus.REJECTED, reason=reason)
        return OrderResult(order, ack, [], OrderStatus.REJECTED, reason)

    def _result(self, order: OrderRequest, status: str, fills, reason) -> OrderResult:
        ack = OrderAck(order.client_order_id, accepted=status not in (OrderStatus.REJECTED, OrderStatus.RISK_REJECTED),
                       status=status, reason=reason)
        return OrderResult(order, ack, fills, status, reason)

    # ================================================================== #
    # read API
    # ================================================================== #
    def get_orders(self, limit: int = 200) -> list[dict]:
        return self.store.get_orders(limit=limit)

    def get_open_orders(self) -> list[dict]:
        return self.store.get_open_orders()

    def get_recent_orders(self, limit: int = 50) -> list[dict]:
        return self.store.get_recent_orders(limit)

    def get_order(self, client_order_id: str) -> Optional[dict]:
        return self.store.get_order(client_order_id)

    def get_fills(self, limit: int = 200) -> list[dict]:
        return self.store.get_fills(limit=limit)

    def get_positions(self) -> list[dict]:
        return self.store.get_positions()

    def status(self) -> dict:
        opens = self.store.get_open_orders()
        return {
            "degraded": self.degraded, "degraded_reason": self.degraded_reason,
            "open_orders": len(opens), "broker": self.broker.config(),
            "last_reconciliation": self.recon.last_report,
        }

    def bregman_hedge_preflight(self, opp) -> dict:
        """Read-only preflight before a (future) certified Bregman hedge.

        A fully-hedged Bregman set may only be routed when the OMS is healthy
        (not degraded, last reconciliation clean) AND the opportunity is
        certified risk-free. Returns ``{ok, reasons}``; ``ok=False`` means the
        hedge MUST NOT be placed (it stays a logged candidate). Never submits.
        Quant scope — *CLOB v2 Execution* + *Risk Management* + *Compliance*."""
        from .reconciliation import report_is_clean
        reasons: list = []
        if self.degraded:
            reasons.append(f"oms_degraded:{self.degraded_reason}")
        if not report_is_clean(self.recon.last_report):
            reasons.append("reconciliation_not_clean")
        cert = getattr(opp, "certificate", None)
        if not bool(getattr(opp, "certified", False)):
            reasons.append("not_certified")
        elif cert is not None and not bool(getattr(cert, "risk_free", False)):
            reasons.append("certificate_not_risk_free")
        return {"ok": not reasons, "reasons": reasons}

    def readiness_execution_summary(self) -> dict:
        """Execution-realism + reconciliation snapshot for the live-readiness gate
        (CLOB v2 Execution + Compliance). Read-only: reports whether the OMS is
        degraded and whether the last reconciliation was clean — both must be
        healthy before any real-money escalation."""
        from .reconciliation import report_is_clean
        return {
            "degraded": bool(self.degraded),
            "degraded_reason": self.degraded_reason,
            "reconciliation_clean": report_is_clean(self.recon.last_report),
            "reconciliation_severity": (self.recon.last_report or {}).get("severity"),
            # CLOB v2 execution-realism posture (live-readiness validation): a
            # real-money escalation requires realistic + conservative fills.
            "realistic_fills": bool(getattr(self.broker, "realistic", False)),
            "conservative_execution": bool(getattr(self.broker, "conservative", False)),
        }

    def open_capital_lock(self) -> float:
        """Total notional currently locked across open positions (the capital
        the adaptive capital allocator must keep within its open-capital-lock
        ceiling). Read-only — never sizes / places an order."""
        try:
            positions = self.recon.positions() if hasattr(self.recon, "positions") else []
        except Exception:  # noqa: BLE001 — accounting must never break the OMS
            positions = []
        total = 0.0
        for p in (positions or []):
            qty = abs(float(getattr(p, "qty", getattr(p, "quantity", 0.0)) or 0.0))
            px = float(getattr(p, "avg_price", getattr(p, "price", 0.0)) or 0.0)
            total += qty * px
        return round(total, 6)

    def canary_rollback_engaged(self, kill_switch_path: Optional[str] = None) -> bool:
        """True when the canary rollback kill switch file is engaged, so no live
        order may proceed. Checks the file directly (no cross-package import, to
        respect the OMS<->live boundary). Read-only; default disabled. Live
        Trading & Monitoring + Compliance — never enables live execution."""
        import os
        from pathlib import Path
        path = kill_switch_path or os.getenv("CANARY_ROLLBACK_KILL_SWITCH_PATH",
                                             "./CANARY_ROLLBACK_KILL_SWITCH")
        try:
            return bool(path) and Path(path).exists()
        except OSError:
            return False
