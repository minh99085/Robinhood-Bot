"""DryRunLiveBroker (Phase 8).

Produces an UNSIGNED / UNSENT DryRunOrderIntent from an internal order. It never
signs, never submits, never cancels, and never calls a venue order endpoint or
the network. submit/cancel/replace raise LiveExecutionDisabled. A dry-run intent
requires both a RiskEngine decision id and a SafetyEnvelope decision id.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .errors import LiveExecutionDisabled
from .schemas import DryRunOrderIntent
from .venue_mappers import get_mapper


def _d(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


class DryRunLiveBroker:
    def __init__(self, store=None, config=None):
        self.store = store
        self.config = config

    def validate_order(self, order: dict, *, risk_decision_id: Optional[str] = None,
                       safety_envelope_decision_id: Optional[str] = None,
                       now_ms: Optional[int] = None) -> DryRunOrderIntent:
        venue = order.get("venue", "polymarket")
        intent = DryRunOrderIntent(
            venue=venue, market_id=order.get("market_id"),
            market_ticker=order.get("market_ticker"), asset_id=order.get("asset_id"),
            outcome=order.get("outcome", "YES"), side=str(order.get("side", "BUY")).upper(),
            order_type=str(order.get("order_type", "LIMIT")).upper(),
            limit_price=_d(order.get("price") or order.get("limit_price")),
            quantity=_d(order.get("quantity")), notional=_d(order.get("notional")),
            internal_order_request=dict(order),
            risk_decision_id=risk_decision_id,
            safety_envelope_decision_id=safety_envelope_decision_id,
            # invariants: never signed, never sent, never networked
            unsigned=True, unsent=True, signer_used=False, network_called=False)
        if not risk_decision_id:
            intent.status, intent.reason = "BLOCKED", "missing_risk_decision"
        elif not safety_envelope_decision_id:
            intent.status, intent.reason = "BLOCKED", "missing_safety_envelope_decision"
        else:
            payload, errors = get_mapper(venue)(order)
            intent.venue_payload = payload
            if errors:
                intent.status, intent.reason = "REJECTED", "; ".join(errors)
            else:
                intent.status = "VALIDATED"
        if self.store is not None:
            try:
                self.store.add_dry_run_order_intent(intent.record())
            except Exception:  # noqa: BLE001
                intent.status, intent.reason = "ERROR", "storage_failure"
        return intent

    # alias
    create_dry_run_intent = validate_order

    # --- execution remains locked -------------------------------------- #
    def submit_order(self, *a, **k):
        raise LiveExecutionDisabled("submit_order", "DryRunLiveBroker is dry-run only")

    def cancel_order(self, *a, **k):
        raise LiveExecutionDisabled("cancel_order", "DryRunLiveBroker is dry-run only")

    def replace_order(self, *a, **k):
        raise LiveExecutionDisabled("replace_order", "DryRunLiveBroker is dry-run only")
