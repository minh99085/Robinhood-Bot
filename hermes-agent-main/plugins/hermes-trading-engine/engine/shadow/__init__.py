"""Phase 7: shadow-mode orchestration.

Runs the full live decision stack on live read-only data WITHOUT submitting
orders: records would-have-traded decisions, routes every proposal through the
RiskEngine, simulates fills with the PaperBroker, tracks subsequent market
outcomes, and produces a hard live-readiness report. No real order submission,
cancellation, live broker, wallet signing, Kalshi order endpoints, or private
user channels exist here.
"""

from __future__ import annotations

from .alerts import AlertManager
from .candidate_selector import ShadowCandidateSelector
from .config import ShadowConfig
from .decision_engine import ShadowDecisionEngine
from .metrics import (
    brier_score,
    by_venue,
    compute_session_metrics,
    ece,
    edge_capture,
    fill_ratio,
    log_loss,
    markout_by_horizon,
)
from .orchestrator import ShadowOrchestrator
from .outcome_tracker import ShadowOutcomeTracker
from .readiness import LiveReadinessGate
from .report import NO_LIVE_STATEMENT, write_report
from .scheduler import ShadowScheduler
from .schemas import (
    SHADOW_MODE,
    CandidateMarket,
    LiveReadinessReport,
    ReadinessGateResult,
    ShadowDecision,
    ShadowFill,
    ShadowObservation,
    ShadowOrder,
    ShadowSession,
)
from .shadow_oms import ShadowOMS

__all__ = [
    "ShadowConfig", "ShadowOrchestrator", "ShadowScheduler", "ShadowCandidateSelector",
    "ShadowDecisionEngine", "ShadowOMS", "ShadowOutcomeTracker", "LiveReadinessGate",
    "AlertManager", "write_report", "NO_LIVE_STATEMENT", "compute_session_metrics",
    "fill_ratio", "edge_capture", "by_venue", "markout_by_horizon", "brier_score",
    "log_loss", "ece", "SHADOW_MODE", "ShadowSession", "CandidateMarket", "ShadowDecision",
    "ShadowOrder", "ShadowFill", "ShadowObservation", "ReadinessGateResult",
    "LiveReadinessReport",
]
