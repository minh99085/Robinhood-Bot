"""Within-window risk-free arbitrage (dutch book) detector for the BTC up/down 5-min market.

Roan's roadmap: for a 2-OUTCOME window the ONLY real risk-free edge is the within-window dutch book
-- buy 1 `up` share + 1 `down` share for less than $1 and collect exactly $1 at settlement (exactly
one side resolves to $1). No prediction, no view, no model. The heavy combinatorial machinery
(Bregman/Frank-Wolfe/IP/Gurobi) is for multi-condition markets and is intentionally OUT OF SCOPE:
the marginal polytope of a 2-outcome window is trivial and arbitrage here is just
``vwap_up + vwap_down < 1``.

PAPER ONLY: this simulates fills over the live ask ladders (reusing ``execution_gate.vwap_fill``);
it never places a real order. P&L is GUARANTEED and is kept in a SEPARATE ledger so it is never
blended with the directional strategy's win-rate / profit-factor stats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.pulse.execution_gate import vwap_fill, _on_tick


@dataclass
class ArbOpportunity:
    """A detected within-window dutch book. ``kind`` is 'buy_both' (executable) or 'sell_both'
    (detect/log only — the long-only paper ledger cannot short)."""
    kind: str
    up_vwap: float
    down_vwap: float
    shares: float                 # matched shares per leg (1 up + 1 down -> $1)
    cost_usd: float               # total paid for both legs
    guaranteed_profit_usd: float  # shares*1 - cost (>=0 by construction when actionable)
    ask_sum: float                # vwap_up + vwap_down
    tob_residual: float           # best_up_ask + best_down_ask - 1 (top-of-book simplex residual)
    vwap_residual: float          # vwap_up + vwap_down - 1 (executable simplex residual)
    depth_capped: bool = False
    actionable: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "up_vwap": round(self.up_vwap, 6),
                "down_vwap": round(self.down_vwap, 6), "shares": round(self.shares, 4),
                "cost_usd": round(self.cost_usd, 4),
                "guaranteed_profit_usd": round(self.guaranteed_profit_usd, 4),
                "ask_sum": round(self.ask_sum, 6), "tob_residual": round(self.tob_residual, 6),
                "vwap_residual": round(self.vwap_residual, 6), "depth_capped": self.depth_capped,
                "actionable": self.actionable, "reason": self.reason}


def detect_arbitrage(up_book, down_book, *, size_usd: float = 5.0, fees: float = 0.0,
                     epsilon: float = 0.05, max_depth_consume_frac: float = 0.5,
                     tick_size: float = 0.01, now: Optional[float] = None,
                     max_book_age_s: float = 30.0,
                     min_profit_usd: float = 0.0) -> Optional[ArbOpportunity]:
    """Detect a risk-free within-window dutch book across the up/down ask ladders.

    BUY-both is actionable when ``vwap_up + vwap_down < 1 - fees - epsilon`` and both legs fully fill
    within depth (capped at ``max_depth_consume_frac`` of each side). SELL-both (bids sum
    > 1 + fees + epsilon) is detected but NOT actionable (no paper short). Returns the best
    opportunity or None. Reuses the strict execution-gate VWAP/depth math (no top-of-book fantasy)."""
    if up_book is None or down_book is None:
        return None
    up_asks = getattr(up_book, "asks", None) or []
    dn_asks = getattr(down_book, "asks", None) or []
    best_up = getattr(up_book, "best_ask", None)
    best_dn = getattr(down_book, "best_ask", None)
    buy = None
    # ---- BUY-BOTH (the executable risk-free leg) ----
    if up_asks and dn_asks and best_up and best_dn:
        # stale-book guard (same realism as the execution gate)
        ts_u, ts_d = getattr(up_book, "ts", 0), getattr(down_book, "ts", 0)
        stale = (now is not None and max_book_age_s > 0
                 and ((ts_u and now - float(ts_u) > max_book_age_s)
                      or (ts_d and now - float(ts_d) > max_book_age_s)))
        # cap notional to a fraction of the thinner side's depth so we don't move the book
        up_depth = float(getattr(up_book, "ask_depth_usd", 0.0) or 0.0)
        dn_depth = float(getattr(down_book, "ask_depth_usd", 0.0) or 0.0)
        cap_notional = max_depth_consume_frac * min(up_depth, dn_depth)
        target = min(float(size_usd), cap_notional) if cap_notional > 0 else float(size_usd)
        depth_capped = bool(cap_notional > 0 and target < float(size_usd))
        vwu, spent_u, sh_u, full_u = vwap_fill(up_asks, target)
        vwd, spent_d, sh_d, full_d = vwap_fill(dn_asks, target)
        tob_res = (float(best_up) + float(best_dn) - 1.0)
        if vwu is not None and vwd is not None:
            vwap_res = (vwu + vwd - 1.0)
            shares = min(sh_u, sh_d)            # matched pairs: 1 up + 1 down -> guaranteed $1
            cost = shares * (vwu + vwd)
            profit = shares * (1.0 - (vwu + vwd))
            ok_tick = _on_tick(float(best_up), tick_size) and _on_tick(float(best_dn), tick_size)
            actionable = bool(full_u and full_d and not stale and ok_tick and shares > 0
                              and (vwu + vwd) < (1.0 - fees - epsilon)
                              and profit >= min_profit_usd)
            reason = ("ok" if actionable else
                      ("stale_book" if stale else
                       ("partial_fill" if not (full_u and full_d) else
                        ("below_epsilon" if (vwu + vwd) >= (1.0 - fees - epsilon) else
                         ("tick" if not ok_tick else "no_edge")))))
            buy = ArbOpportunity(
                kind="buy_both", up_vwap=vwu, down_vwap=vwd, shares=shares, cost_usd=cost,
                guaranteed_profit_usd=profit, ask_sum=(vwu + vwd), tob_residual=tob_res,
                vwap_residual=vwap_res, depth_capped=depth_capped, actionable=actionable,
                reason=reason)
            if buy.actionable:
                return buy                       # the executable risk-free dutch book wins
    # ---- SELL-BOTH (detect/log only; no paper short) ----
    bbu, bbd = getattr(up_book, "best_bid", None), getattr(down_book, "best_bid", None)
    if bbu and bbd and (float(bbu) + float(bbd)) > (1.0 + fees + epsilon):
        return ArbOpportunity(
            kind="sell_both", up_vwap=0.0, down_vwap=0.0, shares=0.0, cost_usd=0.0,
            guaranteed_profit_usd=0.0, ask_sum=0.0,
            tob_residual=((float(best_up) + float(best_dn) - 1.0) if (best_up and best_dn) else 0.0),
            vwap_residual=(float(bbu) + float(bbd) - 1.0), depth_capped=False,
            actionable=False, reason="sell_both_detected_no_paper_short")
    return buy                                    # non-actionable buy-both (residual diagnostics) or None


class ArbLedger:
    """SEPARATE paper ledger for risk-free arbitrage fills — kept apart from the directional ledger
    so the two strategies are NEVER blended in win-rate / profit-factor stats. P&L is deterministic."""

    def __init__(self):
        self.positions: dict = {}     # window_key -> open arb position dict
        self.detected = 0             # actionable buy-both opportunities seen
        self.sell_both_detected = 0   # log-only
        self.executed = 0
        self.settled = 0
        self.realized_profit_usd = 0.0
        self.guaranteed_booked_usd = 0.0   # sum of guaranteed profit at book time

    def has_arb(self, window_key: str) -> bool:
        return window_key in self.positions

    def book(self, window_key: str, opp: ArbOpportunity, *, close_ts: float, now: float) -> bool:
        if not opp.actionable or window_key in self.positions:
            return False
        self.positions[window_key] = {
            "window_key": window_key, "kind": opp.kind, "shares": opp.shares,
            "cost_usd": opp.cost_usd, "guaranteed_profit_usd": opp.guaranteed_profit_usd,
            "up_vwap": opp.up_vwap, "down_vwap": opp.down_vwap, "entry_ts": float(now),
            "close_ts": float(close_ts), "status": "open", "entry_mode": "arbitrage"}
        self.executed += 1
        self.guaranteed_booked_usd = round(self.guaranteed_booked_usd + opp.guaranteed_profit_usd, 6)
        return True

    def settle_due(self, now: float) -> int:
        """Settle arb positions at/after close. P&L is the GUARANTEED profit, independent of outcome
        (one leg pays $1, the other $0; total payout = shares*$1)."""
        n = 0
        for wk, p in list(self.positions.items()):
            if p["status"] == "open" and now >= p["close_ts"]:
                p["status"] = "settled"
                self.realized_profit_usd = round(self.realized_profit_usd
                                                 + p["guaranteed_profit_usd"], 6)
                self.settled += 1
                n += 1
        return n

    def open_positions(self) -> list:
        return [p for p in self.positions.values() if p["status"] == "open"]

    def report(self) -> dict:
        return {"strategy": "within_window_arbitrage", "paper_only": True, "risk_free": True,
                "segregated_from_directional": True, "detected_actionable": self.detected,
                "sell_both_detected": self.sell_both_detected, "executed": self.executed,
                "settled": self.settled, "open": len(self.open_positions()),
                "realized_profit_usd": round(self.realized_profit_usd, 4),
                "guaranteed_booked_usd": round(self.guaranteed_booked_usd, 4),
                "note": ("risk-free dutch book (up_vwap+down_vwap<1-fees-eps); deterministic P&L; "
                         "NEVER blended into directional win-rate/profit-factor. PAPER ONLY.")}

    def to_state(self) -> dict:
        return {"positions": {k: dict(v) for k, v in self.positions.items()},
                "detected": self.detected, "sell_both_detected": self.sell_both_detected,
                "executed": self.executed, "settled": self.settled,
                "realized_profit_usd": self.realized_profit_usd,
                "guaranteed_booked_usd": self.guaranteed_booked_usd}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.positions = {k: dict(v) for k, v in (data.get("positions") or {}).items()}
        self.detected = int(data.get("detected", 0) or 0)
        self.sell_both_detected = int(data.get("sell_both_detected", 0) or 0)
        self.executed = int(data.get("executed", 0) or 0)
        self.settled = int(data.get("settled", 0) or 0)
        self.realized_profit_usd = float(data.get("realized_profit_usd", 0.0) or 0.0)
        self.guaranteed_booked_usd = float(data.get("guaranteed_booked_usd", 0.0) or 0.0)
