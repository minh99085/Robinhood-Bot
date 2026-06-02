"""Execution modules import cleanly and expose the expected surface."""

from __future__ import annotations

import importlib


def test_compile_and_import_execution_modules():
    for mod in ("engine.execution.types", "engine.execution.fees",
                "engine.execution.slippage", "engine.execution.paper_broker",
                "engine.execution.oms", "engine.execution.reconciliation",
                "engine.execution"):
        importlib.import_module(mod)

    from engine.execution import (
        FeeModel,
        OrderManagementSystem,
        OrderRequest,
        PaperBroker,
        ReconciliationService,
        SlippageModel,
    )

    assert OrderManagementSystem is not None
    assert PaperBroker is not None
    assert FeeModel is not None
    assert SlippageModel is not None
    assert ReconciliationService is not None
    # OrderRequest computes notional from price * quantity
    from decimal import Decimal
    o = OrderRequest(venue="v", market_id="m", side="BUY", limit_price=Decimal("0.5"),
                     quantity=Decimal("10"))
    assert o.notional == Decimal("5.0")
    assert o.client_order_id  # auto-generated
