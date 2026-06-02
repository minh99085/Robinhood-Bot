"""ExchangeGateway — PAPER order placement (simulated fills) + balances.

place_order simulates a MARKET fill at the venue's live bid/ask, with adverse
slippage and the venue taker fee. PAPER ONLY: no real order is sent. A live
gateway would implement signed REST/WebSocket order placement behind the
existing triple safeguard. Leg-2 failure can be forced (testing) or injected via
ARB_LEG2_FAIL_PROB so the recovery path is exercised.
"""

from __future__ import annotations

import os
import random
import time

from .detector import SLIPPAGE, TAKER
from .symbol_map import EXCHANGES


class ExchangeGateway:
    def __init__(self, feeds, mapper, paper: bool = True):
        self.feeds = feeds
        self.mapper = mapper
        self.paper = paper
        seed = float(os.getenv("ARB_PAPER_BALANCE_PER_EX", "2000"))
        self.balances = {ex: {"USD": seed} for ex in EXCHANGES}
        self.leg2_fail_prob = float(os.getenv("ARB_LEG2_FAIL_PROB", "0"))

    def get_balance(self, exchange: str) -> dict:
        return self.balances.get(exchange, {"USD": 0.0})

    def _price(self, exchange: str, symbol: str, side: str) -> float | None:
        t = self.feeds.get_latest_tick(exchange, symbol)
        if not t:
            return None
        return t["ask"] if side == "BUY" else t["bid"]

    def place_order(self, exchange: str, symbol: str, side: str, *,
                    usd: float | None = None, qty: float | None = None,
                    force_fail: bool = False) -> dict:
        t0 = time.time()
        if not self.paper:
            return {"ok": False, "error": "no live execution adapter connected"}
        if force_fail or (self.leg2_fail_prob > 0 and random.random() < self.leg2_fail_prob):
            return {"ok": False, "error": "fill failed", "fillTime_ms": round((time.time() - t0) * 1000, 1)}
        px = self._price(exchange, symbol, side)
        if px is None:
            return {"ok": False, "error": "no tick"}
        fee_rate = TAKER.get(exchange, 0.005)
        if side == "BUY":
            fill_price = px * (1 + SLIPPAGE)
            spend = float(usd or 0.0)
            fee = spend * fee_rate
            fill_qty = max(0.0, (spend - fee) / fill_price)
            self.balances.setdefault(exchange, {"USD": 0.0})["USD"] -= spend
        else:  # SELL
            fill_price = px * (1 - SLIPPAGE)
            q = float(qty or 0.0)
            proceeds = q * fill_price
            fee = proceeds * fee_rate
            self.balances.setdefault(exchange, {"USD": 0.0})["USD"] += proceeds - fee
            fill_qty = q
        return {"ok": True, "exchange": exchange, "side": side, "fillPrice": round(fill_price, 6),
                "fillQty": round(fill_qty, 8), "fee": round(fee, 4),
                "fillTime_ms": round((time.time() - t0) * 1000 + random.uniform(20, 120), 1)}
