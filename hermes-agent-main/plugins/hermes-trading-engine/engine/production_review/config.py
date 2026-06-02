"""ProductionReviewConfig (Phase 11). Production-canary DESIGN REVIEW only.

This config NEVER enables production execution, production cancellation,
production signing, size increase, or autonomous live trading. The three
"enable_*" flags are forced False in code regardless of env; any env attempt to
enable production execution is surfaced as a critical review failure.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


class ProductionExecutionNotImplemented(Exception):
    """Raised by any production execution stub. Phase 11 implements none."""


def _i(name, d):
    try:
        return int(os.getenv(name, str(d)))
    except (TypeError, ValueError):
        return d


def _f(name, d):
    try:
        return float(os.getenv(name, str(d)))
    except (TypeError, ValueError):
        return d


def _b(name, d):
    return os.getenv(name, d) not in ("0", "false", "False", "")


# Env vars that, if set truthy, indicate an ATTEMPT to enable production
# execution. Phase 11 treats any of these as a critical review failure.
PRODUCTION_ENABLE_ATTEMPT_ENVS = (
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "PRODUCTION_REVIEW_ALLOW_SIZE_INCREASE",
    "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE",
    "PRODUCTION_REVIEW_ALLOW_DASHBOARD_SUBMIT",
    "PRODUCTION_REVIEW_ALLOW_API_SUBMIT",
    "ENABLE_REAL_ORDERS", "ENABLE_PRODUCTION", "PRODUCTION_EXECUTION",
)


@dataclass
class ProductionReviewConfig:
    enabled: bool = True
    output_dir: str = "production_review_artifacts"
    allow_readonly_account_snapshot: bool = False
    allow_production_network: bool = False
    mock_only_conformance: bool = True

    min_clean_demo_canaries: int = 5
    require_all_canaries_clean: bool = True
    require_no_unresolved_canaries: bool = True
    min_renewed_shadow_hours: float = 48.0
    min_renewed_shadow_decisions: int = 500
    max_evidence_age_hours: float = 72.0
    require_phase8_conformance: bool = True
    require_phase9_conformance: bool = True
    require_phase10_eligibility: bool = True

    require_jurisdiction_attestation: bool = True
    require_account_readiness_attestation: bool = True
    require_venue_terms_attestation: bool = True
    require_prohibited_market_review: bool = True
    require_funding_attestation: bool = True
    require_exchange_permission_attestation: bool = True

    block_order_endpoints: bool = True
    block_cancel_endpoints: bool = True
    block_deposit_withdraw_transfer: bool = True
    block_wallet_signing: bool = True
    require_readonly_trading_key_separation: bool = True
    require_secret_rotation_plan: bool = True
    require_secret_revocation_plan: bool = True
    secret_scan_enabled: bool = True

    require_incident_response_plan: bool = True
    require_rollback_plan: bool = True
    require_monitoring_plan: bool = True
    require_manual_exchange_ui_checklist: bool = True
    require_change_control: bool = True
    required_human_reviewers: int = 2
    approval_expiry_hours: float = 24.0

    # HARD-OFF in Phase 11 (never settable True)
    enable_production_execution: bool = False
    allow_size_increase: bool = False
    allow_autonomous_live: bool = False
    allow_dashboard_submit: bool = False
    allow_api_submit: bool = False

    @classmethod
    def from_env(cls) -> "ProductionReviewConfig":
        return cls(
            enabled=_b("PRODUCTION_REVIEW_ENABLED", "1"),
            output_dir=os.getenv("PRODUCTION_REVIEW_OUTPUT_DIR", "production_review_artifacts"),
            allow_readonly_account_snapshot=_b("PRODUCTION_REVIEW_ALLOW_READONLY_ACCOUNT_SNAPSHOT", "0"),
            allow_production_network=_b("PRODUCTION_REVIEW_ALLOW_PRODUCTION_NETWORK", "0"),
            mock_only_conformance=_b("PRODUCTION_REVIEW_MOCK_ONLY_CONFORMANCE", "1"),
            min_clean_demo_canaries=_i("PRODUCTION_REVIEW_MIN_CLEAN_DEMO_CANARIES", 5),
            require_all_canaries_clean=_b("PRODUCTION_REVIEW_REQUIRE_ALL_CANARIES_CLEAN", "1"),
            require_no_unresolved_canaries=_b("PRODUCTION_REVIEW_REQUIRE_NO_UNRESOLVED_CANARIES", "1"),
            min_renewed_shadow_hours=_f("PRODUCTION_REVIEW_MIN_RENEWED_SHADOW_HOURS", 48),
            min_renewed_shadow_decisions=_i("PRODUCTION_REVIEW_MIN_RENEWED_SHADOW_DECISIONS", 500),
            max_evidence_age_hours=_f("PRODUCTION_REVIEW_MAX_EVIDENCE_AGE_HOURS", 72),
            require_phase8_conformance=_b("PRODUCTION_REVIEW_REQUIRE_PHASE8_CONFORMANCE", "1"),
            require_phase9_conformance=_b("PRODUCTION_REVIEW_REQUIRE_PHASE9_CONFORMANCE", "1"),
            require_phase10_eligibility=_b("PRODUCTION_REVIEW_REQUIRE_PHASE10_ELIGIBILITY", "1"),
            require_jurisdiction_attestation=_b("PRODUCTION_REVIEW_REQUIRE_JURISDICTION_ATTESTATION", "1"),
            require_account_readiness_attestation=_b("PRODUCTION_REVIEW_REQUIRE_ACCOUNT_READINESS_ATTESTATION", "1"),
            require_venue_terms_attestation=_b("PRODUCTION_REVIEW_REQUIRE_VENUE_TERMS_ATTESTATION", "1"),
            require_prohibited_market_review=_b("PRODUCTION_REVIEW_REQUIRE_PROHIBITED_MARKET_REVIEW", "1"),
            require_funding_attestation=_b("PRODUCTION_REVIEW_REQUIRE_FUNDING_ATTESTATION", "1"),
            require_exchange_permission_attestation=_b("PRODUCTION_REVIEW_REQUIRE_EXCHANGE_PERMISSION_ATTESTATION", "1"),
            block_order_endpoints=_b("PRODUCTION_REVIEW_BLOCK_ORDER_ENDPOINTS", "1"),
            block_cancel_endpoints=_b("PRODUCTION_REVIEW_BLOCK_CANCEL_ENDPOINTS", "1"),
            block_deposit_withdraw_transfer=_b("PRODUCTION_REVIEW_BLOCK_DEPOSIT_WITHDRAW_TRANSFER", "1"),
            block_wallet_signing=_b("PRODUCTION_REVIEW_BLOCK_WALLET_SIGNING", "1"),
            require_readonly_trading_key_separation=_b("PRODUCTION_REVIEW_REQUIRE_READONLY_TRADING_KEY_SEPARATION", "1"),
            require_secret_rotation_plan=_b("PRODUCTION_REVIEW_REQUIRE_SECRET_ROTATION_PLAN", "1"),
            require_secret_revocation_plan=_b("PRODUCTION_REVIEW_REQUIRE_SECRET_REVOCATION_PLAN", "1"),
            secret_scan_enabled=_b("PRODUCTION_REVIEW_SECRET_SCAN_ENABLED", "1"),
            require_incident_response_plan=_b("PRODUCTION_REVIEW_REQUIRE_INCIDENT_RESPONSE_PLAN", "1"),
            require_rollback_plan=_b("PRODUCTION_REVIEW_REQUIRE_ROLLBACK_PLAN", "1"),
            require_monitoring_plan=_b("PRODUCTION_REVIEW_REQUIRE_MONITORING_PLAN", "1"),
            require_manual_exchange_ui_checklist=_b("PRODUCTION_REVIEW_REQUIRE_MANUAL_EXCHANGE_UI_CHECKLIST", "1"),
            require_change_control=_b("PRODUCTION_REVIEW_REQUIRE_CHANGE_CONTROL", "1"),
            required_human_reviewers=_i("PRODUCTION_REVIEW_REQUIRED_HUMAN_REVIEWERS", 2),
            approval_expiry_hours=_f("PRODUCTION_REVIEW_APPROVAL_EXPIRY_HOURS", 24),
        )

    def __post_init__(self):
        # HARD invariants: Phase 11 can never authorize production execution.
        self.enable_production_execution = False
        self.allow_size_increase = False
        self.allow_autonomous_live = False
        self.allow_dashboard_submit = False
        self.allow_api_submit = False

    @staticmethod
    def production_enable_attempt_detected() -> list[str]:
        out = []
        for env in PRODUCTION_ENABLE_ATTEMPT_ENVS:
            if os.getenv(env, "0") not in ("0", "false", "False", ""):
                out.append(env)
        return out

    def public_dict(self) -> dict:
        return asdict(self)
