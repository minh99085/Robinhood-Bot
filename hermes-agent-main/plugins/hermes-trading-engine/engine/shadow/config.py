"""ShadowConfig — env-driven, frozen-per-session configuration (Phase 7).

mode is always "shadow_live". Online research is OFF by default (cached only).
A config_hash freezes the knobs for a session so artifacts/readiness are
reproducible. No secrets are stored on this object.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .schemas import SHADOW_MODE


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _ms_list(name: str, default: str) -> list[int]:
    out = []
    for x in os.getenv(name, default).split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except ValueError:
            continue
    return sorted(set(out))


@dataclass
class ShadowConfig:
    mode: str = SHADOW_MODE
    enabled: bool = False
    session_name: str = ""
    venues: list[str] = field(default_factory=lambda: ["polymarket", "kalshi"])
    start_on_boot: bool = False
    max_runtime_minutes: int = 0

    # scheduling (ms)
    candidate_refresh_ms: int = 30000
    research_refresh_ms: int = 300000
    decision_interval_ms: int = 5000
    equity_snapshot_ms: int = 5000
    readiness_snapshot_ms: int = 60000
    outcome_horizons_ms: list[int] = field(
        default_factory=lambda: [0, 5000, 30000, 60000, 300000, 900000, 3600000])

    # candidate selection
    max_candidates_per_cycle: int = 20
    max_candidates_per_venue: int = 10
    max_proposals_per_cycle: int = 5
    category_whitelist: list[str] = field(default_factory=list)
    category_blacklist: list[str] = field(default_factory=list)
    min_volume: Decimal = Decimal("0")
    min_open_interest: Decimal = Decimal("0")
    min_liquidity_score: float = 0.25
    max_spread: float = 0.05
    min_time_to_close_seconds: int = 3600
    require_resolution_rules: bool = True
    max_ambiguity_score: float = 0.35

    # sizing / risk
    default_notional_usd: Decimal = Decimal("5")
    max_order_notional_usd: Decimal = Decimal("5")
    max_market_exposure_usd: Decimal = Decimal("25")
    max_venue_exposure_usd: Decimal = Decimal("100")
    max_total_exposure_usd: Decimal = Decimal("250")
    min_edge_after_costs: float = 0.04
    max_open_orders: int = 25
    max_daily_loss_usd: Decimal = Decimal("50")

    # data quality
    max_stale_ms: int = 3000
    require_bbo: bool = True
    require_orderbook: bool = True
    block_on_sequence_gap: bool = True
    block_on_tick_size_dirty: bool = True
    block_on_venue_degraded: bool = True

    # research
    use_research: bool = True
    allow_online_research: bool = False
    use_cached_research: bool = True
    min_evidence_score: float = 0.35
    min_source_count: int = 2
    max_research_stale_seconds: int = 900

    # readiness thresholds
    min_decisions_for_readiness: int = 200
    min_runtime_hours_for_readiness: float = 24.0
    required_venue_uptime_pct: float = 0.98
    max_stale_book_rate: float = 0.01
    max_parse_error_rate: float = 0.001
    max_sequence_gap_rate: float = 0.001
    max_risk_bypass_count: int = 0
    max_unhandled_exception_count: int = 0
    min_fill_ratio: float = 0.25
    max_reject_rate: float = 0.80
    min_edge_capture_ratio: float = 0.10
    max_drawdown_pct: float = 0.10
    min_calibration_samples: int = 30
    max_brier_score: float = 0.25
    max_log_loss: float = 0.75
    max_ece: float = 0.10
    min_positive_edge_bucket_pnl: float = 0.0
    require_reconciliation_clean: bool = True

    # artifacts / kill switch
    output_dir: str = "shadow_artifacts"
    report_format: list[str] = field(default_factory=lambda: ["json", "md", "csv"])
    store_verbose_decisions: bool = True
    kill_switch_path: str = "./SHADOW_KILL_SWITCH"

    @classmethod
    def from_env(cls) -> "ShadowConfig":
        return cls(
            mode=(os.getenv("SHADOW_MODE", SHADOW_MODE) or SHADOW_MODE),
            enabled=_b("SHADOW_ENABLED", "0"),
            session_name=os.getenv("SHADOW_SESSION_NAME", "") or "",
            venues=_list("SHADOW_VENUES", "polymarket,kalshi"),
            start_on_boot=_b("SHADOW_START_ON_BOOT", "0"),
            max_runtime_minutes=_i("SHADOW_MAX_RUNTIME_MINUTES", 0),
            candidate_refresh_ms=_i("SHADOW_CANDIDATE_REFRESH_MS", 30000),
            research_refresh_ms=_i("SHADOW_RESEARCH_REFRESH_MS", 300000),
            decision_interval_ms=_i("SHADOW_DECISION_INTERVAL_MS", 5000),
            equity_snapshot_ms=_i("SHADOW_EQUITY_SNAPSHOT_MS", 5000),
            readiness_snapshot_ms=_i("SHADOW_READINESS_SNAPSHOT_MS", 60000),
            outcome_horizons_ms=_ms_list("SHADOW_OUTCOME_HORIZONS_MS",
                                         "0,5000,30000,60000,300000,900000,3600000"),
            max_candidates_per_cycle=_i("SHADOW_MAX_CANDIDATES_PER_CYCLE", 20),
            max_candidates_per_venue=_i("SHADOW_MAX_CANDIDATES_PER_VENUE", 10),
            max_proposals_per_cycle=_i("SHADOW_MAX_PROPOSALS_PER_CYCLE", 5),
            category_whitelist=_list("SHADOW_CATEGORY_WHITELIST", ""),
            category_blacklist=_list("SHADOW_CATEGORY_BLACKLIST", ""),
            min_volume=Decimal(str(_f("SHADOW_MIN_VOLUME", 0))),
            min_open_interest=Decimal(str(_f("SHADOW_MIN_OPEN_INTEREST", 0))),
            min_liquidity_score=_f("SHADOW_MIN_LIQUIDITY_SCORE", 0.25),
            max_spread=_f("SHADOW_MAX_SPREAD", 0.05),
            min_time_to_close_seconds=_i("SHADOW_MIN_TIME_TO_CLOSE_SECONDS", 3600),
            require_resolution_rules=_b("SHADOW_REQUIRE_RESOLUTION_RULES", "1"),
            max_ambiguity_score=_f("SHADOW_MAX_AMBIGUITY_SCORE", 0.35),
            default_notional_usd=Decimal(str(_f("SHADOW_DEFAULT_NOTIONAL_USD", 5))),
            max_order_notional_usd=Decimal(str(_f("SHADOW_MAX_ORDER_NOTIONAL_USD", 5))),
            max_market_exposure_usd=Decimal(str(_f("SHADOW_MAX_MARKET_EXPOSURE_USD", 25))),
            max_venue_exposure_usd=Decimal(str(_f("SHADOW_MAX_VENUE_EXPOSURE_USD", 100))),
            max_total_exposure_usd=Decimal(str(_f("SHADOW_MAX_TOTAL_EXPOSURE_USD", 250))),
            min_edge_after_costs=_f("SHADOW_MIN_EDGE_AFTER_COSTS", 0.04),
            max_open_orders=_i("SHADOW_MAX_OPEN_ORDERS", 25),
            max_daily_loss_usd=Decimal(str(_f("SHADOW_MAX_DAILY_LOSS_USD", 50))),
            max_stale_ms=_i("SHADOW_MAX_STALE_MS", 3000),
            require_bbo=_b("SHADOW_REQUIRE_BBO", "1"),
            require_orderbook=_b("SHADOW_REQUIRE_ORDERBOOK", "1"),
            block_on_sequence_gap=_b("SHADOW_BLOCK_ON_SEQUENCE_GAP", "1"),
            block_on_tick_size_dirty=_b("SHADOW_BLOCK_ON_TICK_SIZE_DIRTY", "1"),
            block_on_venue_degraded=_b("SHADOW_BLOCK_ON_VENUE_DEGRADED", "1"),
            use_research=_b("SHADOW_USE_RESEARCH", "1"),
            allow_online_research=_b("SHADOW_ALLOW_ONLINE_RESEARCH", "0"),
            use_cached_research=_b("SHADOW_USE_CACHED_RESEARCH", "1"),
            min_evidence_score=_f("SHADOW_MIN_EVIDENCE_SCORE", 0.35),
            min_source_count=_i("SHADOW_MIN_SOURCE_COUNT", 2),
            max_research_stale_seconds=_i("SHADOW_MAX_RESEARCH_STALE_SECONDS", 900),
            min_decisions_for_readiness=_i("SHADOW_MIN_DECISIONS_FOR_READINESS", 200),
            min_runtime_hours_for_readiness=_f("SHADOW_MIN_RUNTIME_HOURS_FOR_READINESS", 24),
            required_venue_uptime_pct=_f("SHADOW_REQUIRED_VENUE_UPTIME_PCT", 0.98),
            max_stale_book_rate=_f("SHADOW_MAX_STALE_BOOK_RATE", 0.01),
            max_parse_error_rate=_f("SHADOW_MAX_PARSE_ERROR_RATE", 0.001),
            max_sequence_gap_rate=_f("SHADOW_MAX_SEQUENCE_GAP_RATE", 0.001),
            max_risk_bypass_count=_i("SHADOW_MAX_RISK_BYPASS_COUNT", 0),
            max_unhandled_exception_count=_i("SHADOW_MAX_UNHANDLED_EXCEPTION_COUNT", 0),
            min_fill_ratio=_f("SHADOW_MIN_FILL_RATIO", 0.25),
            max_reject_rate=_f("SHADOW_MAX_REJECT_RATE", 0.80),
            min_edge_capture_ratio=_f("SHADOW_MIN_EDGE_CAPTURE_RATIO", 0.10),
            max_drawdown_pct=_f("SHADOW_MAX_DRAWDOWN_PCT", 0.10),
            min_calibration_samples=_i("SHADOW_MIN_CALIBRATION_SAMPLES", 30),
            max_brier_score=_f("SHADOW_MAX_BRIER_SCORE", 0.25),
            max_log_loss=_f("SHADOW_MAX_LOG_LOSS", 0.75),
            max_ece=_f("SHADOW_MAX_ECE", 0.10),
            min_positive_edge_bucket_pnl=_f("SHADOW_MIN_POSITIVE_EDGE_BUCKET_PNL", 0.0),
            require_reconciliation_clean=_b("SHADOW_REQUIRE_RECONCILIATION_CLEAN", "1"),
            output_dir=os.getenv("SHADOW_OUTPUT_DIR", "shadow_artifacts"),
            report_format=_list("SHADOW_REPORT_FORMAT", "json,md,csv"),
            store_verbose_decisions=_b("SHADOW_STORE_VERBOSE_DECISIONS", "1"),
            kill_switch_path=os.getenv("SHADOW_KILL_SWITCH_PATH", "./SHADOW_KILL_SWITCH"),
        )

    def kill_switch_active(self) -> bool:
        try:
            return bool(self.kill_switch_path and Path(self.kill_switch_path).exists())
        except OSError:
            return False

    def verify_safe_to_start(self) -> tuple[bool, str]:
        """Fail-closed pre-flight. Never allows anything resembling live trading."""
        if not self.enabled:
            return False, "shadow_disabled (set SHADOW_ENABLED=1)"
        if self.mode != SHADOW_MODE:
            return False, f"invalid mode {self.mode!r} (must be {SHADOW_MODE!r})"
        # No live broker / real-money mode may be configured.
        bad = (os.getenv("HTE_LIVE_BROKER") or os.getenv("LIVE_BROKER_ENABLED")
               or os.getenv("REAL_MONEY") or "")
        if bad not in ("", "0", "false", "False"):
            return False, "live broker configured — refusing to start shadow"
        if self.kill_switch_active():
            return False, f"kill switch present ({self.kill_switch_path})"
        return True, "ok"

    def config_hash(self) -> str:
        d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
        blob = json.dumps(d, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def public_dict(self) -> dict:
        """Config view safe for API/artifacts — no secrets are present here anyway."""
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
