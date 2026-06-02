"""FeedbackLoop — recursive learning loop for the paper trainer.

Every closed paper trade feeds back into:

* the ``FeedbackCalibrator`` (rolling hit-rate / Brier -> an ``edge_adjustment``
  multiplier applied to the *next* cycle's net-edge threshold), and
* the ``OnlineLearner`` (bucketed calibration, category reliability, edge-bucket
  PnL, markouts).

Every ``interval_seconds`` it produces a training summary and persists state, so
the bot's calibration and market selection improve over time.

Quant scope — *Strategy Optimization & Robustness Testing* (recursive calibration
+ edge-threshold adaptation) and *Live Trading & Monitoring* (training summary).
The loop can only make the gate STRICTER when calibration degrades; it never
relaxes risk and never places, sizes, approves, or arms an order (PAPER ONLY).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from engine.campaigns.signal_models import FeedbackCalibrator

from .online_learner import OnlineLearner


class FeedbackLoop:
    def __init__(self, learner: OnlineLearner, *, calibrator: Optional[FeedbackCalibrator] = None,
                 interval_seconds: float = 300.0, enabled: bool = True):
        self.learner = learner
        self.calibrator = calibrator or FeedbackCalibrator(enabled=enabled)
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled)
        self.updates = 0
        self.last_update_ts = 0.0
        self._last_summary: dict = {}

    def record_outcome(self, *, predicted_prob: float, predicted_edge: float,
                       realized_pnl: float, size_usd: float, win: bool,
                       category: str = "uncategorized", net_edge: float = 0.0,
                       spread: float = 0.0, liquidity: float = 0.0,
                       ambiguity: float = 0.0, evidence: float = 0.0,
                       markouts: Optional[dict] = None) -> None:
        self.calibrator.record_outcome(predicted_prob=predicted_prob,
                                       predicted_edge=predicted_edge,
                                       realized_pnl=realized_pnl, size_usd=size_usd)
        self.learner.record_outcome(predicted_prob=predicted_prob, win=win,
                                    realized_pnl=realized_pnl, category=category,
                                    net_edge=net_edge, spread=spread,
                                    liquidity=liquidity, ambiguity=ambiguity,
                                    evidence=evidence, markouts=markouts)

    def edge_adjustment(self) -> float:
        """Multiplier on the next cycle's edge threshold.

        >1 widens the gate (good calibration / hit-rate); <1 tightens it. Poor
        global calibration error further dampens the multiplier."""
        if not self.enabled:
            return 1.0
        adj = float(self.calibrator.edge_adjustment())
        calib_err = self.learner.calibration_error()
        if calib_err > 0.15:                       # poor calibration -> be stricter
            adj *= max(0.6, 1.0 - calib_err)
        return round(adj, 3)

    def maybe_update(self, now: Optional[float] = None, force: bool = False) -> Optional[dict]:
        now = now or time.time()
        if not force and (now - self.last_update_ts) < self.interval_seconds:
            return None
        self.last_update_ts = now
        self.updates += 1
        self.learner.persist()
        self._last_summary = self.summary()
        return self._last_summary

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "updates": self.updates,
            "edge_adjustment": self.edge_adjustment(),
            "calibrator": self.calibrator.summary(),
            "learner": self.learner.summary(),
        }
