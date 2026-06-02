"""Phase 8: guarded-live design skeleton (DRY-RUN ONLY).

Design the door, install the locks, do NOT open the door. Real execution is
impossible: every submit/cancel/replace raises LiveExecutionDisabled; there is no
live broker, no wallet signing, no order endpoint, and no private user channels.
"""

from __future__ import annotations

from .approval import ApprovalWorkflow
from .arming import ArmingTokenManager
from .broker_interfaces import LiveBrokerInterface
from .conformance import ConformanceHarness
from .config import GuardedLiveConfig
from .disabled_brokers import DisabledLiveBroker
from .dry_run import DryRunLiveBroker
from .errors import (
    ApprovalError,
    ArmingError,
    ConformanceFailure,
    GuardedLiveError,
    GuardedLiveStateError,
    LiveExecutionDisabled,
    SecretPolicyViolationError,
)
from .precheck import run_precheck
from .readiness_loader import load_latest_readiness, validate_readiness
from .report import NO_LIVE_STATEMENT, write_report
from .safety_envelope import SafetyEnvelope
from .schemas import (
    ApprovalBatch,
    ArmingTokenRecord,
    ConformanceCheck,
    ConformanceRun,
    DryRunOrderIntent,
    GuardedLivePrecheck,
    ManualApproval,
    PrecheckResult,
    SafetyEnvelopeDecision,
    SecretPolicyViolation,
)
from .secret_policy import SecretPolicy, redact
from .state_machine import FORBIDDEN_LIVE_STATES, STATES, GuardedLiveStateMachine
from .venue_mappers import map_kalshi_order, map_polymarket_order

__all__ = [
    "GuardedLiveConfig", "GuardedLiveStateMachine", "STATES", "FORBIDDEN_LIVE_STATES",
    "SafetyEnvelope", "ApprovalWorkflow", "ArmingTokenManager", "LiveBrokerInterface",
    "DisabledLiveBroker", "DryRunLiveBroker", "ConformanceHarness", "SecretPolicy", "redact",
    "run_precheck", "validate_readiness", "load_latest_readiness", "write_report",
    "NO_LIVE_STATEMENT", "map_polymarket_order", "map_kalshi_order",
    "LiveExecutionDisabled", "GuardedLiveError", "GuardedLiveStateError", "ApprovalError",
    "ArmingError", "ConformanceFailure", "SecretPolicyViolationError",
    "GuardedLivePrecheck", "PrecheckResult", "ManualApproval", "ApprovalBatch",
    "ArmingTokenRecord", "DryRunOrderIntent", "SafetyEnvelopeDecision", "ConformanceRun",
    "ConformanceCheck", "SecretPolicyViolation",
]
