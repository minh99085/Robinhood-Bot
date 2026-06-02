"""PostCanaryConfig (Phase 10). Conservative analysis thresholds + veto rules.

This config NEVER enables live trading, NEVER scales size, and NEVER permits
production execution. ``size_increase_allowed`` and ``autonomous_live_allowed``
are forced False in code regardless of env.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal


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


def _ints(name, d):
    raw = os.getenv(name, d)
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            try:
                out.append(int(x))
            except ValueError:
                continue
    return out or [0, 5000, 30000, 60000, 300000, 900000, 3600000]


_DEFAULT_HORIZONS = "0,5000,30000,60000,300000,900000,3600000"


@dataclass
class PostCanaryConfig:
    enabled: bool = True
    auto_analyze_after_submit: bool = True
    require_analysis_before_next_canary: bool = True
    output_dir: str = "post_canary_artifacts"

    markout_horizons_ms: list = field(default_factory=lambda: _ints("X", _DEFAULT_HORIZONS))
    market_data_window_before_ms: int = 60000
    market_data_window_after_ms: int = 3600000
    allow_readonly_refresh: bool = False

    max_ack_latency_ms: int = 5000
    max_reconciliation_latency_ms: int = 30000
    max_slippage_bps: float = 50.0
    max_fee_deviation_bps: float = 10.0
    max_payload_drift_fields: int = 0
    allow_partial_fill: bool = False
    allow_unexpected_resting_order: bool = False
    allow_emergency_cancel_for_clean: bool = False
    unknown_status_blocks: bool = True

    max_bbo_age_ms: int = 750
    max_orderbook_age_ms: int = 750
    max_spread: float = 0.02
    require_sequence_clean: bool = True
    require_tick_clean: bool = True
    require_market_open_at_submit: bool = True

    max_ambiguity_score: float = 0.20
    min_evidence_score: float = 0.60
    min_source_count: int = 2
    require_risk_approved: bool = True
    require_safety_allowed: bool = True
    require_kill_switch_check: bool = True

    min_clean_demo_canaries_for_prod_review: int = 5
    min_renewed_shadow_hours_after_canary: float = 24.0
    min_renewed_shadow_decisions_after_canary: int = 200
    require_all_demo_canaries_clean_for_prod_review: bool = True

    # HARD-OFF in Phase 10 (never settable True)
    size_increase_allowed: bool = False
    autonomous_live_allowed: bool = False
    production_canary_implemented: bool = False

    secret_scan_enabled: bool = True
    require_audit_chain: bool = True
    require_no_forbidden_network_calls: bool = True

    # adverse-markout veto threshold (bps); large adverse markout -> repeat shadow
    max_adverse_markout_bps: float = 200.0

    @classmethod
    def from_env(cls) -> "PostCanaryConfig":
        c = cls(
            enabled=_b("POST_CANARY_ENABLED", "1"),
            auto_analyze_after_submit=_b("POST_CANARY_AUTO_ANALYZE_AFTER_SUBMIT", "1"),
            require_analysis_before_next_canary=_b("POST_CANARY_REQUIRE_ANALYSIS_BEFORE_NEXT_CANARY", "1"),
            output_dir=os.getenv("POST_CANARY_OUTPUT_DIR", "post_canary_artifacts"),
            markout_horizons_ms=_ints("POST_CANARY_MARKOUT_HORIZONS_MS", _DEFAULT_HORIZONS),
            market_data_window_before_ms=_i("POST_CANARY_MARKET_DATA_WINDOW_BEFORE_MS", 60000),
            market_data_window_after_ms=_i("POST_CANARY_MARKET_DATA_WINDOW_AFTER_MS", 3600000),
            allow_readonly_refresh=_b("POST_CANARY_ALLOW_READONLY_REFRESH", "0"),
            max_ack_latency_ms=_i("POST_CANARY_MAX_ACK_LATENCY_MS", 5000),
            max_reconciliation_latency_ms=_i("POST_CANARY_MAX_RECONCILIATION_LATENCY_MS", 30000),
            max_slippage_bps=_f("POST_CANARY_MAX_SLIPPAGE_BPS", 50),
            max_fee_deviation_bps=_f("POST_CANARY_MAX_FEE_DEVIATION_BPS", 10),
            max_payload_drift_fields=_i("POST_CANARY_MAX_PAYLOAD_DRIFT_FIELDS", 0),
            allow_partial_fill=_b("POST_CANARY_ALLOW_PARTIAL_FILL", "0"),
            allow_unexpected_resting_order=_b("POST_CANARY_ALLOW_UNEXPECTED_RESTING_ORDER", "0"),
            allow_emergency_cancel_for_clean=_b("POST_CANARY_ALLOW_EMERGENCY_CANCEL_FOR_CLEAN", "0"),
            unknown_status_blocks=_b("POST_CANARY_UNKNOWN_STATUS_BLOCKS", "1"),
            max_bbo_age_ms=_i("POST_CANARY_MAX_BBO_AGE_MS", 750),
            max_orderbook_age_ms=_i("POST_CANARY_MAX_ORDERBOOK_AGE_MS", 750),
            max_spread=_f("POST_CANARY_MAX_SPREAD", 0.02),
            require_sequence_clean=_b("POST_CANARY_REQUIRE_SEQUENCE_CLEAN", "1"),
            require_tick_clean=_b("POST_CANARY_REQUIRE_TICK_CLEAN", "1"),
            require_market_open_at_submit=_b("POST_CANARY_REQUIRE_MARKET_OPEN_AT_SUBMIT", "1"),
            max_ambiguity_score=_f("POST_CANARY_MAX_AMBIGUITY_SCORE", 0.20),
            min_evidence_score=_f("POST_CANARY_MIN_EVIDENCE_SCORE", 0.60),
            min_source_count=_i("POST_CANARY_MIN_SOURCE_COUNT", 2),
            require_risk_approved=_b("POST_CANARY_REQUIRE_RISK_APPROVED", "1"),
            require_safety_allowed=_b("POST_CANARY_REQUIRE_SAFETY_ALLOWED", "1"),
            require_kill_switch_check=_b("POST_CANARY_REQUIRE_KILL_SWITCH_CHECK", "1"),
            min_clean_demo_canaries_for_prod_review=_i(
                "POST_CANARY_MIN_CLEAN_DEMO_CANARIES_FOR_PROD_REVIEW", 5),
            min_renewed_shadow_hours_after_canary=_f(
                "POST_CANARY_MIN_RENEWED_SHADOW_HOURS_AFTER_CANARY", 24),
            min_renewed_shadow_decisions_after_canary=_i(
                "POST_CANARY_MIN_RENEWED_SHADOW_DECISIONS_AFTER_CANARY", 200),
            require_all_demo_canaries_clean_for_prod_review=_b(
                "POST_CANARY_REQUIRE_ALL_DEMO_CANARIES_CLEAN_FOR_PROD_REVIEW", "1"),
            secret_scan_enabled=_b("POST_CANARY_SECRET_SCAN_ENABLED", "1"),
            require_audit_chain=_b("POST_CANARY_REQUIRE_AUDIT_CHAIN", "1"),
            require_no_forbidden_network_calls=_b("POST_CANARY_REQUIRE_NO_FORBIDDEN_NETWORK_CALLS", "1"),
            max_adverse_markout_bps=_f("POST_CANARY_MAX_ADVERSE_MARKOUT_BPS", 200),
        )
        return c

    def __post_init__(self):
        # HARD invariants: Phase 10 can never authorize these.
        self.size_increase_allowed = False
        self.autonomous_live_allowed = False
        self.production_canary_implemented = False

    def public_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
