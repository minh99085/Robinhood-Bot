"""Deterministic, offline replay / backtest framework (Phase 4).

Replays saved raw market events through the same RiskEngine + OMS + PaperBroker
used in paper trading, against the *replayed* book. No network, no Grok calls,
no live order submission. Results are isolated from operational tables by
``replay_run_id``.
"""

from . import calibration, metrics
from .clock import ReplayClock
from .episode import ReplayConfig, ReplayEpisode, ReplayEvent
from .event_loader import ReplayEventLoader
from .policy import (
    CachedGrokPolicy,
    ExistingStrategyPolicy,
    NoOpPolicy,
    RandomPolicy,
    ReplayPolicy,
    SimpleEdgePolicy,
    build_policy,
)
from .report import write_report
from .runner import ReplayRunner

__all__ = [
    "ReplayConfig", "ReplayEpisode", "ReplayEvent", "ReplayClock",
    "ReplayEventLoader", "ReplayRunner", "ReplayPolicy", "NoOpPolicy",
    "SimpleEdgePolicy", "CachedGrokPolicy", "ExistingStrategyPolicy",
    "RandomPolicy", "build_policy", "write_report", "metrics", "calibration",
]
