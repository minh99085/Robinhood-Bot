"""MicroLiveLocks (Phase 9). Multiple INDEPENDENT locks, all of which must be
open before any real order can be submitted. Defaults keep live disabled.

The source-code build lock (BUILD_ENABLED) is a module constant — production code
must NOT infer it from env. Tests monkeypatch ``engine.micro_live.locks.BUILD_ENABLED``.
"""

from __future__ import annotations

import os
from typing import Optional

from .config import REQUIRED_ACK_PHRASE, MicroLiveConfig
from .schemas import MicroLiveLockStatus

# Source-code build lock. NEVER set from env. Default False = live impossible.
BUILD_ENABLED = False


def _ls(name, passed, reason="", required=None, observed=None) -> MicroLiveLockStatus:
    return MicroLiveLockStatus(lock_name=name, passed=bool(passed), reason=reason,
                               required_value=required, observed_value_redacted=observed)


def check_locks(config: Optional[MicroLiveConfig] = None) -> list[MicroLiveLockStatus]:
    cfg = config or MicroLiveConfig.from_env()
    out: list[MicroLiveLockStatus] = []

    # 1) source-code build lock (read module global at call time -> monkeypatchable)
    out.append(_ls("source_build_lock", BUILD_ENABLED,
                   "MICRO_LIVE_BUILD_ENABLED build constant must be True (not env-derived)"))
    # 2) runtime env lock
    out.append(_ls("runtime_lock", cfg.enabled, "MICRO_LIVE_ENABLED=1 required"))
    # 3) explicit real-money acknowledgement (value never logged)
    ack = os.getenv("MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", "")
    out.append(_ls("real_money_acknowledgement", ack == REQUIRED_ACK_PHRASE,
                   "exact acknowledgement phrase required", observed="[REDACTED]"))
    # 4) demo/prod lock
    env_ok = (cfg.environment in cfg.allowed_environments
              and (cfg.environment != "prod" or cfg.allow_production))
    out.append(_ls("environment_lock", env_ok,
                   "prod requires MICRO_LIVE_ALLOW_PRODUCTION=1 and allowlisted env",
                   observed=cfg.environment))
    # 5) CLI-only lock (the safe state is cli_only=True)
    out.append(_ls("cli_only_lock", cfg.cli_only, "MICRO_LIVE_CLI_ONLY=1 required"))
    # 6) single-order lock
    out.append(_ls("single_order_lock", cfg.one_order_per_token,
                   "MICRO_LIVE_ONE_ORDER_PER_TOKEN=1 required"))
    # 7) kill switches clear
    out.append(_ls("kill_switches_clear", not cfg.kill_switch_active(),
                   "a kill switch file is present"))
    # 8) no autonomous loop
    out.append(_ls("no_autonomous_loop", not cfg.allow_autonomous_loop,
                   "autonomous live loop is forbidden in Phase 9"))
    return out


def all_pass(results: list[MicroLiveLockStatus]) -> bool:
    return all(r.passed for r in results)


def failed_locks(results: list[MicroLiveLockStatus]) -> list[str]:
    return [r.lock_name for r in results if not r.passed]
