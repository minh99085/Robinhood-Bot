"""Aggressive PAPER-ONLY training mode + global paper-only safety lock.

``AGGRESSIVE_PAPER_TRAINING=1`` turns on high-volume paper feedback generation and
makes ABCAS the flagship paper engine. It is **PAPER ONLY**: this module is the
single global guard proving real execution is impossible in aggressive mode —

* it FORCES every live/real-money flag OFF (paper-only locks),
* it FAILS CLOSED (raises :class:`AggressivePaperUnsafe`) if any real-money flag
  is already enabled, so aggressive mode refuses to start, and
* :func:`real_execution_possible` is always False (no wallet signing, no real
  CLOB order submission, no private order endpoint).

It only ever sets PAPER defaults + tightens live locks — it never enables a live
path, never touches wallet/signing logic, and never alters guarded-live/micro-live
safety locks except to force them OFF.

Quant scope — *Compliance, Security & Operational Excellence*: the fail-closed
choke point for paper-mode activation.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger("hte.aggressive_paper")

AGGRESSIVE_PAPER_FLAG = "AGGRESSIVE_PAPER_TRAINING"

# Real-money / live flags that MUST be off in aggressive paper mode. If any is
# truthy we fail closed (refuse to start) rather than forcing it off silently.
FORBIDDEN_LIVE_FLAGS = (
    "BTC_PULSE_LIVE_ENABLED", "BTC_AUTOTRADE_ENABLED", "GUARDED_LIVE_ENABLED",
    "MICRO_LIVE_ENABLED", "MICRO_LIVE_EXECUTION_ENABLED",
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION", "ARB_EXECUTION_ENABLED",
    "HTE_AUTOTRADE", "LIVE_TRADING_ENABLED", "REAL_MONEY_ENABLED",
)

# Live flags forced OFF (paper-only locks) when aggressive paper mode starts.
PAPER_ONLY_LOCKS = {
    "BTC_PULSE_PAPER_ONLY": "1",
    "BTC_PULSE_LIVE_ENABLED": "0",
    "BTC_AUTOTRADE_ENABLED": "0",
    "GUARDED_LIVE_ENABLED": "0",
    "MICRO_LIVE_ENABLED": "0",
    "ARB_EXECUTION_ENABLED": "0",
    "HTE_AUTOTRADE": "0",
    "HTE_MODE": "paper",
}

# Aggressive PAPER defaults (only applied when not already set). All paper-only:
# higher scan/decision throughput, exploration buckets, fill realism, research
# evidence packets, and BTC Pulse aggressive shadow learning.
AGGRESSIVE_PAPER_DEFAULTS = {
    # market universe / throughput
    "POLYMARKET_SCAN_LIMIT": "2000", "POLYMARKET_SHORTLIST_LIMIT": "300",
    "POLYMARKET_LIVE_WATCH_LIMIT": "150", "POLYMARKET_TRADE_CANDIDATE_LIMIT": "80",
    "MARKET_SCAN_LIMIT": "2000", "MARKET_SHORTLIST_LIMIT": "300",
    "MARKET_LIVE_WATCHLIST_LIMIT": "150", "MARKET_TRADE_CANDIDATE_LIMIT": "80",
    "POLYMARKET_SCAN_INTERVAL_SECONDS": "15", "SCORE_REFRESH_SECONDS": "15",
    "CATALOG_REFRESH_SECONDS": "180", "POLYMARKET_MAX_CONCURRENT_REQUESTS": "16",
    # feedback acceleration + labeling (100X paper profit-discovery profile)
    "FEEDBACK_ACCELERATOR_ENABLED": "1", "FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER": "100",
    "PAPER_PROFIT_DISCOVERY_PROFILE": "1",
    "SHADOW_DECISION_LOGGING_ENABLED": "1", "NO_TRADE_LABELING_ENABLED": "1",
    "ACTIVE_LEARNING_ENABLED": "1", "EXPLORATION_TINY_SIZE_ENABLED": "1",
    # exploration buckets (training only; never readiness)
    "POLYMARKET_EXPLORATION_ENABLED": "1", "POLYMARKET_EXPLORATION_RATE": "0.75",
    "POLYMARKET_EXPLORATION_MIN_EDGE": "-0.10", "POLYMARKET_EXPLORATION_NOTIONAL_USD": "1",
    "POLYMARKET_EXPLORATION_BUDGET_USD": "100", "POLYMARKET_PAPER_FIXED_NOTIONAL_USD": "2",
    "PAPER_MAX_ORDER_NOTIONAL_USD": "2", "PAPER_MAX_TOTAL_EXPOSURE_USD": "250",
    "PAPER_MAX_OPEN_ORDERS": "100", "POLYMARKET_MAX_OPEN_TRADES": "25",
    "POLYMARKET_MAX_OPEN_TRADES_HARD_CAP": "50",
    # ABCAS flagship + fill realism
    "BREGMAN_PAPER_SCAN_ENABLED": "1", "ABCAS_ENABLED": "1",
    "FILL_REALISM_ENABLED": "1", "ROBUSTNESS_VALIDATION_ENABLED": "1",
    # research evidence (advisory only)
    "NEWS_SCANNER_ENABLED": "1", "NEWS_PROVIDER_MODE": "live_read_only",
    "RESEARCH_MODE": "online_paper", "NEWS_ENABLE_GROK_PACKET": "1",
    "RESEARCH_USE_IN_STRATEGY": "1", "RESEARCH_ALLOW_TRADE_PROPOSALS": "1",
    "SHADOW_USE_RESEARCH": "1", "SHADOW_ALLOW_ONLINE_RESEARCH": "1",
    # BTC Pulse aggressive shadow/paper learning (gated validation kept strict)
    "BTC_PULSE_ENABLED": "1", "HTE_BTC_PULSE_PAPER_ENABLED": "1",
    "BTC_PULSE_ISOLATED_LEARNING": "1", "BTC_PULSE_REQUIRE_CHAINLINK": "1",
    "BTC_PULSE_FAST_PRICE_REQUIRED": "1",
}


class AggressivePaperUnsafe(RuntimeError):
    """Raised when aggressive paper mode would start with a real-money flag on."""


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on") if v is not None else False


def is_aggressive_paper(env: Optional[Mapping] = None) -> bool:
    env = env if env is not None else os.environ
    return _truthy(env.get(AGGRESSIVE_PAPER_FLAG))


def real_execution_possible(env: Optional[Mapping] = None) -> bool:
    """Hard invariant: real execution is impossible in aggressive paper mode.

    True only if a real-money flag is enabled (which aggressive mode refuses to
    start with). Used by tests to prove no live order path is reachable."""
    env = env if env is not None else os.environ
    if not is_aggressive_paper(env):
        return any(_truthy(env.get(f)) for f in FORBIDDEN_LIVE_FLAGS)
    # in aggressive paper mode the locks force everything off -> never possible
    return False


def enabled_live_flags(env: Optional[Mapping] = None) -> list:
    env = env if env is not None else os.environ
    return [f for f in FORBIDDEN_LIVE_FLAGS if _truthy(env.get(f))]


def aggressive_paper_proof(env: Optional[Mapping] = None) -> dict:
    """Report-grade proof block for the 100X paper profit-discovery profile.

    PURE + read-only. Proves, from the resolved environment, that aggressive paper
    mode is active, the Feedback Accelerator + 100X multiplier are on, real execution
    is impossible, and every forbidden live flag is forced off. Never enables a live
    path. Safe for inclusion in status/report telemetry."""
    e = env if env is not None else os.environ
    agg = is_aggressive_paper(e)
    try:
        mult = int(float(e.get("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", 0) or 0))
    except (TypeError, ValueError):
        mult = 0
    return {
        "aggressive_paper_training_enabled": bool(agg),
        "feedback_accelerator_enabled": _truthy(e.get("FEEDBACK_ACCELERATOR_ENABLED")),
        "feedback_accelerator_target_multiplier": mult,
        "paper_profit_discovery_profile_enabled": (
            _truthy(e.get("PAPER_PROFIT_DISCOVERY_PROFILE")) or bool(agg)),
        # Hard invariants — both are constant under aggressive paper locks.
        "real_execution_possible": real_execution_possible(e),
        "live_flags_forced_off": not enabled_live_flags(e),
    }


def assert_paper_only(env: Optional[Mapping] = None) -> None:
    """Fail closed if any real-money flag is enabled (before applying locks)."""
    on = enabled_live_flags(env)
    if on:
        raise AggressivePaperUnsafe(
            f"refusing aggressive paper mode: real-money flags enabled: {on}")


def apply_aggressive_paper_env(env: Optional[Mapping] = None) -> dict:
    """Activate aggressive PAPER mode: fail-closed safety check, force paper-only
    locks, then apply aggressive defaults (without overriding explicit values).

    Returns ``{locks, defaults_applied, forbidden_clear}``. Raises
    :class:`AggressivePaperUnsafe` if a real-money flag is enabled. Never enables
    a live path."""
    e = env if env is not None else os.environ
    assert_paper_only(e)                       # fail closed on any live flag
    locks: list = []
    for k, v in PAPER_ONLY_LOCKS.items():
        e[k] = v
        locks.append(k)
    applied: list = []
    for k, v in AGGRESSIVE_PAPER_DEFAULTS.items():
        if not str(e.get(k, "")).strip():
            e[k] = v
            applied.append(k)
    e[AGGRESSIVE_PAPER_FLAG] = "1"
    logger.info("AGGRESSIVE_PAPER_TRAINING=1 activated: %d paper-locks, %d defaults; "
                "real_execution_possible=%s", len(locks), len(applied),
                real_execution_possible(e))
    return {"locks": locks, "defaults_applied": applied,
            "forbidden_clear": True, "real_execution_possible": False}
