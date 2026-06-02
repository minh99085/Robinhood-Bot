"""Polymarket-only PAPER training engine.

A high-speed, PAPER-ONLY training machine for Polymarket prediction markets:

  scan -> rank -> subscribe top candidates -> estimate fair probability ->
  trade only when fair prob beats the executable price after costs ->
  learn from every paper trade -> improve calibration + selection over time.

Hard safety invariants (enforced across this package):

* PAPER trading only. No real orders. No Micro Live. No production execution.
* No dashboard/API live-submit path. No wallet/private-key signing.
* Polymarket only — no Kalshi trading, no crypto arbitrage, no stocks.
* Grok may RESEARCH and estimate probability only; it can never place, cancel,
  approve, arm, scale, or size an order.
* Every paper trade passes a deterministic RiskEngine gate and is routed
  through the paper broker. Every paper fill links proposal_id,
  risk_decision_id, order_id, and fill_id.
"""

# Back-compat: the original engine/training.py module is now legacy.py inside
# this package. Re-export its public API so `engine.training.Reporter`,
# `phase_for`, `compute_metrics`, `next_phase_at`, etc. keep working unchanged.
from .legacy import *  # noqa: F401,F403
from . import legacy  # noqa: F401

from .config import (TrainingConfig, AggressivePaperTrainingConfig,
                     FORBIDDEN_LIVE_FLAGS, MODES)
from .algorithm_inventory import algorithm_inventory
from .metrics import ScanMetrics, bucket_label, liquidity_bucket, spread_bucket
from .candidate_ranker import CandidateRanker, rank_candidates, score_candidate
from .market_scanner import MarketScanner, ScanResult
from .subscription_manager import SubscriptionManager, SubscriptionHealth
from .institutional_features import (InstitutionalFeatures, compute_features,
                                     feature_coverage, binary_entropy, FEATURE_FIELDS)
from .market_grouping import (EventGroup, group_markets, bregman_suitability,
                              grouping_metrics, detection_precision, GROUP_TYPES)
from .probability_stack import ProbabilityEstimate, ProbabilityStack
from .edge_engine import EdgeEngine, EdgeResult, NO_TRADE_REASONS
from .paper_policy import PaperPolicy, TradeProposal
from .online_learner import OnlineLearner
from .feedback_loop import FeedbackLoop
from .baselines import BaselineComparator
from .diagnostics import DiagnosticsRecord, build_record
from .store import TrainingStore
from .polymarket_trainer import (
    PolymarketPaperTrainer,
    PolymarketTrainingEngine,
    TrainingRiskGate,
    PaperBroker,
    RiskDecision,
)

__all__ = [
    "TrainingConfig", "AggressivePaperTrainingConfig", "algorithm_inventory",
    "FORBIDDEN_LIVE_FLAGS", "MODES",
    "ScanMetrics", "bucket_label", "liquidity_bucket", "spread_bucket",
    "CandidateRanker", "rank_candidates", "score_candidate",
    "MarketScanner", "ScanResult",
    "SubscriptionManager", "SubscriptionHealth",
    "InstitutionalFeatures", "compute_features", "feature_coverage",
    "binary_entropy", "FEATURE_FIELDS",
    "EventGroup", "group_markets", "bregman_suitability", "grouping_metrics",
    "detection_precision", "GROUP_TYPES",
    "ProbabilityEstimate", "ProbabilityStack",
    "EdgeEngine", "EdgeResult", "NO_TRADE_REASONS",
    "PaperPolicy", "TradeProposal",
    "OnlineLearner", "FeedbackLoop", "BaselineComparator",
    "DiagnosticsRecord", "build_record", "TrainingStore",
    "PolymarketPaperTrainer", "PolymarketTrainingEngine", "TrainingRiskGate",
    "PaperBroker", "RiskDecision",
]
