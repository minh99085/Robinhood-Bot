"""Paper trading book — positions, sizing, holding time, realized P&L.

The operator's chart-battery verdicts become PAPER positions here: opened
and closed at live Robinhood prices, sized by fixed risk rules, tracked for
holding time so the console can prompt "review due — upload fresh charts",
and settled into the same daily-loss safety gate live trading will use.

No Robinhood order API is ever called from this module. It is a ledger.

Sizing (fixed, no knobs to tune):
    risk_per_trade = equity * RISK_PCT (2% of the $10k bankroll by default)
    qty            = risk_per_trade / (entry_price * stop_pct)
    capped by      MAX_POSITION_PCT of equity and available cash.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BOOK_FILENAME = "paper_book.json"
TRADES_FILENAME = "paper_trades.jsonl"

DEFAULT_EQUITY_START = 10_000.0
RISK_PCT = 0.02            # 2% of equity at risk per trade (owner's limit)
MAX_POSITION_PCT = 0.10    # no position above 10% of equity
DEFAULT_STOP_PCT = 0.05    # if the analysis supplied no stop
# Fractional shares (Robinhood supports them): sizing is DOLLAR-based so a
# $600 or $2,000 asset deploys the same capital as a $50 one on a $10k
# account. Below this notional a trade is dust — refuse instead.
MIN_NOTIONAL_USD = 5.0
QTY_DECIMALS = 4
# horizon_days is in TRADING days; calendar conversion for review prompts
_CAL_PER_TRADING_DAY = 7.0 / 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _age_trading_days(opened_at_iso: str) -> float:
    try:
        then = datetime.fromisoformat(opened_at_iso)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    cal_days = (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
    return max(0.0, cal_days / _CAL_PER_TRADING_DAY)


class PaperBook:
    """File-backed paper portfolio at ``<data_dir>/paper_book.json``."""

    def __init__(self, data_dir: str | Path,
                 equity_start: float = DEFAULT_EQUITY_START) -> None:
        self.dir = Path(data_dir)
        self.path = self.dir / BOOK_FILENAME
        self.trades_path = self.dir / TRADES_FILENAME
        self.state = self._load(equity_start)

    # ---------------- persistence ----------------

    def _load(self, equity_start: float) -> Dict[str, Any]:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "cash" in raw:
                    return raw
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "equity_start": float(equity_start),
            "cash": float(equity_start),
            "positions": [],          # open positions
            "realized_pnl": 0.0,      # lifetime, all closed paper trades
            "created_at": _now_iso(),
        }

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _log_trade(self, row: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        row = dict(row)
        row.setdefault("ts", time.time())
        with self.trades_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ---------------- queries ----------------

    def position_for(self, symbol: str) -> Optional[Dict[str, Any]]:
        sym = symbol.upper()
        for p in self.state["positions"]:
            if p["symbol"] == sym:
                return p
        return None

    def equity(self, marks: Optional[Dict[str, float]] = None) -> float:
        """Cash + market value of open positions (entry price when no mark)."""
        total = float(self.state["cash"])
        for p in self.state["positions"]:
            px = (marks or {}).get(p["symbol"], p["entry_price"])
            total += p["qty"] * float(px)
        return round(total, 2)

    def snapshot(self, marks: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        positions = []
        for p in self.state["positions"]:
            mark = (marks or {}).get(p["symbol"])
            age = _age_trading_days(p["opened_at"])
            horizon = float(p.get("horizon_days") or 5)
            entry = float(p["entry_price"])
            row = dict(p)
            row["age_trading_days"] = round(age, 1)
            row["review_due"] = age >= horizon
            row["review_in_days"] = max(0.0, round(horizon - age, 1))
            if mark:
                row["mark_price"] = mark
                row["unrealized_pnl"] = round((mark - entry) * p["qty"], 2)
                row["unrealized_pct"] = round(mark / entry - 1.0, 4)
                if p.get("stop_price") and mark <= float(p["stop_price"]):
                    row["stop_breached"] = True
            positions.append(row)
        return {
            "cash": round(float(self.state["cash"]), 2),
            "equity": self.equity(marks),
            "equity_start": self.state["equity_start"],
            "realized_pnl": round(float(self.state["realized_pnl"]), 2),
            "positions": positions,
            "n_open": len(positions),
            "review_due": [p["symbol"] for p in positions if p["review_due"]],
        }

    # ---------------- sizing ----------------

    def size_buy(self, price: float, stop_pct: float,
                 max_order_notional: Optional[float] = None) -> Dict[str, Any]:
        """Fixed-risk, DOLLAR-based sizing with fractional shares.

        target dollars = (equity × 2%) / stop_pct, then capped by the 10%
        position cap, any per-order cap, and available cash. Fractional
        qty = dollars / price — so any asset price deploys the intended
        capital (a $2,000 stock gets 0.5 sh, not zero).
        """
        if price <= 0:
            return {"qty": 0.0, "reason": "bad price", "capped_by": []}
        equity = self.equity()
        stop_pct = float(stop_pct) if stop_pct and stop_pct > 0 else DEFAULT_STOP_PCT
        risk_usd = equity * RISK_PCT
        target = risk_usd / stop_pct
        capped_by: List[str] = []
        cap_notional = equity * MAX_POSITION_PCT
        if max_order_notional:
            cap_notional = min(cap_notional, float(max_order_notional))
        if target > cap_notional:
            target = cap_notional
            capped_by.append(f"position cap ${cap_notional:.0f}")
        if target > self.state["cash"]:
            target = float(self.state["cash"])
            capped_by.append("available cash")
        if target < MIN_NOTIONAL_USD:
            return {"qty": 0.0, "risk_usd": round(risk_usd, 2),
                    "stop_pct": stop_pct, "notional": 0.0,
                    "capped_by": capped_by,
                    "reason": f"sized under ${MIN_NOTIONAL_USD:.0f} minimum"}
        qty = round(target / price, QTY_DECIMALS)
        return {
            "qty": qty,
            "risk_usd": round(risk_usd, 2),
            "stop_pct": stop_pct,
            "notional": round(qty * price, 2),
            "capped_by": capped_by,
            "reason": "ok",
        }

    # ---------------- trades ----------------

    def open_position(
        self,
        symbol: str,
        price: float,
        *,
        stop_pct: Optional[float] = None,
        horizon_days: float = 5,
        thesis: str = "",
        max_order_notional: Optional[float] = None,
    ) -> Dict[str, Any]:
        sym = symbol.upper()
        if self.position_for(sym) is not None:
            return {"ok": False, "error": f"already holding {sym} — close it first"}
        if price <= 0:
            return {"ok": False, "error": "bad price"}
        plan = self.size_buy(price, stop_pct or DEFAULT_STOP_PCT,
                             max_order_notional)
        if plan["qty"] <= 0:
            return {"ok": False, "error": f"sized to zero ({plan.get('capped_by') or plan['reason']})"}
        pos = {
            "symbol": sym,
            "qty": plan["qty"],
            "entry_price": round(float(price), 4),
            "stop_pct": plan["stop_pct"],
            "stop_price": round(price * (1.0 - plan["stop_pct"]), 4),
            "horizon_days": float(horizon_days),
            "opened_at": _now_iso(),
            "thesis": thesis[:300],
        }
        self.state["positions"].append(pos)
        self.state["cash"] = round(self.state["cash"] - plan["qty"] * price, 2)
        self.save()
        self._log_trade({"side": "buy", "mode": "paper", **pos,
                         "notional": plan["notional"],
                         "capped_by": plan["capped_by"]})
        return {"ok": True, "position": pos, "plan": plan}

    def close_position(self, symbol: str, price: float,
                       *, reason: str = "") -> Dict[str, Any]:
        sym = symbol.upper()
        pos = self.position_for(sym)
        if pos is None:
            return {"ok": False, "error": f"no open paper position in {sym}"}
        if price <= 0:
            return {"ok": False, "error": "bad price"}
        pnl = round((float(price) - pos["entry_price"]) * pos["qty"], 2)
        self.state["positions"] = [
            p for p in self.state["positions"] if p["symbol"] != sym]
        self.state["cash"] = round(self.state["cash"] + pos["qty"] * price, 2)
        self.state["realized_pnl"] = round(
            self.state["realized_pnl"] + pnl, 2)
        self.save()
        row = {"side": "sell", "mode": "paper", "symbol": sym,
               "qty": pos["qty"], "entry_price": pos["entry_price"],
               "exit_price": round(float(price), 4), "pnl_usd": pnl,
               "held_trading_days": round(_age_trading_days(pos["opened_at"]), 1),
               "reason": reason[:200]}
        self._log_trade(row)
        return {"ok": True, "trade": row, "realized_pnl_total":
                self.state["realized_pnl"]}
