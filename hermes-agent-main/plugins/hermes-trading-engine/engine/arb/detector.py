"""ArbitrageDetector — scan exchanges, compute net spread, surface opportunities.

For each active symbol it reads bid/ask on every venue, finds the cheapest ask
(buy there) and richest bid (sell elsewhere), and computes the spread net of each
venue's taker fee. Real cross-exchange spreads on liquid coins are almost always
negative after fees — that honest result is the point. ARB_SIMULATE_OPPS injects
a synthetic positive opportunity so the execution pipeline can be exercised.
"""

from __future__ import annotations

import os
import random
import time

from .symbol_map import EXCHANGES

# per-venue taker fee (fraction), overridable via env
TAKER = {
    "coinbase": float(os.getenv("ARB_FEE_COINBASE", "0.006")),
    "kraken": float(os.getenv("ARB_FEE_KRAKEN", "0.0026")),
    "bitstamp": float(os.getenv("ARB_FEE_BITSTAMP", "0.004")),
}
SLIPPAGE = float(os.getenv("ARB_SLIPPAGE", "0.0005"))
DETECT_THRESHOLD_PCT = float(os.getenv("ARB_DETECT_NET_PCT", "0.15"))


def _parse_sim_prob(raw) -> float:
    """ARB_SIMULATE_OPPS is a probability in [0.0, 1.0].

    Per-scan chance of injecting one synthetic opportunity when no real one
    exists. Legacy boolean-ish values map sensibly: "1"/"true"/"on" -> 1.0,
    ""/"0"/"false"/"off" -> 0.0. Out-of-range / unparseable -> 0.0 (safe-off).
    """
    s = str(raw if raw is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on"):
        return 1.0
    if s in ("", "0", "false", "no", "off"):
        return 0.0
    try:
        v = float(s)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, v))


def _tier(net_pct: float) -> str:
    if net_pct >= 0.5:
        return "A"
    if net_pct >= 0.25:
        return "B"
    return "C"


class ArbitrageDetector:
    def __init__(self, feeds, mapper, universe):
        self.feeds = feeds
        self.mapper = mapper
        self.universe = universe
        # Probability in [0,1] of injecting a synthetic opportunity per scan.
        self.simulate_prob = _parse_sim_prob(os.getenv("ARB_SIMULATE_OPPS", "0"))
        self.simulate = self.simulate_prob > 0.0

    def _opp_for_symbol(self, symbol: str) -> dict | None:
        quotes = {}
        for ex in EXCHANGES:
            t = self.feeds.get_latest_tick(ex, symbol)
            if t:
                quotes[ex] = t
        if len(quotes) < 2:
            return None
        buy_ex = min(quotes, key=lambda e: quotes[e]["ask"])
        sell_ex = max(quotes, key=lambda e: quotes[e]["bid"])
        if buy_ex == sell_ex:
            return None
        buy_ask = quotes[buy_ex]["ask"]
        sell_bid = quotes[sell_ex]["bid"]
        gross_pct = (sell_bid - buy_ask) / buy_ask * 100.0
        net_pct = gross_pct - 100.0 * (TAKER[buy_ex] + TAKER[sell_ex])
        exec_net_pct = net_pct - 100.0 * (2 * SLIPPAGE)
        staleness_ms = (time.time() - min(quotes[buy_ex]["ts"], quotes[sell_ex]["ts"])) * 1000.0
        return {
            "symbol": symbol, "buyExchange": buy_ex, "buyAsk": round(buy_ask, 6),
            "sellExchange": sell_ex, "sellBid": round(sell_bid, 6),
            "grossPct": round(gross_pct, 4), "netPct": round(net_pct, 4),
            "executionNetPct": round(exec_net_pct, 4),
            "estimatedProfit_1k": round(net_pct / 100.0 * 1000.0, 2),
            "tier": _tier(net_pct), "staleness_ms": round(staleness_ms, 1),
            "simulated": False,
        }

    def _synthetic(self) -> dict:
        sym = random.choice(self.universe.active_symbols() or ["SOL"])
        px = self.universe.price(sym) or 100.0
        net = random.uniform(0.28, 0.9)  # synthetic positive net %
        gross = net + 100.0 * (TAKER["coinbase"] + TAKER["kraken"])
        buy_ask = px
        sell_bid = px * (1 + gross / 100.0)
        return {
            "symbol": sym, "buyExchange": "kraken", "buyAsk": round(buy_ask, 6),
            "sellExchange": "coinbase", "sellBid": round(sell_bid, 6),
            "grossPct": round(gross, 4), "netPct": round(net, 4),
            "executionNetPct": round(net - 0.1, 4),
            "estimatedProfit_1k": round(net / 100.0 * 1000.0, 2),
            "tier": _tier(net), "staleness_ms": round(random.uniform(50, 400), 1),
            "simulated": True,
        }

    def scan(self) -> list[dict]:
        opps = []
        for sym in self.universe.active_symbols():
            o = self._opp_for_symbol(sym)
            if o and o["netPct"] > DETECT_THRESHOLD_PCT:
                opps.append(o)
        if self.simulate and not opps and random.random() < self.simulate_prob:
            opps.append(self._synthetic())
        opps.sort(key=lambda o: -o["netPct"])
        return opps
