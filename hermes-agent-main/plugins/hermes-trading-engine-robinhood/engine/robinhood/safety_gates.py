"""Pre-trade safety gates for Robinhood Agentic live orders."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import ORDER_TOOLS, PLACE_TOOLS, REVIEW_TOOLS

logger = logging.getLogger("hermes.robinhood.safety")


@dataclass
class SafetyVerdict:
    allowed: bool
    reason: str
    review_required: bool = False
    review_tool: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class DayTradeTracker:
    """Rolling PDT-style day-trade counter (operator-configured limit)."""

    trades: list[float] = field(default_factory=list)

    def record(self) -> None:
        self.trades.append(time.time())

    def count_last_5_days(self) -> int:
        cutoff = time.time() - 5 * 86400
        self.trades = [t for t in self.trades if t >= cutoff]
        return len(self.trades)


class RobinhoodSafetyGates:
    """Enforces sizing, loss, concentration, PDT, and review-before-place rules."""

    def __init__(
        self,
        config: RobinhoodConfig,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config
        self.audit = audit or AuditLog(config.data_dir)
        self.day_trades = DayTradeTracker()
        self._daily_realized_pnl_usd = 0.0
        self._daily_pnl_day: str | None = None

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def record_realized_pnl(self, pnl_usd: float) -> None:
        today = self._today_key()
        if self._daily_pnl_day != today:
            self._daily_pnl_day = today
            self._daily_realized_pnl_usd = 0.0
        self._daily_realized_pnl_usd += pnl_usd

    @staticmethod
    def _order_notional(args: dict[str, Any]) -> float:
        for key in ("notional", "amount", "dollar_amount", "order_amount"):
            if key in args:
                try:
                    return float(args[key])
                except (TypeError, ValueError):
                    pass
        qty = args.get("quantity") or args.get("qty")
        price = args.get("price") or args.get("limit_price")
        if qty is not None and price is not None:
            try:
                return abs(float(qty) * float(price))
            except (TypeError, ValueError):
                pass
        return 0.0

    @staticmethod
    def _symbol(args: dict[str, Any]) -> str:
        for key in ("symbol", "instrument", "ticker"):
            val = args.get(key)
            if val:
                return str(val).upper()
        return ""

    def evaluate(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        portfolio: dict[str, Any] | None = None,
    ) -> SafetyVerdict:
        """Check whether a tool call may proceed (does not call Robinhood review APIs)."""
        args = dict(arguments or {})
        tool = tool.strip()

        if tool not in ORDER_TOOLS and tool not in PLACE_TOOLS:
            return SafetyVerdict(True, "non_order_tool")

        if not self.config.live_trading_enabled and tool in PLACE_TOOLS:
            verdict = SafetyVerdict(False, "live_trading_disabled")
            self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
            return verdict

        if self.config.approval_mode == "disabled" and tool in PLACE_TOOLS:
            verdict = SafetyVerdict(False, "approval_mode_disabled")
            self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
            return verdict

        notional = self._order_notional(args)
        if notional > self.config.max_order_notional_usd:
            verdict = SafetyVerdict(
                False,
                f"notional ${notional:.2f} exceeds max ${self.config.max_order_notional_usd:.2f}",
            )
            self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
            return verdict

        if self._daily_realized_pnl_usd <= -abs(self.config.daily_loss_limit_usd):
            verdict = SafetyVerdict(False, "daily_loss_limit_reached")
            self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
            return verdict

        if tool in PLACE_TOOLS:
            dt_count = self.day_trades.count_last_5_days()
            if dt_count >= self.config.max_day_trades_5d:
                verdict = SafetyVerdict(False, f"pdt_limit_reached ({dt_count} day trades / 5d)")
                self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
                return verdict

        if tool in PLACE_TOOLS and "option" in tool:
            verdict = self._check_option_order(args, notional)
            if not verdict.allowed:
                self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
                return verdict

        if portfolio and tool in PLACE_TOOLS:
            verdict = self._check_concentration(args, portfolio, notional)
            if not verdict.allowed:
                self.audit.record("safety_block", tool=tool, allowed=False, reason=verdict.reason)
                return verdict

        if tool in PLACE_TOOLS and notional >= self.config.review_threshold_notional_usd:
            review_tool = (
                "review_option_order" if "option" in tool else "review_equity_order"
            )
            verdict = SafetyVerdict(
                True,
                "review_required_before_place",
                review_required=True,
                review_tool=review_tool,
            )
            self.audit.record("safety_review_required", tool=tool, allowed=True, reason=verdict.reason)
            return verdict

        verdict = SafetyVerdict(True, "ok")
        self.audit.record("safety_pass", tool=tool, allowed=True, reason=verdict.reason)
        return verdict

    def _check_concentration(
        self,
        args: dict[str, Any],
        portfolio: dict[str, Any],
        notional: float,
    ) -> SafetyVerdict:
        total = float(portfolio.get("total_value") or portfolio.get("equity") or 0)
        buying_power = float(portfolio.get("buying_power") or 0)
        if buying_power < self.config.min_buying_power_buffer_usd:
            return SafetyVerdict(False, "insufficient_buying_power_buffer")

        symbol = self._symbol(args)
        if total > 0 and notional > 0:
            new_pct = (notional / total) * 100
            if new_pct > self.config.max_position_pct:
                return SafetyVerdict(
                    False,
                    f"order {new_pct:.1f}% of portfolio exceeds max {self.config.max_position_pct:.1f}%",
                )
            if symbol:
                positions = portfolio.get("positions") or portfolio.get("equity_positions") or []
                sym_value = 0.0
                for pos in positions:
                    psym = str(pos.get("symbol") or pos.get("ticker") or "").upper()
                    if psym == symbol:
                        sym_value += float(pos.get("market_value") or pos.get("value") or 0)
                conc = ((sym_value + notional) / total) * 100
                if conc > self.config.max_symbol_concentration_pct:
                    return SafetyVerdict(
                        False,
                        f"{symbol} concentration {conc:.1f}% exceeds max "
                        f"{self.config.max_symbol_concentration_pct:.1f}%",
                    )
        return SafetyVerdict(True, "concentration_ok")

    def _check_option_order(self, args: dict[str, Any], notional: float) -> SafetyVerdict:
        symbol = self._symbol(args)
        if symbol and self.config.options_watchlist and symbol not in self.config.options_watchlist:
            return SafetyVerdict(False, f"{symbol} not in options watchlist")

        side = str(args.get("side") or "buy").lower()
        if self.config.options_long_only and side not in ("buy", "debit", "long"):
            return SafetyVerdict(False, f"long_only: side {side!r} blocked")

        qty_raw = args.get("quantity") or args.get("qty") or 1
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            qty = 1
        if qty > self.config.options_max_contracts:
            return SafetyVerdict(
                False,
                f"quantity {qty} exceeds max contracts {self.config.options_max_contracts}",
            )

        price = args.get("limit_price") or args.get("price")
        premium = 0.0
        if price is not None:
            try:
                premium = float(price) * qty * 100.0
            except (TypeError, ValueError):
                premium = 0.0
        elif notional > 0:
            premium = notional
        if premium > self.config.options_max_premium_usd:
            return SafetyVerdict(
                False,
                f"premium ${premium:.2f} exceeds max ${self.config.options_max_premium_usd:.2f}",
            )

        return SafetyVerdict(True, "option_checks_ok")

    async def enforce_review(
        self,
        adapter: Any,
        tool: str,
        arguments: dict[str, Any],
        verdict: SafetyVerdict,
    ) -> SafetyVerdict:
        """Call Robinhood review_* tool before place; block on hard warnings."""
        if not verdict.review_required or not verdict.review_tool:
            return verdict
        review_args = dict(arguments)
        review_args.setdefault("dry_run", True)
        try:
            review_result = await adapter.call_tool(verdict.review_tool, review_args)
        except Exception as exc:  # noqa: BLE001
            blocked = SafetyVerdict(False, f"review_call_failed: {exc}")
            self.audit.record("safety_block", tool=tool, allowed=False, reason=blocked.reason)
            return blocked

        warnings = _extract_warnings(review_result)
        verdict.warnings = warnings
        if _has_blocking_warnings(warnings):
            blocked = SafetyVerdict(False, "review_blocked", warnings=warnings)
            self.audit.record(
                "safety_block",
                tool=tool,
                allowed=False,
                reason=blocked.reason,
                details={"warnings": warnings},
            )
            return blocked

        self.audit.record(
            "safety_review_pass",
            tool=tool,
            allowed=True,
            details={"warnings": warnings},
        )
        return verdict


def _extract_warnings(review_result: Any) -> list[str]:
    if isinstance(review_result, dict):
        for key in ("warnings", "alerts", "issues", "messages"):
            val = review_result.get(key)
            if isinstance(val, list):
                return [str(x) for x in val]
        if review_result.get("blocked") or review_result.get("can_place") is False:
            return [str(review_result)]
    if isinstance(review_result, list):
        return [str(x) for x in review_result]
    return []


def _has_blocking_warnings(warnings: list[str]) -> bool:
    if not warnings:
        return False
    block_tokens = ("error", "reject", "blocked", "not tradable", "insufficient", "pattern day")
    joined = " ".join(warnings).lower()
    return any(tok in joined for tok in block_tokens)