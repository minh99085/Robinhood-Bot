"""GuardedLiveConfig (Phase 8).

Defines how a FUTURE guarded-live phase would be configured, while keeping real
execution disabled. Defaults keep guarded live disabled / design-only / dry-run.
Even with GUARDED_LIVE_ENABLED=1, real execution remains impossible in Phase 8.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

# Method names that MUST be hard-disabled (raise LiveExecutionDisabled).
FORBIDDEN_METHOD_NAMES = (
    "submit_order", "cancel_order", "replace_order", "post_order", "create_order",
    "create_and_post_order", "create_market_order", "create_and_post_market_order",
)

# Env vars that must NOT be present in guarded-live mode (would imply real exec).
DEFAULT_FORBIDDEN_ENV = (
    "POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_PRIVATE_KEY",
    "POLYMARKET_SIGNER_PRIVATE_KEY", "KALSHI_TRADING_PRIVATE_KEY",
    "KALSHI_ORDER_PRIVATE_KEY", "LIVE_BROKER_ENABLED", "ENABLE_REAL_ORDERS",
    "REAL_MONEY", "PRODUCTION_EXECUTION",
)

# Endpoint substrings that must never be called in guarded-live dry-run.
DEFAULT_FORBIDDEN_ENDPOINTS = (
    "/orders", "create_order", "createOrder", "postOrder", "createAndPostOrder",
    "createAndPostMarketOrder", "/portfolio/orders", "order/create",
)


def _f(name, d):
    try:
        return float(os.getenv(name, str(d)))
    except (TypeError, ValueError):
        return d


def _i(name, d):
    try:
        return int(os.getenv(name, str(d)))
    except (TypeError, ValueError):
        return d


def _b(name, d):
    return os.getenv(name, d) not in ("0", "false", "False", "")


def _list(name, d=""):
    return [x.strip() for x in os.getenv(name, d).split(",") if x.strip()]


@dataclass
class GuardedLiveConfig:
    enabled: bool = False
    mode: str = "design_only"          # disabled | design_only | dry_run_only
    dry_run_only: bool = True
    start_on_boot: bool = False

    # required shadow readiness
    required_shadow_status: str = "READY_FOR_MANUAL_REVIEW"
    min_shadow_decisions: int = 200
    min_shadow_runtime_hours: float = 24.0
    max_shadow_report_age_hours: float = 24.0

    # approvals / arming
    required_approvals: int = 2
    approval_expiry_minutes: int = 60
    arming_expiry_minutes: int = 15
    approver_roles: list = field(default_factory=lambda: ["lab_manager", "risk_reviewer"])
    require_typed_confirmation: bool = True

    # dry-run controls
    allow_dry_run_intents: bool = True
    block_network: bool = True
    block_signing: bool = True
    forbid_order_endpoints: bool = True
    conformance_required: bool = True

    # safety envelope
    max_order_notional_usd: Decimal = Decimal("1")
    max_daily_notional_usd: Decimal = Decimal("10")
    max_market_exposure_usd: Decimal = Decimal("5")
    max_venue_exposure_usd: Decimal = Decimal("10")
    max_total_exposure_usd: Decimal = Decimal("20")
    max_daily_loss_usd: Decimal = Decimal("5")
    min_edge_after_costs: float = 0.08
    max_spread: float = 0.03
    max_stale_ms: int = 1000
    max_ambiguity_score: float = 0.25
    min_evidence_score: float = 0.50
    min_source_count: int = 2
    min_time_to_close_seconds: int = 3600
    allowlist_venues: list = field(default_factory=list)
    allowlist_markets: list = field(default_factory=list)
    blocklist_markets: list = field(default_factory=list)

    # kill switches
    kill_switch_path: str = "./GUARDED_LIVE_KILL_SWITCH"
    global_kill_switch_path: str = "./KILL_SWITCH"

    # secret policy
    secret_policy_strict: bool = True
    forbidden_env_patterns: tuple = DEFAULT_FORBIDDEN_ENV
    forbidden_method_names: tuple = FORBIDDEN_METHOD_NAMES
    forbidden_endpoint_patterns: tuple = DEFAULT_FORBIDDEN_ENDPOINTS

    output_dir: str = "guarded_live_artifacts"
    _frozen: bool = False

    @classmethod
    def from_env(cls) -> "GuardedLiveConfig":
        return cls(
            enabled=_b("GUARDED_LIVE_ENABLED", "0"),
            mode=(os.getenv("GUARDED_LIVE_MODE", "design_only") or "design_only"),
            dry_run_only=_b("GUARDED_LIVE_DRY_RUN_ONLY", "1"),
            start_on_boot=_b("GUARDED_LIVE_START_ON_BOOT", "0"),
            required_shadow_status=os.getenv("GUARDED_LIVE_REQUIRED_SHADOW_STATUS",
                                             "READY_FOR_MANUAL_REVIEW"),
            min_shadow_decisions=_i("GUARDED_LIVE_MIN_SHADOW_DECISIONS", 200),
            min_shadow_runtime_hours=_f("GUARDED_LIVE_MIN_SHADOW_RUNTIME_HOURS", 24),
            max_shadow_report_age_hours=_f("GUARDED_LIVE_MAX_SHADOW_REPORT_AGE_HOURS", 24),
            required_approvals=_i("GUARDED_LIVE_REQUIRED_APPROVALS", 2),
            approval_expiry_minutes=_i("GUARDED_LIVE_APPROVAL_EXPIRY_MINUTES", 60),
            arming_expiry_minutes=_i("GUARDED_LIVE_ARMING_EXPIRY_MINUTES", 15),
            approver_roles=_list("GUARDED_LIVE_APPROVER_ROLES", "lab_manager,risk_reviewer"),
            require_typed_confirmation=_b("GUARDED_LIVE_REQUIRE_TYPED_CONFIRMATION", "1"),
            allow_dry_run_intents=_b("GUARDED_LIVE_ALLOW_DRY_RUN_INTENTS", "1"),
            block_network=_b("GUARDED_LIVE_BLOCK_NETWORK", "1"),
            block_signing=_b("GUARDED_LIVE_BLOCK_SIGNING", "1"),
            forbid_order_endpoints=_b("GUARDED_LIVE_FORBID_ORDER_ENDPOINTS", "1"),
            conformance_required=_b("GUARDED_LIVE_CONFORMANCE_REQUIRED", "1"),
            max_order_notional_usd=Decimal(str(_f("GUARDED_LIVE_MAX_ORDER_NOTIONAL_USD", 1))),
            max_daily_notional_usd=Decimal(str(_f("GUARDED_LIVE_MAX_DAILY_NOTIONAL_USD", 10))),
            max_market_exposure_usd=Decimal(str(_f("GUARDED_LIVE_MAX_MARKET_EXPOSURE_USD", 5))),
            max_venue_exposure_usd=Decimal(str(_f("GUARDED_LIVE_MAX_VENUE_EXPOSURE_USD", 10))),
            max_total_exposure_usd=Decimal(str(_f("GUARDED_LIVE_MAX_TOTAL_EXPOSURE_USD", 20))),
            max_daily_loss_usd=Decimal(str(_f("GUARDED_LIVE_MAX_DAILY_LOSS_USD", 5))),
            min_edge_after_costs=_f("GUARDED_LIVE_MIN_EDGE_AFTER_COSTS", 0.08),
            max_spread=_f("GUARDED_LIVE_MAX_SPREAD", 0.03),
            max_stale_ms=_i("GUARDED_LIVE_MAX_STALE_MS", 1000),
            max_ambiguity_score=_f("GUARDED_LIVE_MAX_AMBIGUITY_SCORE", 0.25),
            min_evidence_score=_f("GUARDED_LIVE_MIN_EVIDENCE_SCORE", 0.50),
            min_source_count=_i("GUARDED_LIVE_MIN_SOURCE_COUNT", 2),
            min_time_to_close_seconds=_i("GUARDED_LIVE_MIN_TIME_TO_CLOSE_SECONDS", 3600),
            allowlist_venues=_list("GUARDED_LIVE_ALLOWLIST_VENUES", ""),
            allowlist_markets=_list("GUARDED_LIVE_ALLOWLIST_MARKETS", ""),
            blocklist_markets=_list("GUARDED_LIVE_BLOCKLIST_MARKETS", ""),
            kill_switch_path=os.getenv("GUARDED_LIVE_KILL_SWITCH_PATH",
                                       "./GUARDED_LIVE_KILL_SWITCH"),
            global_kill_switch_path=os.getenv("GLOBAL_KILL_SWITCH_PATH", "./KILL_SWITCH"),
            secret_policy_strict=_b("GUARDED_LIVE_SECRET_POLICY_STRICT", "1"),
            forbidden_env_patterns=tuple(_list("GUARDED_LIVE_FORBIDDEN_ENV_PATTERNS",
                                               ",".join(DEFAULT_FORBIDDEN_ENV))),
            output_dir=os.getenv("GUARDED_LIVE_OUTPUT_DIR", "guarded_live_artifacts"),
        )

    def freeze(self) -> None:
        object.__setattr__(self, "_frozen", True)

    def kill_switch_active(self) -> bool:
        for p in (self.kill_switch_path, self.global_kill_switch_path):
            try:
                if p and Path(p).exists():
                    return True
            except OSError:
                continue
        return False

    def public_dict(self) -> dict:
        d = {k: (str(v) if isinstance(v, Decimal) else (list(v) if isinstance(v, tuple) else v))
             for k, v in asdict(self).items() if not k.startswith("_")}
        return d

    def config_hash(self) -> str:
        blob = json.dumps(self.public_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def risk_limits_hash(self) -> str:
        try:
            from ..risk import RiskLimits
            blob = json.dumps(RiskLimits.from_env().as_dict(), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            blob = "{}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
