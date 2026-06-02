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

from .metrics import LabelQualityMetrics
from .online_learner import OnlineLearner
from .settlement import LabelState, SettlementLabel, is_trainable_state


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
        # Settlement-label quality (Strategy Optimization + Live Monitoring):
        # only clean labels reach the calibrator/learner; dirty ones are counted.
        self.label_quality = LabelQualityMetrics()
        self.suppressed = 0

    def record_outcome(self, *, predicted_prob: float, predicted_edge: float,
                       realized_pnl: float, size_usd: float, win: bool,
                       category: str = "uncategorized", net_edge: float = 0.0,
                       spread: float = 0.0, liquidity: float = 0.0,
                       ambiguity: float = 0.0, evidence: float = 0.0,
                       markouts: Optional[dict] = None,
                       label: Optional[SettlementLabel] = None,
                       label_state: Optional[str] = None,
                       label_confidence: float = 1.0,
                       settlement_source: str = "paper_mark",
                       settlement_delay_ms=None) -> bool:
        """Feed one closed paper trade into calibration + learning.

        SETTLEMENT-TRUTH GATE: a closed position only trains the calibrator and
        learner when it carries a clean (``resolved_yes`` / ``resolved_no``)
        settlement label. ``label``/``label_state=None`` is treated as a legacy
        clean label (back-compat). Dirty labels (void / ambiguous / unresolved /
        partially_invalid / stale_resolution) are recorded for label-quality
        reporting and otherwise SUPPRESSED — they never move calibration or
        learner state. Returns True iff the outcome trained the model.
        """
        if label is not None:
            label_state = label.state
            label_confidence = label.confidence
            settlement_source = label.source
            settlement_delay_ms = label.delay_ms
        trainable = is_trainable_state(label_state)
        # effective state for reporting (legacy clean default when unlabelled)
        eff_state = label_state if label_state is not None else (
            LabelState.RESOLVED_YES if win else LabelState.RESOLVED_NO)
        self.label_quality.record(state=eff_state, trainable=trainable,
                                  confidence=label_confidence,
                                  delay_ms=settlement_delay_ms)
        if not trainable:
            self.suppressed += 1
            return False

        self.calibrator.record_outcome(predicted_prob=predicted_prob,
                                       predicted_edge=predicted_edge,
                                       realized_pnl=realized_pnl, size_usd=size_usd)
        self.learner.record_outcome(predicted_prob=predicted_prob, win=win,
                                    realized_pnl=realized_pnl, category=category,
                                    net_edge=net_edge, spread=spread,
                                    liquidity=liquidity, ambiguity=ambiguity,
                                    evidence=evidence, markouts=markouts,
                                    label_state=label_state, trainable=True)
        return True

    def label_quality_report(self) -> dict:
        """Label coverage / delay / ambiguous-rate / invalid-feedback suppression
        for the training report (Strategy Optimization & Live Monitoring)."""
        return self.label_quality.to_dict()

    def category_sample_progress(self, target: int) -> dict:
        """Per-category feedback-sample progress vs. an active-learning target.

        Surfaces which categories are still under-sampled so aggressive paper
        mode can steer exploration toward them (Portfolio Optimization /
        Monitoring). Read-only; never changes a gate."""
        target = int(target)
        counts = self.learner.category_sample_counts() if hasattr(
            self.learner, "category_sample_counts") else {}
        return {cat: {"samples": n, "target": target, "under_target": max(0, target - n),
                      "satisfied": n >= target}
                for cat, n in counts.items()}

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

    def calibration_instability(self) -> float:
        """Calibration instability in [0,1] from the learner (dispersion of per-
        bucket reliability gaps) — feeds the Bayesian shrink so an unstable
        calibrator is trusted less. 0 when the learner cannot report it."""
        fn = getattr(self.learner, "calibration_instability", None)
        return float(fn()) if callable(fn) else 0.0

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "updates": self.updates,
            "edge_adjustment": self.edge_adjustment(),
            "suppressed_dirty_labels": self.suppressed,
            "label_quality": self.label_quality_report(),
            "calibration_instability": self.calibration_instability(),
            "calibrator": self.calibrator.summary(),
            "learner": self.learner.summary(),
        }
