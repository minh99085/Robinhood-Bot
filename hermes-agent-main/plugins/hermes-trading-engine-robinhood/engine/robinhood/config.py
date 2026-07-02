"""Environment-driven configuration for the Robinhood Agentic plugin."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


ApprovalMode = Literal["review_required", "disabled"]


@dataclass(frozen=True)
class RobinhoodConfig:
    """All tunables for MCP connectivity and safety gates."""

    mcp_url: str
    mcp_server_base: str
    data_dir: str
    live_trading_enabled: bool
    approval_mode: ApprovalMode
    agentic_account_id: str
    max_order_notional_usd: float
    review_threshold_notional_usd: float
    daily_loss_limit_usd: float
    max_position_pct: float
    max_symbol_concentration_pct: float
    max_day_trades_5d: int
    min_buying_power_buffer_usd: float
    oauth_redirect_uri: str
    oauth_client_name: str
    reconnect_base_s: float
    reconnect_max_s: float
    health_interval_s: float
    api_port: int

    @classmethod
    def from_env(cls) -> "RobinhoodConfig":
        mcp_url = _env("RH_MCP_URL", "https://agent.robinhood.com/mcp/trading")
        # OAuth protected-resource metadata uses the full MCP endpoint URL.
        base = mcp_url
        mode_raw = _env("RH_APPROVAL_MODE", "review_required").lower()
        approval_mode: ApprovalMode = (
            "review_required" if mode_raw == "review_required" else "disabled"
        )
        return cls(
            mcp_url=mcp_url,
            mcp_server_base=base,
            data_dir=_env("RH_DATA_DIR", "/data"),
            live_trading_enabled=_env_bool("RH_LIVE_TRADING_ENABLED", False),
            approval_mode=approval_mode,
            agentic_account_id=_env("RH_AGENTIC_ACCOUNT_ID"),
            max_order_notional_usd=_env_float("RH_MAX_ORDER_NOTIONAL_USD", 100.0),
            review_threshold_notional_usd=_env_float("RH_REVIEW_THRESHOLD_NOTIONAL_USD", 50.0),
            daily_loss_limit_usd=_env_float("RH_DAILY_LOSS_LIMIT_USD", 200.0),
            max_position_pct=_env_float("RH_MAX_POSITION_PCT", 10.0),
            max_symbol_concentration_pct=_env_float("RH_MAX_SYMBOL_CONCENTRATION_PCT", 25.0),
            max_day_trades_5d=_env_int("RH_MAX_DAY_TRADES_5D", 3),
            min_buying_power_buffer_usd=_env_float("RH_MIN_BUYING_POWER_BUFFER_USD", 50.0),
            oauth_redirect_uri=_env("RH_OAUTH_REDIRECT_URI", "http://127.0.0.1:53682/callback"),
            oauth_client_name=_env("RH_OAUTH_CLIENT_NAME", "Hermes Robinhood Agent VPS"),
            reconnect_base_s=_env_float("RH_MCP_RECONNECT_BASE_S", 2.0),
            reconnect_max_s=_env_float("RH_MCP_RECONNECT_MAX_S", 300.0),
            health_interval_s=_env_float("RH_HEALTH_INTERVAL_S", 60.0),
            api_port=_env_int("RH_API_PORT", 8810),
        )