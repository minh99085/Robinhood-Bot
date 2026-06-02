"""Triple-safeguard system for paper/live mode.

Quant scope — *Compliance/Security/Operational Excellence*: the live-unlock
safeguards are UNCHANGED by the paper risk/portfolio upgrade. Aggressive paper
sizing only changes PAPER order sizes/diversity; it cannot relax these gates,
re-enable live execution, or alter the kill switch.


GUARD 1  AgentReadiness  — automatic gate: live is only unlockable once the paper
                           track record clears hard thresholds (trades, Sharpe,
                           win rate, drawdown, no recent crashes). Exposes a 0-100
                           readiness score.
GUARD 2  (confirmation)  — enforced in the API/UI layer (type CONFIRM + checkbox).
GUARD 3  CircuitBreaker  — runtime kill switches, active in LIVE mode: daily loss
                           auto-downgrade, consecutive-loss pause, max trade size,
                           runaway-order emergency stop, API error-rate halt.

NOTE: there is no real-exchange execution adapter in this codebase. LIVE mode is
"armed simulation" — the guards are real and testable, but no real order is ever
sent. A vetted execution adapter would plug in behind these same guards.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import numpy as np


def _equity_series(curve: list[dict]) -> np.ndarray:
    return np.array([c.get("equity", 0.0) for c in curve], dtype=float)


def equity_sharpe(curve: list[dict]) -> float:
    eq = _equity_series(curve)
    if eq.size < 3:
        return 0.0
    rets = np.diff(eq) / eq[:-1]
    if rets.std() == 0:
        return 0.0
    return float(rets.mean() / rets.std() * np.sqrt(len(rets)))


def equity_max_drawdown(curve: list[dict]) -> float:
    eq = _equity_series(curve)
    if eq.size == 0:
        return 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return float(mdd)


class AgentReadiness:
    """GUARD 1 — the automatic training gate."""

    MIN_TRADES = 500
    MIN_SHARPE = 1.5
    MIN_WINRATE = 0.55
    MAX_DRAWDOWN = 0.15

    @classmethod
    def evaluate(cls, *, trades: int, win_rate: float, sharpe: float,
                 max_dd: float, errors_24h: int) -> dict:
        checks = {
            "min_500_trades": trades >= cls.MIN_TRADES,
            "sharpe_gt_1_5": sharpe > cls.MIN_SHARPE,
            "winrate_gt_55": win_rate > cls.MIN_WINRATE,
            "maxdd_lt_15": max_dd < cls.MAX_DRAWDOWN,
            "no_crashes_24h": errors_24h == 0,
        }
        prog = [
            min(1.0, trades / cls.MIN_TRADES),
            min(1.0, max(0.0, sharpe / cls.MIN_SHARPE)),
            min(1.0, max(0.0, win_rate / cls.MIN_WINRATE)),
            min(1.0, max(0.0, 1.0 - max_dd / cls.MAX_DRAWDOWN)),
            1.0 if errors_24h == 0 else 0.0,
        ]
        score = round(100.0 * sum(prog) / len(prog))
        ready = all(checks.values())
        missing = [k for k, v in checks.items() if not v]
        return {
            "score": score, "ready": ready, "checks": checks, "missing": missing,
            "metrics": {
                "trades": trades, "win_rate": round(win_rate, 4),
                "sharpe": round(sharpe, 2), "max_drawdown": round(max_dd, 4),
                "errors_24h": errors_24h,
            },
            "thresholds": {
                "min_trades": cls.MIN_TRADES, "min_sharpe": cls.MIN_SHARPE,
                "min_winrate": cls.MIN_WINRATE, "max_drawdown": cls.MAX_DRAWDOWN,
            },
        }


class CircuitBreaker:
    """GUARD 3 — runtime kill switches (enforced while in LIVE mode)."""

    def __init__(self, *, log_path: Path | None = None,
                 daily_loss_pct: float = 0.03, consec_losses: int = 3,
                 pause_minutes: int = 30, max_trade_pct: float = 0.05,
                 max_orders_per_min: int = 10, api_fail_rate: float = 0.20,
                 api_window_s: int = 300):
        self.log_path = Path(log_path) if log_path else None
        self.daily_loss_pct = daily_loss_pct
        self.consec_losses = consec_losses
        self.pause_seconds = pause_minutes * 60
        self.max_trade_pct = max_trade_pct
        self.max_orders_per_min = max_orders_per_min
        self.api_fail_rate = api_fail_rate
        self.api_window_s = api_window_s

        self.paused_until = 0.0
        self.halted = False
        self.halt_reason = ""
        self._consec = 0
        self._orders: deque = deque(maxlen=200)
        self._api: deque = deque(maxlen=500)
        self.events: deque = deque(maxlen=50)
        self.last_alert: dict | None = None

    # --- recorders -----------------------------------------------------
    def record_order(self, ts: float | None = None) -> None:
        ts = ts or time.time()
        self._orders.append(ts)
        cutoff = ts - 60
        while self._orders and self._orders[0] < cutoff:
            self._orders.popleft()
        if len(self._orders) > self.max_orders_per_min:
            self._halt(f"runaway orders: {len(self._orders)} in 60s")

    def record_result(self, pnl: float) -> None:
        if pnl < 0:
            self._consec += 1
            if self._consec >= self.consec_losses:
                self.pause(f"{self._consec} consecutive losing trades")
                self._consec = 0
        else:
            self._consec = 0

    def record_api(self, ok: bool) -> None:
        now = time.time()
        self._api.append((now, bool(ok)))
        cutoff = now - self.api_window_s
        while self._api and self._api[0][0] < cutoff:
            self._api.popleft()
        total = len(self._api)
        fails = sum(1 for _, o in self._api if not o)
        if total >= 10 and (fails / total) > self.api_fail_rate:
            self._halt(f"API error rate {fails}/{total} > {self.api_fail_rate:.0%} in {self.api_window_s}s")

    # --- checks / actions ----------------------------------------------
    def daily_loss_breached(self, day_pnl: float, starting_balance: float) -> bool:
        return starting_balance > 0 and day_pnl <= -self.daily_loss_pct * starting_balance

    def trading_allowed(self) -> bool:
        return not self.halted and time.time() >= self.paused_until

    def cap_stake_fraction(self, frac: float) -> float:
        return min(frac, self.max_trade_pct)

    def pause(self, reason: str) -> None:
        self.paused_until = time.time() + self.pause_seconds
        self._event("pause", reason)

    def _halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            self._event("halt", reason)

    def reset_session(self) -> None:
        self.paused_until = 0.0
        self.halted = False
        self.halt_reason = ""
        self._consec = 0
        self._orders.clear()
        self._api.clear()

    def _event(self, kind: str, reason: str) -> None:
        ev = {"ts": round(time.time(), 1), "kind": kind, "reason": reason}
        self.events.append(ev)
        self.last_alert = ev
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    import json
                    f.write(json.dumps(ev) + "\n")
            except OSError:
                pass

    def status(self) -> dict:
        now = time.time()
        return {
            "halted": self.halted, "halt_reason": self.halt_reason or None,
            "paused": now < self.paused_until,
            "paused_seconds_left": max(0, int(self.paused_until - now)),
            "consecutive_losses": self._consec,
            "orders_last_60s": len(self._orders),
            "trading_allowed": self.trading_allowed(),
            "last_alert": self.last_alert,
            "recent_events": list(self.events)[-8:],
            "limits": {
                "daily_loss_pct": self.daily_loss_pct, "consec_losses": self.consec_losses,
                "pause_minutes": self.pause_seconds // 60, "max_trade_pct": self.max_trade_pct,
                "max_orders_per_min": self.max_orders_per_min, "api_fail_rate": self.api_fail_rate,
            },
        }
