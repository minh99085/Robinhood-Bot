"""Safe Robinhood MCP client — all tool calls pass through safety gates."""

from __future__ import annotations

import logging
from typing import Any

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import PLACE_TOOLS
from engine.robinhood.robinhood_mcp_adapter import RobinhoodMCPAdapter
from engine.robinhood.safety_gates import RobinhoodSafetyGates, SafetyVerdict

logger = logging.getLogger("hermes.robinhood.client")


class SafeRobinhoodClient:
    """Wraps :class:`RobinhoodMCPAdapter` with mandatory safety enforcement."""

    def __init__(
        self,
        adapter: RobinhoodMCPAdapter,
        gates: RobinhoodSafetyGates | None = None,
        config: RobinhoodConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.config = config or adapter.config
        self.gates = gates or RobinhoodSafetyGates(self.config, adapter.audit)
        self.audit = adapter.audit

    async def _portfolio_snapshot(self) -> dict[str, Any] | None:
        try:
            result = await self.adapter.call_tool("get_portfolio", {})
            return result if isinstance(result, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("portfolio snapshot failed: %s", exc)
            return None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = dict(arguments or {})
        portfolio = None
        if name in PLACE_TOOLS:
            portfolio = await self._portfolio_snapshot()

        verdict = self.gates.evaluate(name, args, portfolio=portfolio)
        if not verdict.allowed:
            raise PermissionError(f"safety gate blocked {name}: {verdict.reason}")

        if name in PLACE_TOOLS:
            verdict = await self.gates.enforce_review(self.adapter, name, args, verdict)
            if not verdict.allowed:
                raise PermissionError(f"review blocked {name}: {verdict.reason}")

        result = await self.adapter.call_tool(name, args)
        if name in PLACE_TOOLS:
            self.gates.day_trades.record()
        return result

    async def list_tools(self) -> list[str]:
        return await self.adapter.list_tools()

    async def review_option_order(self, arguments: dict[str, Any]) -> Any:
        """Call Robinhood review_option_order (allowed even when live trading is off)."""
        args = dict(arguments or {})
        args.setdefault("dry_run", True)
        self.audit.record("mcp_tool_call", tool="review_option_order", details={"arguments": args})
        return await self.adapter.call_tool("review_option_order", args)

    async def review_before_place(
        self, tool: str, arguments: dict[str, Any]
    ) -> SafetyVerdict:
        """Run review_* for a place order without placing."""
        verdict = self.gates.evaluate(tool, arguments)
        if not verdict.allowed:
            return verdict
        if tool not in PLACE_TOOLS:
            return verdict
        return await self.gates.enforce_review(self.adapter, tool, arguments, verdict)

    def status(self) -> dict[str, Any]:
        base = self.adapter.status_dict()
        base["safety"] = {
            "live_trading_enabled": self.config.live_trading_enabled,
            "approval_mode": self.config.approval_mode,
            "max_order_notional_usd": self.config.max_order_notional_usd,
            "review_threshold_notional_usd": self.config.review_threshold_notional_usd,
            "daily_loss_limit_usd": self.config.daily_loss_limit_usd,
            "day_trades_5d": self.gates.day_trades.count_last_5_days(),
        }
        return base