"""Baseline strategies for honest evaluation.

Compares the edge strategy against:
  * do_nothing            — never trades (the null baseline)
  * market_midpoint       — treats p_market as fair value (≈ zero alpha)
  * naive_price_extreme   — the OLD rule: buy YES if yes_price>=0.65, buy NO if <=0.35
  * current_strategy      — the v2 net-edge model

``observe()`` is called for every evaluated candidate (trade COUNTS). ``settle()``
is called when a strategy position closes with a known realized settle value
(per-baseline PnL on that opportunity). The report flags FIX_STRATEGY if
current_strategy does not beat the baselines on realized PnL.
"""

from __future__ import annotations

from dataclasses import dataclass

YES_EXTREME_HI = 0.65
YES_EXTREME_LO = 0.35


@dataclass
class _Acc:
    trade_count: int = 0
    pnl: float = 0.0
    wins: int = 0
    scored: int = 0
    _equity: float = 0.0
    drawdown: float = 0.0

    def add_count(self, traded: bool):
        if traded:
            self.trade_count += 1

    def add_pnl(self, pnl: float, win: bool):
        self.scored += 1
        self.pnl = round(self.pnl + float(pnl), 6)
        self.wins += 1 if win else 0
        self._equity += float(pnl)
        self.drawdown = round(min(self.drawdown, self._equity), 6)

    def result(self, name: str) -> dict:
        return {"baseline_name": name, "trade_count": self.trade_count,
                "scored_trades": self.scored, "pnl": round(self.pnl, 6),
                "win_rate": round(self.wins / self.scored, 4) if self.scored else None,
                "drawdown": self.drawdown}


def _yes_pnl(price, realized):
    return float(realized) - float(price)


def _no_pnl(no_price, realized):
    return (1.0 - float(realized)) - float(no_price)


class BaselineComparator:
    NAMES = ("do_nothing", "market_midpoint", "naive_price_extreme", "current_strategy")

    def __init__(self):
        self.b = {k: _Acc() for k in self.NAMES}

    @staticmethod
    def _midpoint_traded(p_market, executable_price, min_net_edge):
        return (p_market - executable_price) > min_net_edge

    @staticmethod
    def _naive_traded(yes_price):
        return yes_price >= YES_EXTREME_HI or yes_price <= YES_EXTREME_LO

    def observe(self, *, yes_price, executable_price, p_market, min_net_edge, traded):
        self.b["do_nothing"].add_count(False)
        self.b["market_midpoint"].add_count(
            self._midpoint_traded(p_market, executable_price, min_net_edge))
        self.b["naive_price_extreme"].add_count(self._naive_traded(yes_price))
        self.b["current_strategy"].add_count(bool(traded))

    def settle(self, *, yes_price, executable_price, p_market, realized, min_net_edge,
               strategy_pnl, strategy_win):
        if realized is None:
            self.b["current_strategy"].add_pnl(strategy_pnl or 0.0, strategy_win)
            return
        if self._midpoint_traded(p_market, executable_price, min_net_edge):
            p = _yes_pnl(executable_price, realized)
            self.b["market_midpoint"].add_pnl(p, p > 0)
        if yes_price >= YES_EXTREME_HI:
            p = _yes_pnl(executable_price, realized)
            self.b["naive_price_extreme"].add_pnl(p, p > 0)
        elif yes_price <= YES_EXTREME_LO:
            p = _no_pnl(1.0 - yes_price, realized)
            self.b["naive_price_extreme"].add_pnl(p, p > 0)
        self.b["current_strategy"].add_pnl(strategy_pnl or 0.0, strategy_win)

    def results(self) -> list:
        return [self.b[k].result(k) for k in self.NAMES]

    def beats_baselines(self) -> bool:
        res = {r["baseline_name"]: r for r in self.results()}
        cur = res["current_strategy"]["pnl"]
        others = max(res["do_nothing"]["pnl"], res["market_midpoint"]["pnl"],
                     res["naive_price_extreme"]["pnl"])
        return cur >= others
