"""Environment-driven configuration for chart vision extraction."""

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


VisionProvider = Literal["openai", "anthropic", "google", "xai", "mock"]
ExecutionMode = Literal["log_only", "recommendation_only", "gated_execution"]


@dataclass(frozen=True)
class ChartVisionConfig:
    """Tunables for vision extraction, MCP validation, and MC handoff."""

    enabled: bool
    provider: VisionProvider
    model: str
    api_key: str
    api_base: str

    # Validation thresholds
    min_overall_confidence: float
    downweight_confidence: float
    max_price_rel_error: float
    require_mcp: bool

    # Pipeline mode
    execution_mode: ExecutionMode
    run_monte_carlo: bool
    mc_paths: int
    mc_horizon_days: int
    mc_seed: int
    monte_carlo_sim_path: str

    # Sizing (aligned with Robinhood safety defaults)
    max_order_notional_usd: float
    max_position_pct: float
    risk_per_trade_pct: float

    # Timeouts
    vision_timeout_s: float

    @classmethod
    def from_env(cls) -> "ChartVisionConfig":
        provider_raw = _env("CHART_VISION_PROVIDER", "mock").lower()
        if provider_raw not in ("openai", "anthropic", "google", "xai", "mock"):
            provider_raw = "mock"
        provider: VisionProvider = provider_raw  # type: ignore[assignment]

        # Provider-specific default models
        default_models = {
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-20250514",
            "google": "gemini-2.0-flash",
            "xai": "grok-2-vision-1212",
            "mock": "mock",
        }
        model = _env("CHART_VISION_MODEL", default_models[provider])

        # API key resolution
        key_env = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "xai": "XAI_API_KEY",
            "mock": "",
        }
        api_key = _env("CHART_VISION_API_KEY") or (
            _env(key_env[provider]) if key_env[provider] else ""
        )

        default_bases = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com",
            "google": "https://generativelanguage.googleapis.com/v1beta",
            "xai": "https://api.x.ai/v1",
            "mock": "",
        }
        api_base = _env("CHART_VISION_API_BASE", default_bases[provider])

        mode_raw = _env("CHART_VISION_EXECUTION_MODE", "recommendation_only").lower()
        if mode_raw not in ("log_only", "recommendation_only", "gated_execution"):
            mode_raw = "recommendation_only"

        return cls(
            enabled=_env_bool("CHART_VISION_ENABLED", True),
            provider=provider,
            model=model,
            api_key=api_key,
            api_base=api_base,
            min_overall_confidence=_env_float("CHART_VISION_MIN_CONFIDENCE", 0.45),
            downweight_confidence=_env_float("CHART_VISION_DOWNWEIGHT_CONFIDENCE", 0.60),
            max_price_rel_error=_env_float("CHART_VISION_MAX_PRICE_REL_ERROR", 0.02),
            require_mcp=_env_bool("CHART_VISION_REQUIRE_MCP", False),
            execution_mode=mode_raw,  # type: ignore[arg-type]
            run_monte_carlo=_env_bool("CHART_VISION_RUN_MC", True),
            mc_paths=_env_int("CHART_VISION_MC_PATHS", 100_000),
            mc_horizon_days=_env_int("CHART_VISION_MC_HORIZON_DAYS", 5),
            mc_seed=_env_int("CHART_VISION_MC_SEED", 42),
            monte_carlo_sim_path=_env(
                "MONTE_CARLO_SIM_PATH",
                str(
                    # Sensible Windows default for this operator workspace
                    os.path.expanduser("~/Monte-Carlo-Sim")
                    if os.name != "nt"
                    else r"C:\Users\tieut\Monte-Carlo-Sim"
                ),
            ),
            max_order_notional_usd=_env_float(
                "CHART_VISION_MAX_ORDER_NOTIONAL_USD",
                _env_float("RH_MAX_ORDER_NOTIONAL_USD", 100.0),
            ),
            max_position_pct=_env_float(
                "CHART_VISION_MAX_POSITION_PCT",
                _env_float("RH_MAX_POSITION_PCT", 10.0),
            ),
            risk_per_trade_pct=_env_float("CHART_VISION_RISK_PER_TRADE_PCT", 0.5),
            vision_timeout_s=_env_float("CHART_VISION_TIMEOUT_S", 90.0),
        )
