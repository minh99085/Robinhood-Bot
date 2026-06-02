"""MicroLiveConfig (Phase 9).

Disabled by default, demo by default, FOK-only by default, one order per token,
no production, no REST submit endpoint. Hard caps are enforced IN CODE (not just
env): e.g. order notional can never exceed $1 regardless of env.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

# --- HARD, CODE-ENFORCED CAPS (env can only make things SMALLER) ----------- #
HARD_MAX_ORDER_NOTIONAL_USD = Decimal("1.0")
HARD_MAX_DAILY_NOTIONAL_USD = Decimal("5.0")
HARD_MAX_ORDERS_PER_DAY = 3
HARD_MAX_ACTIVE_ORDERS = 1

# The exact phrase a human must set to acknowledge real-money risk.
REQUIRED_ACK_PHRASE = "I ACCEPT MICRO LIVE REAL MONEY RISK"
SUBMIT_CONFIRMATION = "SUBMIT ONE MICRO LIVE CANARY ORDER"
EMERGENCY_CANCEL_CONFIRMATION = "EMERGENCY CANCEL MICRO LIVE ORDER"

_FORBIDDEN_TIF = ("good_till_canceled", "good_till_date", "gtc", "gtd")


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


def _capped(env_name: str, default: float, hard_cap: Decimal) -> Decimal:
    v = Decimal(str(_f(env_name, default)))
    return min(v, hard_cap) if v > 0 else hard_cap


@dataclass
class MicroLiveConfig:
    enabled: bool = False
    environment: str = "demo"          # demo | prod
    allow_production: bool = False
    cli_only: bool = True
    one_order_per_token: bool = True
    max_active_orders: int = 1
    max_orders_per_day: int = 1

    # hard limits (post-cap)
    max_order_notional_usd: Decimal = HARD_MAX_ORDER_NOTIONAL_USD
    max_daily_notional_usd: Decimal = Decimal("1")
    max_market_exposure_usd: Decimal = Decimal("1")
    max_venue_exposure_usd: Decimal = Decimal("1")
    max_total_exposure_usd: Decimal = Decimal("1")
    max_daily_loss_usd: Decimal = Decimal("1")
    min_edge_after_costs: float = 0.10
    max_spread: float = 0.02
    max_stale_ms: int = 750
    max_ambiguity_score: float = 0.20
    min_evidence_score: float = 0.60
    min_source_count: int = 2
    min_time_to_close_seconds: int = 7200

    # allowed execution shape
    allowed_venues: list = field(default_factory=lambda: ["kalshi"])
    allowed_environments: list = field(default_factory=lambda: ["demo"])
    allowed_order_types: list = field(default_factory=lambda: ["FOK"])
    allowed_tif: list = field(default_factory=lambda: ["fill_or_kill"])
    allow_fak: bool = False
    allow_ioc: bool = False
    allow_gtc: bool = False
    allow_gtd: bool = False
    allow_post_only: bool = False
    allow_batch: bool = False
    allow_replace: bool = False
    allow_amend: bool = False
    allow_autonomous_loop: bool = False

    # prerequisites
    require_phase8_conformance: bool = True
    require_shadow_ready: bool = True
    required_shadow_status: str = "READY_FOR_MANUAL_REVIEW"
    max_shadow_report_age_hours: float = 24.0
    require_approvals: bool = True
    require_arming_token: bool = True
    require_dry_run_intent: bool = True
    require_account_snapshot: bool = True
    require_reconciliation: bool = True

    market_allowlist: list = field(default_factory=list)

    # kill switches
    kill_switch_path: str = "./MICRO_LIVE_KILL_SWITCH"
    guarded_live_kill_switch_path: str = "./GUARDED_LIVE_KILL_SWITCH"
    global_kill_switch_path: str = "./KILL_SWITCH"

    output_dir: str = "micro_live_artifacts"

    @classmethod
    def from_env(cls) -> "MicroLiveConfig":
        return cls(
            enabled=_b("MICRO_LIVE_ENABLED", "0"),
            environment=(os.getenv("MICRO_LIVE_ENV", "demo") or "demo").lower(),
            allow_production=_b("MICRO_LIVE_ALLOW_PRODUCTION", "0"),
            cli_only=_b("MICRO_LIVE_CLI_ONLY", "1"),
            one_order_per_token=_b("MICRO_LIVE_ONE_ORDER_PER_TOKEN", "1"),
            max_active_orders=min(_i("MICRO_LIVE_MAX_ACTIVE_ORDERS", 1), HARD_MAX_ACTIVE_ORDERS),
            max_orders_per_day=min(_i("MICRO_LIVE_MAX_ORDERS_PER_DAY", 1), HARD_MAX_ORDERS_PER_DAY),
            max_order_notional_usd=_capped("MICRO_LIVE_MAX_ORDER_NOTIONAL_USD", 1,
                                           HARD_MAX_ORDER_NOTIONAL_USD),
            max_daily_notional_usd=_capped("MICRO_LIVE_MAX_DAILY_NOTIONAL_USD", 1,
                                           HARD_MAX_DAILY_NOTIONAL_USD),
            max_market_exposure_usd=_capped("MICRO_LIVE_MAX_MARKET_EXPOSURE_USD", 1,
                                            HARD_MAX_ORDER_NOTIONAL_USD * 5),
            max_venue_exposure_usd=_capped("MICRO_LIVE_MAX_VENUE_EXPOSURE_USD", 1,
                                           HARD_MAX_DAILY_NOTIONAL_USD),
            max_total_exposure_usd=_capped("MICRO_LIVE_MAX_TOTAL_EXPOSURE_USD", 1,
                                           HARD_MAX_DAILY_NOTIONAL_USD),
            max_daily_loss_usd=_capped("MICRO_LIVE_MAX_DAILY_LOSS_USD", 1, HARD_MAX_DAILY_NOTIONAL_USD),
            min_edge_after_costs=_f("MICRO_LIVE_MIN_EDGE_AFTER_COSTS", 0.10),
            max_spread=_f("MICRO_LIVE_MAX_SPREAD", 0.02),
            max_stale_ms=_i("MICRO_LIVE_MAX_STALE_MS", 750),
            max_ambiguity_score=_f("MICRO_LIVE_MAX_AMBIGUITY_SCORE", 0.20),
            min_evidence_score=_f("MICRO_LIVE_MIN_EVIDENCE_SCORE", 0.60),
            min_source_count=_i("MICRO_LIVE_MIN_SOURCE_COUNT", 2),
            min_time_to_close_seconds=_i("MICRO_LIVE_MIN_TIME_TO_CLOSE_SECONDS", 7200),
            allowed_venues=_list("MICRO_LIVE_ALLOWED_VENUES", "kalshi"),
            allowed_environments=_list("MICRO_LIVE_ALLOWED_ENVIRONMENTS", "demo"),
            allowed_order_types=_list("MICRO_LIVE_ALLOWED_ORDER_TYPES", "FOK"),
            allowed_tif=_list("MICRO_LIVE_ALLOWED_TIF", "fill_or_kill"),
            allow_fak=_b("MICRO_LIVE_ALLOW_FAK", "0"),
            allow_ioc=_b("MICRO_LIVE_ALLOW_IOC", "0"),
            allow_gtc=_b("MICRO_LIVE_ALLOW_GTC", "0"),
            allow_gtd=_b("MICRO_LIVE_ALLOW_GTD", "0"),
            allow_post_only=_b("MICRO_LIVE_ALLOW_POST_ONLY", "0"),
            allow_batch=_b("MICRO_LIVE_ALLOW_BATCH", "0"),
            allow_replace=_b("MICRO_LIVE_ALLOW_REPLACE", "0"),
            allow_amend=_b("MICRO_LIVE_ALLOW_AMEND", "0"),
            allow_autonomous_loop=_b("MICRO_LIVE_ALLOW_AUTONOMOUS_LOOP", "0"),
            require_phase8_conformance=_b("MICRO_LIVE_REQUIRE_PHASE8_CONFORMANCE", "1"),
            require_shadow_ready=_b("MICRO_LIVE_REQUIRE_SHADOW_READY", "1"),
            required_shadow_status=os.getenv("MICRO_LIVE_REQUIRED_SHADOW_STATUS",
                                             "READY_FOR_MANUAL_REVIEW"),
            max_shadow_report_age_hours=_f("MICRO_LIVE_MAX_SHADOW_REPORT_AGE_HOURS", 24),
            require_approvals=_b("MICRO_LIVE_REQUIRE_APPROVALS", "1"),
            require_arming_token=_b("MICRO_LIVE_REQUIRE_ARMING_TOKEN", "1"),
            require_dry_run_intent=_b("MICRO_LIVE_REQUIRE_DRY_RUN_INTENT", "1"),
            require_account_snapshot=_b("MICRO_LIVE_REQUIRE_ACCOUNT_SNAPSHOT", "1"),
            require_reconciliation=_b("MICRO_LIVE_REQUIRE_RECONCILIATION", "1"),
            market_allowlist=_list("MICRO_LIVE_MARKET_ALLOWLIST", ""),
            kill_switch_path=os.getenv("MICRO_LIVE_KILL_SWITCH_PATH", "./MICRO_LIVE_KILL_SWITCH"),
            guarded_live_kill_switch_path=os.getenv("GUARDED_LIVE_KILL_SWITCH_PATH",
                                                    "./GUARDED_LIVE_KILL_SWITCH"),
            global_kill_switch_path=os.getenv("GLOBAL_KILL_SWITCH_PATH", "./KILL_SWITCH"),
            output_dir=os.getenv("MICRO_LIVE_OUTPUT_DIR", "micro_live_artifacts"),
        )

    def __post_init__(self):
        # enforce hard caps even if constructed directly
        self.max_order_notional_usd = min(Decimal(str(self.max_order_notional_usd)),
                                          HARD_MAX_ORDER_NOTIONAL_USD)
        self.max_orders_per_day = min(int(self.max_orders_per_day), HARD_MAX_ORDERS_PER_DAY)
        self.max_active_orders = min(int(self.max_active_orders), HARD_MAX_ACTIVE_ORDERS)
        # FOK-only: forbidden TIFs can never be allowed in Phase 9
        self.allowed_tif = [t for t in self.allowed_tif if t.lower() not in _FORBIDDEN_TIF] or \
            ["fill_or_kill"]
        self.allow_gtc = False
        self.allow_gtd = False
        self.allow_batch = False
        self.allow_replace = False
        self.allow_amend = False
        self.allow_autonomous_loop = False

    def order_type_allowed(self, order_type: str) -> bool:
        return order_type.upper() in [t.upper() for t in self.allowed_order_types]

    def tif_allowed(self, tif: str) -> bool:
        return tif.lower() in [t.lower() for t in self.allowed_tif] \
            and tif.lower() not in _FORBIDDEN_TIF

    def kill_switch_active(self) -> bool:
        for p in (self.kill_switch_path, self.guarded_live_kill_switch_path,
                  self.global_kill_switch_path):
            try:
                if p and Path(p).exists():
                    return True
            except OSError:
                continue
        return False

    def public_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.public_dict(), sort_keys=True,
                                         default=str).encode()).hexdigest()[:16]
