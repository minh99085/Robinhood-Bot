"""ResearchBudget — fail-closed rate + cost limits for Grok calls.

Per-minute / hour / day request caps, per-market/day cap, a daily USD cost cap,
an env kill switch, and an emergency-disable flag. ``check()`` is consulted
BEFORE any network call; if any limit is exceeded it returns (False, reason) and
no call is made.
"""

from __future__ import annotations

import os
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:  # noqa: BLE001
        return Decimal(default)


class ResearchBudget:
    def __init__(self, *, per_minute: int = 6, per_hour: int = 60, per_day: int = 300,
                 per_market_per_day: int = 12, max_daily_cost_usd: Decimal | None = None,
                 disable_on_exceeded: bool = True, kill_switch_file: Optional[Path] = None,
                 clock: Optional[Callable[[], int]] = None):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.per_day = per_day
        self.per_market_per_day = per_market_per_day
        self.max_daily_cost_usd = max_daily_cost_usd if max_daily_cost_usd is not None else Decimal("5")
        self.disable_on_exceeded = disable_on_exceeded
        self.kill_switch_file = kill_switch_file
        self.now_ms = clock or _now_ms
        self._calls: deque = deque()                 # ms timestamps
        self._per_market: dict[str, deque] = {}
        self._daily_cost = Decimal(0)
        self._day_anchor = self._day_key()
        self.disabled = os.getenv("RESEARCH_DISABLED", "0") not in ("0", "false", "False", "")

    @classmethod
    def from_env(cls, clock=None, kill_switch_file=None) -> "ResearchBudget":
        return cls(
            per_minute=_env_int("RESEARCH_MAX_REQUESTS_PER_MINUTE", 6),
            per_hour=_env_int("RESEARCH_MAX_REQUESTS_PER_HOUR", 60),
            per_day=_env_int("RESEARCH_MAX_REQUESTS_PER_DAY", 300),
            per_market_per_day=_env_int("RESEARCH_MAX_REQUESTS_PER_MARKET_PER_DAY", 12),
            max_daily_cost_usd=_env_dec("RESEARCH_MAX_DAILY_COST_USD", "5"),
            disable_on_exceeded=os.getenv("RESEARCH_DISABLE_ON_BUDGET_EXCEEDED", "1")
            not in ("0", "false", "False", ""),
            kill_switch_file=kill_switch_file, clock=clock)

    def _day_key(self) -> int:
        return self.now_ms() // 86_400_000

    def _roll_day(self) -> None:
        if self._day_key() != self._day_anchor:
            self._day_anchor = self._day_key()
            self._daily_cost = Decimal(0)
            self._per_market.clear()
            self._calls.clear()

    def _prune(self, dq: deque, window_ms: int) -> None:
        cutoff = self.now_ms() - window_ms
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _kill_switch_active(self) -> bool:
        try:
            return bool(self.kill_switch_file and Path(self.kill_switch_file).exists())
        except OSError:
            return False

    def check(self, market_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
        self._roll_day()
        if self.disabled:
            return False, "research_disabled"
        if self._kill_switch_active():
            return False, "kill_switch"
        self._prune(self._calls, 86_400_000)
        day = len(self._calls)
        minute = sum(1 for t in self._calls if t >= self.now_ms() - 60_000)
        hour = sum(1 for t in self._calls if t >= self.now_ms() - 3_600_000)
        if minute >= self.per_minute:
            return False, "rate_limit_per_minute"
        if hour >= self.per_hour:
            return False, "rate_limit_per_hour"
        if day >= self.per_day:
            return False, "rate_limit_per_day"
        if market_id:
            mdq = self._per_market.get(market_id)
            if mdq is not None:
                self._prune(mdq, 86_400_000)
                if len(mdq) >= self.per_market_per_day:
                    return False, "rate_limit_per_market_per_day"
        if self._daily_cost >= self.max_daily_cost_usd:
            return False, "daily_cost_exceeded"
        return True, None

    def record(self, market_id: Optional[str] = None, cost_usd: Decimal | float = 0) -> None:
        self._roll_day()
        now = self.now_ms()
        self._calls.append(now)
        if market_id:
            self._per_market.setdefault(market_id, deque()).append(now)
        try:
            self._daily_cost += Decimal(str(cost_usd or 0))
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        self._roll_day()
        self._prune(self._calls, 86_400_000)
        return {
            "disabled": self.disabled,
            "requests_today": len(self._calls),
            "requests_last_minute": sum(1 for t in self._calls if t >= self.now_ms() - 60_000),
            "daily_cost_usd": str(self._daily_cost),
            "max_daily_cost_usd": str(self.max_daily_cost_usd),
            "per_minute": self.per_minute, "per_hour": self.per_hour, "per_day": self.per_day,
            "per_market_per_day": self.per_market_per_day,
        }
