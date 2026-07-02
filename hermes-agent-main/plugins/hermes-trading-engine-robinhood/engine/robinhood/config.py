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


from engine.robinhood.watchlist import parse_watchlist

ApprovalMode = Literal["review_required", "disabled"]
OptionBias = Literal["call", "put", "none"]


def _parse_bias(raw: str) -> OptionBias:
    val = raw.strip().lower()
    if val in ("call", "put"):
        return val  # type: ignore[return-value]
    return "none"


def _parse_symbol_biases() -> dict[str, OptionBias]:
    prefix = "RH_OPTIONS_BIAS_"
    out: dict[str, OptionBias] = {}
    for key, val in os.environ.items():
        if not key.startswith(prefix) or key == "RH_OPTIONS_BIAS":
            continue
        sym = key[len(prefix) :].strip().upper()
        if sym:
            out[sym] = _parse_bias(val)
    return out


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
    # Options loop
    options_loop_enabled: bool
    options_tick_seconds: float
    options_watchlist: list[str]
    options_default_bias: OptionBias
    options_symbol_bias: dict[str, OptionBias]
    options_min_dte: int
    options_max_dte: int
    options_strike_band_pct: float
    options_max_spread_pct: float
    options_contracts: int
    options_max_contracts: int
    options_max_premium_usd: float
    options_long_only: bool
    options_max_open_positions: int
    options_symbol_cooldown_s: float
    options_paper_review: bool
    options_min_paper_scans: int

    def bias_for(self, symbol: str) -> OptionBias:
        sym = symbol.upper()
        if sym in self.options_symbol_bias:
            return self.options_symbol_bias[sym]
        return self.options_default_bias

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
            options_loop_enabled=_env_bool("RH_OPTIONS_LOOP_ENABLED", True),
            options_tick_seconds=_env_float("RH_OPTIONS_TICK_SECONDS", 120.0),
            options_watchlist=parse_watchlist(_env("RH_OPTIONS_WATCHLIST", "")),
            options_default_bias=_parse_bias(_env("RH_OPTIONS_BIAS", "none")),
            options_symbol_bias=_parse_symbol_biases(),
            options_min_dte=_env_int("RH_OPTIONS_MIN_DTE", 2),
            options_max_dte=_env_int("RH_OPTIONS_MAX_DTE", 45),
            options_strike_band_pct=_env_float("RH_OPTIONS_STRIKE_BAND_PCT", 5.0),
            options_max_spread_pct=_env_float("RH_OPTIONS_MAX_SPREAD_PCT", 15.0),
            options_contracts=_env_int("RH_OPTIONS_CONTRACTS", 1),
            options_max_contracts=_env_int("RH_OPTIONS_MAX_CONTRACTS", 2),
            options_max_premium_usd=_env_float("RH_OPTIONS_MAX_PREMIUM_USD", 200.0),
            options_long_only=_env_bool("RH_OPTIONS_LONG_ONLY", True),
            options_max_open_positions=_env_int("RH_OPTIONS_MAX_OPEN_POSITIONS", 5),
            options_symbol_cooldown_s=_env_float("RH_OPTIONS_SYMBOL_COOLDOWN_S", 3600.0),
            options_paper_review=_env_bool("RH_OPTIONS_PAPER_REVIEW", True),
            options_min_paper_scans=_env_int("RH_OPTIONS_MIN_PAPER_SCANS", 1),
        )