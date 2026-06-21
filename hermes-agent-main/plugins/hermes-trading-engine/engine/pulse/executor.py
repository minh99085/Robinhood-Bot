"""Paper executor + ledger for BTC 5-min pulse positions.

HARD SAFETY INVARIANT: every fill here is SIMULATED. This module holds NO order client,
NO wallet, NO signing — it can only record hypothetical positions and resolve them for
paper P&L. There is intentionally no code path that contacts an exchange to place an order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

PAPER_ONLY = True          # structural assertion: this engine never places a real order


@dataclass
class PulsePosition:
    window_key: str
    market_id: str
    title: str
    side: str                       # "up" | "down"
    token_id: str
    entry_price: float
    size_usd: float
    shares: float
    fair_at_entry: float
    edge_at_entry: float
    open_ts: float
    close_ts: float
    entry_ts: float
    status: str = "open"            # "open" | "settled"
    outcome_up: Optional[bool] = None
    won: Optional[bool] = None
    pnl_usd: Optional[float] = None
    s_open: Optional[float] = None
    s_close: Optional[float] = None

    _FIELDS = ("window_key", "market_id", "title", "side", "token_id", "entry_price",
               "size_usd", "shares", "fair_at_entry", "edge_at_entry", "open_ts", "close_ts",
               "entry_ts", "status", "outcome_up", "won", "pnl_usd", "s_open", "s_close")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "PulsePosition":
        return cls(**{k: d.get(k) for k in cls._FIELDS})


class PulseLedger:
    """In-memory paper ledger (persisted as JSON by the engine). One position per window."""

    def __init__(self):
        self.positions: dict = {}            # window_key -> PulsePosition
        self.realized_pnl: float = 0.0
        self.trades: int = 0
        self.wins: int = 0
        self.settled: int = 0
        # running profit accumulators (survive pruning + restarts)
        self.settled_entry_sum: float = 0.0          # sum of entry prices of settled trades
        self.side_n: dict = {"up": 0, "down": 0}
        self.side_wins: dict = {"up": 0, "down": 0}
        # how each settled trade was resolved (authoritative Polymarket vs Coinbase proxy) —
        # proxy wins share the entry feed so a high proxy share means win-rate is optimistic.
        self.settle_sources: dict = {"polymarket": 0, "proxy_coinbase": 0}

    def has_position(self, window_key: str) -> bool:
        return window_key in self.positions

    def open_position(self, window, decision, now: float, *, size_usd: float,
                      s_open: Optional[float] = None) -> Optional[PulsePosition]:
        """Record a SIMULATED paper fill at the decision's marketable ask. Never real."""
        if not decision.trade or decision.token_id is None or not decision.price:
            return None
        if self.has_position(window.event_id):
            return None
        price = float(decision.price)
        if price <= 0 or price >= 1:
            return None
        shares = round(float(size_usd) / price, 6)
        pos = PulsePosition(
            window_key=window.event_id, market_id=window.market_id, title=window.title,
            side=decision.side, token_id=decision.token_id, entry_price=price,
            size_usd=float(size_usd), shares=shares,
            fair_at_entry=float(decision.fair_p_up or 0.0),
            edge_at_entry=float(decision.edge), open_ts=window.open_ts,
            close_ts=window.close_ts, entry_ts=float(now), s_open=s_open)
        self.positions[window.event_id] = pos
        self.trades += 1
        return pos

    def settle(self, window_key: str, outcome_up: bool, *,
               s_open: Optional[float] = None, s_close: Optional[float] = None,
               source: Optional[str] = None) -> Optional[PulsePosition]:
        pos = self.positions.get(window_key)
        if pos is None or pos.status == "settled":
            return None
        won = (pos.side == "up" and outcome_up) or (pos.side == "down" and not outcome_up)
        payoff = pos.shares if won else 0.0
        pos.pnl_usd = round(payoff - pos.size_usd, 6)
        pos.won = bool(won)
        pos.outcome_up = bool(outcome_up)
        pos.status = "settled"
        if s_open is not None:
            pos.s_open = s_open
        if s_close is not None:
            pos.s_close = s_close
        self.realized_pnl = round(self.realized_pnl + pos.pnl_usd, 6)
        self.settled += 1
        self.settled_entry_sum += pos.entry_price
        if source in self.settle_sources:
            self.settle_sources[source] += 1
        if pos.side in self.side_n:
            self.side_n[pos.side] += 1
            if won:
                self.side_wins[pos.side] += 1
        if won:
            self.wins += 1
        return pos

    def open_positions(self) -> list:
        return [p for p in self.positions.values() if p.status == "open"]

    def _side_win_rate(self, side: str) -> "float | None":
        n = self.side_n.get(side, 0)
        return round(self.side_wins.get(side, 0) / n, 4) if n else None

    def stats(self) -> dict:
        win_rate = (self.wins / self.settled) if self.settled else None
        avg_entry = (self.settled_entry_sum / self.settled) if self.settled else None
        # edge_realized = how much more often we win than the price we paid implied. >0 means
        # the paper book of trades is profitable in expectation (the headline profit signal).
        edge_realized = (win_rate - avg_entry) if (win_rate is not None
                                                   and avg_entry is not None) else None
        return {"trades": self.trades, "settled": self.settled, "wins": self.wins,
                "win_rate": (round(win_rate, 4) if win_rate is not None else None),
                "avg_entry_price": (round(avg_entry, 4) if avg_entry is not None else None),
                "edge_realized": (round(edge_realized, 4) if edge_realized is not None else None),
                "win_rate_up": self._side_win_rate("up"),
                "win_rate_down": self._side_win_rate("down"),
                "side_counts": dict(self.side_n),
                "settle_sources": dict(self.settle_sources),
                "realized_pnl_usd": round(self.realized_pnl, 4),
                "avg_pnl_per_trade": (round(self.realized_pnl / self.settled, 4)
                                      if self.settled else None),
                "open_positions": len(self.open_positions())}

    def to_dict(self, *, max_positions: int = 200) -> dict:
        recent = sorted(self.positions.values(), key=lambda p: p.entry_ts, reverse=True)
        return {"paper_only": True, "stats": self.stats(),
                "accumulators": {"settled_entry_sum": round(self.settled_entry_sum, 6),
                                 "side_n": dict(self.side_n),
                                 "side_wins": dict(self.side_wins),
                                 "settle_sources": dict(self.settle_sources)},
                "positions": [p.to_dict() for p in recent[:max_positions]]}

    def load_state(self, data: dict) -> None:
        """Restore counters + positions from a persisted ``to_dict()`` so paper P&L survives
        restarts. Counters come from the saved stats (authoritative even after old positions
        were pruned); position records are rebuilt for the retained recent window."""
        stats = (data or {}).get("stats") or {}
        self.trades = int(stats.get("trades", 0) or 0)
        self.settled = int(stats.get("settled", 0) or 0)
        self.wins = int(stats.get("wins", 0) or 0)
        self.realized_pnl = round(float(stats.get("realized_pnl_usd", 0.0) or 0.0), 6)
        acc = (data or {}).get("accumulators") or {}
        self.settled_entry_sum = float(acc.get("settled_entry_sum", 0.0) or 0.0)
        for k in ("up", "down"):
            self.side_n[k] = int((acc.get("side_n") or {}).get(k, 0) or 0)
            self.side_wins[k] = int((acc.get("side_wins") or {}).get(k, 0) or 0)
        for k in ("polymarket", "proxy_coinbase"):
            self.settle_sources[k] = int((acc.get("settle_sources") or {}).get(k, 0) or 0)
        for pd in (data.get("positions") or []):
            try:
                pos = PulsePosition.from_dict(pd)
            except Exception:  # noqa: BLE001 — a bad record never blocks startup
                continue
            if pos.window_key:
                self.positions[pos.window_key] = pos
