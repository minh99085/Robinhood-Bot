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
                     min_profit_usd: float = 0.0,
                     max_usd: Optional[float] = None) -> Optional[ArbOpportunity]:
    """Detect a risk-free within-window dutch book on the up/down ladders. Returns the best
    actionable opportunity (or a non-actionable one for diagnostics, or None).

    - BUY-both: ``vwap_up_ask + vwap_down_ask < 1 - fees - epsilon`` -> buy 1 up + 1 down for < $1,
      collect $1 at settlement.
    - SELL-both: ``vwap_up_bid + vwap_down_bid > 1 + fees + epsilon`` -> MINT a complete set for $1
      (collateral split, NOT a naked short) and sell both legs into the bids for > $1. Profit =
      bid_sum - 1 per set. Risk-free and deterministic; fully collateralized.

    Both legs must FULLY fill within depth (capped at ``max_depth_consume_frac`` of the thinner side)
    and on tick. Reuses the strict execution-gate VWAP/depth math (no top-of-book fantasy)."""
    if up_book is None or down_book is None:
        return None
    # size‑to‑depth: take the full available depth (capped at max_depth_consume_frac of the thinner
    # leg) up to a max_usd ceiling. Defaults to size_usd so legacy callers keep the old behavior.
    max_usd = float(max_usd) if max_usd is not None else float(size_usd)
    up_asks = getattr(up_book, "asks", None) or []
    dn_asks = getattr(down_book, "asks", None) or []
    up_bids = getattr(up_book, "bids", None) or []
    dn_bids = getattr(down_book, "bids", None) or []
    best_up = getattr(up_book, "best_ask", None)
    best_dn = getattr(down_book, "best_ask", None)
    bbu, bbd = getattr(up_book, "best_bid", None), getattr(down_book, "best_bid", None)
    tob_res = ((float(best_up) + float(best_dn) - 1.0) if (best_up and best_dn) else 0.0)
    # stale-book guard (same realism as the execution gate) — shared by both directions
    ts_u, ts_d = getattr(up_book, "ts", 0), getattr(down_book, "ts", 0)
    stale = (now is not None and max_book_age_s > 0
             and ((ts_u and now - float(ts_u) > max_book_age_s)
                  or (ts_d and now - float(ts_d) > max_book_age_s)))
    buy = sell = None
    # ---- BUY-BOTH (buy 1 up + 1 down for < $1) ----
    if up_asks and dn_asks and best_up and best_dn:
        up_depth = float(getattr(up_book, "ask_depth_usd", 0.0) or 0.0)
        dn_depth = float(getattr(down_book, "ask_depth_usd", 0.0) or 0.0)
        cap_notional = max_depth_consume_frac * min(up_depth, dn_depth)
        target = min(cap_notional, max_usd) if cap_notional > 0 else float(max_usd)
        depth_capped = bool(cap_notional > 0 and cap_notional < max_usd)
        vwu, spent_u, sh_u, full_u = vwap_fill(up_asks, target)
        vwd, spent_d, sh_d, full_d = vwap_fill(dn_asks, target)
        if vwu is not None and vwd is not None:
            shares = min(sh_u, sh_d)            # matched pairs: 1 up + 1 down -> guaranteed $1
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
                kind="buy_both", up_vwap=vwu, down_vwap=vwd, shares=shares,
                cost_usd=shares * (vwu + vwd), guaranteed_profit_usd=profit, ask_sum=(vwu + vwd),
                tob_residual=tob_res, vwap_residual=(vwu + vwd - 1.0), depth_capped=depth_capped,
                actionable=actionable, reason=reason)
    # ---- SELL-BOTH (mint a $1 set, sell both legs into the bids for > $1) ----
    if up_bids and dn_bids and bbu and bbd:
        up_bdepth = float(getattr(up_book, "bid_depth_usd", 0.0) or 0.0)
        dn_bdepth = float(getattr(down_book, "bid_depth_usd", 0.0) or 0.0)
        cap_b = max_depth_consume_frac * min(up_bdepth, dn_bdepth)
        target_b = min(cap_b, max_usd) if cap_b > 0 else float(max_usd)
        depth_capped_b = bool(cap_b > 0 and cap_b < max_usd)
        bvu, bspent_u, bsh_u, bfull_u = vwap_fill(up_bids, target_b)
        bvd, bspent_d, bsh_d, bfull_d = vwap_fill(dn_bids, target_b)
        if bvu is not None and bvd is not None:
            shares_s = min(bsh_u, bsh_d)        # mint N sets ($N), sell N up + N down into bids
            profit_s = shares_s * ((bvu + bvd) - 1.0)
            ok_tick_s = _on_tick(float(bbu), tick_size) and _on_tick(float(bbd), tick_size)
            actionable_s = bool(bfull_u and bfull_d and not stale and ok_tick_s and shares_s > 0
                                and (bvu + bvd) > (1.0 + fees + epsilon)
                                and profit_s >= min_profit_usd)
            reason_s = ("ok" if actionable_s else
                        ("stale_book" if stale else
                         ("partial_fill" if not (bfull_u and bfull_d) else
                          ("below_epsilon" if (bvu + bvd) <= (1.0 + fees + epsilon) else
                           ("tick" if not ok_tick_s else "no_edge")))))
            sell = ArbOpportunity(
                kind="sell_both", up_vwap=bvu, down_vwap=bvd, shares=shares_s,
                cost_usd=shares_s * 1.0, guaranteed_profit_usd=profit_s, ask_sum=(bvu + bvd),
                tob_residual=((float(bbu) + float(bbd) - 1.0)), vwap_residual=(bvu + bvd - 1.0),
                depth_capped=depth_capped_b, actionable=actionable_s, reason=reason_s)
    # prefer the actionable opportunity with the larger guaranteed profit; else a diagnostic one
    cands = [o for o in (buy, sell) if o is not None]
    actionables = [o for o in cands if o.actionable]
    if actionables:
        return max(actionables, key=lambda o: o.guaranteed_profit_usd)
    return max(cands, key=lambda o: o.guaranteed_profit_usd) if cands else None


class ArbLedger:
    """SEPARATE paper ledger for risk-free arbitrage fills — kept apart from the directional ledger
    so the two strategies are NEVER blended in win-rate / profit-factor stats. P&L is deterministic."""

    def __init__(self):
        self.positions: dict = {}     # window_key -> open arb position dict
        self.detected = 0             # actionable opportunities seen (buy or sell)
        self.sell_both_detected = 0   # sell-both opportunities seen (actionable or not)
        self.executed = 0
        self.executed_buy = 0         # executed buy-both (buy 1 up + 1 down < $1)
        self.executed_sell = 0        # executed sell-both (mint $1 set, sell both > $1)
        self.settled = 0
        self.realized_profit_usd = 0.0
        self.guaranteed_booked_usd = 0.0   # sum of guaranteed profit at book time
        self.scans = 0
        self.near_miss = 0                  # vwap ask-sum within epsilon of 1 (diagnostic)
        self.min_vwap_residual: Optional[float] = None

    def record_scan(self, opp: Optional["ArbOpportunity"], *, near_miss_eps: float = 0.02) -> None:
        """P1 instrumentation: track every dutch-book scan for capacity planning."""
        self.scans += 1
        if opp is None:
            return
        vr = abs(float(opp.vwap_residual))
        if self.min_vwap_residual is None or vr < self.min_vwap_residual:
            self.min_vwap_residual = round(vr, 6)
        if (not opp.actionable) and vr <= float(near_miss_eps):
            self.near_miss += 1

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
        if opp.kind == "sell_both":
            self.executed_sell += 1
        else:
            self.executed_buy += 1
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
                "executed_buy": self.executed_buy, "executed_sell": self.executed_sell,
                "settled": self.settled, "open": len(self.open_positions()),
                "realized_profit_usd": round(self.realized_profit_usd, 4),
                "guaranteed_booked_usd": round(self.guaranteed_booked_usd, 4),
                "scans": self.scans,
                "near_miss_within_eps": self.near_miss,
                "min_vwap_ask_residual": self.min_vwap_residual,
                "note": ("risk-free dutch book: BUY-both (asks<$1) + SELL-both (mint $1 set, sell "
                         "bids>$1); deterministic P&L; NEVER blended into directional. PAPER ONLY.")}

    def to_state(self) -> dict:
        return {"positions": {k: dict(v) for k, v in self.positions.items()},
                "detected": self.detected, "sell_both_detected": self.sell_both_detected,
                "executed": self.executed, "executed_buy": self.executed_buy,
                "executed_sell": self.executed_sell, "settled": self.settled,
                "realized_profit_usd": self.realized_profit_usd,
                "guaranteed_booked_usd": self.guaranteed_booked_usd,
                "scans": self.scans, "near_miss": self.near_miss,
                "min_vwap_residual": self.min_vwap_residual}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.positions = {k: dict(v) for k, v in (data.get("positions") or {}).items()}
        self.detected = int(data.get("detected", 0) or 0)
        self.sell_both_detected = int(data.get("sell_both_detected", 0) or 0)
        self.executed = int(data.get("executed", 0) or 0)
        self.executed_buy = int(data.get("executed_buy", 0) or 0)
        self.executed_sell = int(data.get("executed_sell", 0) or 0)
        self.settled = int(data.get("settled", 0) or 0)
        self.realized_profit_usd = float(data.get("realized_profit_usd", 0.0) or 0.0)
        self.guaranteed_booked_usd = float(data.get("guaranteed_booked_usd", 0.0) or 0.0)
        self.scans = int(data.get("scans", 0) or 0)
        self.near_miss = int(data.get("near_miss", 0) or 0)
        mvr = data.get("min_vwap_residual")
        self.min_vwap_residual = (float(mvr) if mvr is not None else None)
