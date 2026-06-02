"""Execution layer (Phase 3): internal OMS + simulated PaperBroker.

PAPER ONLY. No real order submission, no wallet/private-key signing, no live
broker adapter. Every order must be RiskEngine-approved before it reaches the
OMS, and every fill is auditable back to its order + risk decision.
"""

from .fees import FeeModel
from .oms import OrderManagementSystem
from .paper_broker import PaperBroker
from .reconciliation import ReconciliationService
from .slippage import SlippageModel
from .types import (
    ExecutionResult,
    Fill,
    LiquidityFlag,
    OrderAck,
    OrderRejectReason,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TimeInForce,
    new_client_order_id,
)

__all__ = [
    "OrderManagementSystem", "PaperBroker", "FeeModel", "SlippageModel",
    "ReconciliationService", "OrderRequest", "OrderAck", "Fill", "Position",
    "OrderResult", "ExecutionResult", "OrderSide", "OrderType", "TimeInForce",
    "OrderStatus", "LiquidityFlag", "OrderRejectReason", "new_client_order_id",
]
